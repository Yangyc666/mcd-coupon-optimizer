"""麦当劳 MCP 客户端：把 MCP 真实返回的数据适配成优化器可直接消费的格式。

协议
----
Streamable HTTP + JSON-RPC 2.0，POST 到 https://mcp.mcd.cn。
每个响应同时携带两份数据：

* ``content[].text``        —— 给 LLM 读的 Markdown 摘要
* ``structuredContent``     —— 结构化 JSON（``{success, code, message, data}``）

本模块**优先使用 structuredContent**，仅在缺失时回退解析文本。这比让 LLM
去读散文再转成数字可靠得多。

⚠️ 单位陷阱（实测踩到，务必注意）
---------------------------------
两个接口的金额单位**不一致**：

=====================  ==================  ================
接口                    实测样例              单位
=====================  ==================  ================
``query-meals``         ``"33.5"``          元，且是字符串
``calculate-price``     ``3350``            分，整数
=====================  ==================  ================

混用会造成 100 倍误差。因此本模块提供两个语义明确的函数：
:func:`yuan_to_cents` 用于菜单价，:func:`api_cents` 用于计价接口返回值。
禁止把 ``calculate-price`` 的数字直接丢给 ``to_cents()``。

Token 来源
----------
按优先级读取：环境变量 ``MCD_MCP_TOKEN`` → ``~/.workbuddy/mcp.json``。
本模块不落盘、不打印 Token。
"""

import io
import json
import os
import urllib.error
import urllib.request

MCP_URL = "https://mcp.mcd.cn"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".workbuddy", "mcp.json")

# 常见订单/业务类型取值（来自 tools/list 的 inputSchema）
ORDER_TYPE_DINE_IN = 1   # 到店（含到店自取 + 得来速车道取餐）
ORDER_TYPE_DELIVERY = 2  # 外送（含麦乐送 + 企业团餐）

BE_TYPE_PICKUP = 1       # 到店自提
BE_TYPE_DELIVERY = 2     # 麦乐送到家
BE_TYPE_DRIVE_THRU = 5   # 得来速（车道取餐）
BE_TYPE_GROUP = 6        # 企业团餐


class McdMcpError(RuntimeError):
    """MCP 调用失败（网络异常或业务返回 success=false）。"""


# --------------------------------------------------------------------------
# 鉴权
# --------------------------------------------------------------------------

def load_token():
    """读取 MCP Token：环境变量优先，其次本机 WorkBuddy 配置。"""
    token = os.environ.get("MCD_MCP_TOKEN", "").strip()
    if token:
        return token

    if os.path.exists(CONFIG_PATH):
        try:
            cfg = json.load(io.open(CONFIG_PATH, encoding="utf-8"))
            entry = (cfg.get("mcpServers") or {}).get("mcd-mcp") or {}
            auth = ((entry.get("headers") or {}).get("Authorization") or "")
            auth = auth.strip()
            if auth.lower().startswith("bearer "):
                return auth[7:].strip()
        except Exception:
            pass

    raise McdMcpError(
        "未找到麦当劳 MCP Token。请设置环境变量 MCD_MCP_TOKEN，"
        "或在 WorkBuddy 中配置 mcd-mcp 连接器。"
    )


# --------------------------------------------------------------------------
# 单位换算（显式命名，避免 100 倍错误）
# --------------------------------------------------------------------------

def yuan_to_cents(value):
    """把「元」金额（``query-meals`` 的字符串价）转成整数分。

    支持 ``"33.5"`` / ``33.5`` / ``"¥33.50"``。无法解析返回 0。
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value * 100
    if isinstance(value, float):
        return int(round(value * 100))
    text = str(value).strip()
    for junk in ("￥", "¥", "元", ",", " "):
        text = text.replace(junk, "")
    if not text:
        return 0
    try:
        return int(round(float(text) * 100))
    except ValueError:
        return 0


def api_cents(value):
    """``calculate-price`` 系列接口返回的金额**本来就是分**，原样取整。

    这个函数存在的唯一目的是让调用点显式表达"我知道这里已经是分"，
    从而不会被误用 ``to_cents()`` 放大 100 倍。
    """
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------
# JSON-RPC 调用
# --------------------------------------------------------------------------

def _post(payload, token, timeout=60):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        MCP_URL,
        data=body,
        headers={
            "Authorization": "Bearer %s" % token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise McdMcpError("HTTP %s：%s" % (exc.code, exc.reason))
    except Exception as exc:
        raise McdMcpError("无法连接麦当劳 MCP：%s" % exc)

    try:
        return json.loads(raw)
    except ValueError:
        # 极少数情况服务端可能以 SSE 形式返回，逐行找 data: 负载
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except ValueError:
                    continue
        raise McdMcpError("MCP 返回了无法解析的内容（前 200 字符）：%s" % raw[:200])


def call_tool(name, arguments=None, token=None, timeout=60):
    """调用一个 MCP 工具。

    返回 :class:`ToolResult`，同时保留结构化数据与文本，方便调用方按需取用。
    """
    token = token or load_token()
    resp = _post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        token,
        timeout=timeout,
    )

    if "error" in resp:
        err = resp["error"]
        raise McdMcpError("工具 %s 调用失败：%s" % (name, err.get("message") or err))

    result = resp.get("result") or {}
    text_chunks = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            text_chunks.append(item.get("text") or "")
    text = "\n".join(text_chunks).strip()

    if result.get("isError"):
        raise McdMcpError("工具 %s 返回错误：%s" % (name, text[:300]))

    structured = result.get("structuredContent")

    # 业务层失败（success=false）统一抛异常，避免调用方拿到空数据还以为成功
    if isinstance(structured, dict) and structured.get("success") is False:
        raise McdMcpError(
            "工具 %s 业务失败 [%s]：%s"
            % (name, structured.get("code"), structured.get("message"))
        )

    return ToolResult(name=name, structured=structured, text=text, raw=result)


class ToolResult(object):
    """一次工具调用的结果，结构化数据优先。"""

    __slots__ = ("name", "structured", "text", "raw")

    def __init__(self, name, structured, text, raw):
        self.name = name
        self.structured = structured
        self.text = text
        self.raw = raw

    @property
    def data(self):
        """``structuredContent.data``；不存在时返回 None。"""
        if isinstance(self.structured, dict):
            return self.structured.get("data")
        return None

    @property
    def has_structured(self):
        return isinstance(self.structured, dict) and "data" in self.structured


# --------------------------------------------------------------------------
# 业务封装
# --------------------------------------------------------------------------

def list_tools(token=None):
    """列出服务端提供的全部工具（用于自检与文档生成）。"""
    token = token or load_token()
    resp = _post({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, token)
    return ((resp.get("result") or {}).get("tools")) or []


def find_stores(city=None, keyword=None, be_type=BE_TYPE_PICKUP, search_type=2, token=None):
    """查找门店。``search_type=2`` 按位置搜索时必须同时给 city 与 keyword。"""
    args = {"beType": be_type, "searchType": search_type}
    if city:
        args["city"] = city
    if keyword:
        args["keyword"] = keyword
    result = call_tool("query-nearby-stores", args, token=token)
    stores = result.data or []
    return [normalize_store(s) for s in stores if isinstance(s, dict)]


def normalize_store(raw):
    """MCP 门店 -> 统一结构。"""
    return {
        "storeCode": str(raw.get("storeCode") or ""),
        "beCode": str(raw.get("beCode") or ""),
        "name": raw.get("storeName") or "",
        "address": raw.get("address") or "",
        "distance": raw.get("distance"),
        "reservation": raw.get("reservation"),
        "businessStatus": raw.get("businessStatus"),
    }


def get_menu(store_code, be_code=None, order_type=ORDER_TYPE_DINE_IN,
             be_type=BE_TYPE_PICKUP, token=None):
    """拉取门店菜单，扁平化成优化器需要的商品列表。

    返回 ``[{code, name, price, original_price, category, tags}, ...]``，
    其中 ``price`` 已由「元字符串」转成**整数分**。
    """
    args = {"storeCode": str(store_code), "orderType": order_type, "beType": be_type}
    if be_code:
        args["beCode"] = str(be_code)

    result = call_tool("query-meals", args, token=token)
    data = result.data or {}
    meals = data.get("meals") or {}
    if not isinstance(meals, dict):
        return []

    # 建立 code -> 所属分类 的索引
    category_of = {}
    for cat in data.get("categories") or []:
        if not isinstance(cat, dict):
            continue
        cat_name = (cat.get("name") or "").replace("\n", "").strip()
        for entry in cat.get("meals") or []:
            if isinstance(entry, dict) and entry.get("code"):
                category_of.setdefault(str(entry["code"]), cat_name)

    items = []
    for code, meal in meals.items():
        if not isinstance(meal, dict):
            continue
        code = str(code)
        items.append(
            {
                "code": code,
                "name": meal.get("name") or "",
                # MCP 给的 currentPrice 是「元」字符串（如 "33.5"）。
                # 这里显式换算一次，并把结果放进 price_cents；price 保持原样
                # 只作展示。优化器优先读 price_cents，因此绝不会二次换算。
                "price": meal.get("currentPrice"),
                "price_cents": yuan_to_cents(meal.get("currentPrice")),
                "original_price": meal.get("originalPrice"),
                "original_price_cents": yuan_to_cents(meal.get("originalPrice")),
                "category": category_of.get(code, ""),
                "tags": list(meal.get("tags") or []) if isinstance(meal.get("tags"), list) else [],
                "can_with_order": bool(meal.get("canWithOrder")),
            }
        )
    items.sort(key=lambda it: (it["category"], -it["price_cents"]))
    return items


def get_store_coupons(store_code, be_code=None, order_type=ORDER_TYPE_DINE_IN,
                      be_type=BE_TYPE_PICKUP, reservation_date=None, token=None):
    """查询【指定门店 + 订单类型】下可用的优惠券。

    注意：麦当劳 MCP **不返回折扣金额/门槛等结构化字段**，优惠规则只存在于
    ``title`` 文案中（例如「9.9元中杯冰美式」「满30减5」），因此本函数把
    ``title`` 原样交给优化器的规则解析层去解析。
    """
    args = {"storeCode": str(store_code), "orderType": order_type, "beType": be_type}
    if be_code:
        args["beCode"] = str(be_code)
    if reservation_date:
        args["reservationDate"] = reservation_date

    result = call_tool("query-store-coupons", args, token=token)
    raw_list = result.data or []
    if isinstance(raw_list, dict):
        raw_list = raw_list.get("list") or raw_list.get("coupons") or []

    coupons = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        products = []
        for prod in raw.get("products") or []:
            if isinstance(prod, dict):
                products.append(
                    {
                        "code": str(prod.get("productCode") or ""),
                        "name": prod.get("productName") or "",
                    }
                )
        coupons.append(
            {
                # 优化器只认 name/desc 来解析规则，所以把 title 同时放进两个字段，
                # 最大化正则命中率（title 里既有"满30减5"也有"9.9元"这类写法）。
                "id": str(raw.get("couponId") or ""),
                "code": str(raw.get("couponCode") or ""),
                "name": raw.get("title") or "",
                "desc": raw.get("title") or "",
                "source": "store",
                "tradeDateTime": raw.get("tradeDateTime"),
                "products": products,
            }
        )
    return coupons


def get_my_coupons(page="1", page_size="200", token=None):
    """查询用户卡包中的券（不做门店/渠道校验）。"""
    result = call_tool(
        "query-my-coupons", {"page": str(page), "pageSize": str(page_size)}, token=token
    )
    data = result.data
    if isinstance(data, dict):
        data = data.get("list") or data.get("coupons") or []
    return data or []


def get_available_coupons(token=None):
    """查询麦麦省当前可领取的券。"""
    result = call_tool("available-coupons", {}, token=token)
    return result.data or []


def auto_bind_coupons(token=None):
    """一键领取麦麦省所有可领的券。"""
    return call_tool("auto-bind-coupons", {}, token=token).text


def calculate_price(store_code, items, be_code=None, order_type=ORDER_TYPE_DINE_IN,
                    be_type=BE_TYPE_PICKUP, reservation_date=None,
                    need_tableware=False, token=None):
    """调用官方计价接口，返回**金额均为整数分**的账单。

    ``items`` 每项形如 ``{"productCode": "9900005456", "quantity": 1,
    "couponId": "...", "couponCode": "..."}``。

    ⚠️ 字段名是 ``productCode``，不是 ``code``——写错会静默返回全 0 账单。
    """
    args = {
        "storeCode": str(store_code),
        "orderType": order_type,
        "beType": be_type,
        "items": items,
    }
    if be_code:
        args["beCode"] = str(be_code)
    if reservation_date:
        args["reservationDate"] = reservation_date
    if need_tableware:
        args["needTableware"] = True

    result = call_tool("calculate-price", args, token=token)
    data = result.data or {}
    return {
        "original_cents": api_cents(data.get("originalPrice")),
        "product_cents": api_cents(data.get("productPrice")),
        "discount_cents": api_cents(data.get("discount")),
        "payable_cents": api_cents(data.get("price")),
        "products": [
            {
                "code": str(p.get("productCode") or ""),
                "name": p.get("productName") or "",
                "quantity": p.get("quantity"),
                "original_subtotal_cents": api_cents(p.get("originalSubtotal")),
                "subtotal_cents": api_cents(p.get("subtotal")),
            }
            for p in (data.get("productList") or [])
            if isinstance(p, dict)
        ],
        "take_way": data.get("takeWayList") or [],
    }


def get_server_time(token=None):
    """获取麦当劳服务端时间（避免用本机时间做预约判断）。"""
    return call_tool("now-time-info", {}, token=token).data or {}


def get_nutrition(token=None):
    """获取餐品营养数据。"""
    return call_tool("list-nutrition-foods", {}, token=token).data or []


# --------------------------------------------------------------------------
# 自检：python scripts/mcd_client.py
# --------------------------------------------------------------------------

def _selftest():
    print("Token 来源检查 ...", end=" ")
    token = load_token()
    print("OK（长度 %d，已隐去）" % len(token))

    print("工具清单 ...", end=" ")
    tools = list_tools(token)
    print("OK，共 %d 个工具" % len(tools))

    print("服务端时间 ...", end=" ")
    now = get_server_time(token)
    print(now.get("formatted") or now)

    print("门店查询（武汉光谷）...", end=" ")
    stores = find_stores("武汉", "光谷", token=token)
    print("OK，%d 家门店" % len(stores))
    if not stores:
        return
    store = stores[0]
    print("  选中：%s（storeCode=%s）" % (store["name"], store["storeCode"]))

    print("菜单拉取 ...", end=" ")
    menu = get_menu(store["storeCode"], token=token)
    print("OK，%d 个商品" % len(menu))
    if menu:
        top = menu[0]
        print("  样例：%s  %d 分  [%s]" % (top["name"], top["price_cents"], top["category"]))

    print("计价接口 ...", end=" ")
    if menu:
        bill = calculate_price(
            store["storeCode"],
            [{"productCode": menu[0]["code"], "quantity": 1}],
            token=token,
        )
        print("OK，实付 %d 分 / 原价 %d 分（优惠 %d 分）"
              % (bill["payable_cents"], bill["original_cents"], bill["discount_cents"]))


if __name__ == "__main__":
    _selftest()
