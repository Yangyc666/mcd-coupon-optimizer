"""端到端：真实麦当劳 MCP 数据 → 优惠券最优方案 → 官方计价接口复核。

这个脚本是「麦麦省钱军师」的主入口，也是本参赛项目**真实使用麦当劳 MCP**
的直接证据：菜单、优惠券、门店、计价全部来自 ``https://mcp.mcd.cn``。

用法
----
查找门店并优化（按城市 + 关键词）::

    python scripts/optimize_order.py --city 武汉 --keyword 光谷 --want 麦辣鸡腿汉堡三件套

直接指定门店编码::

    python scripts/optimize_order.py --store 1960090 --want 9900005456

一次点多个商品、并看前 5 个方案::

    python scripts/optimize_order.py --store 1960090 --want 麦辣鸡腿汉堡三件套 --want 中薯条 --top 5

输出里会同时给出**我们算的实付价**与**麦当劳官方 calculate-price 的实付价**，
两者一致才说明方案真的可落地。

关于取餐方式
------------
``--way`` 取 ``pickup``（到店自提，默认）或 ``delivery``（麦乐送）。外送场景
必须带 ``beCode``，脚本会自动从门店信息里取。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mcd_client as mcd          # noqa: E402
import optimizer as opt           # noqa: E402


# --------------------------------------------------------------------------
# 参数解析
# --------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="optimize_order.py",
        description="基于麦当劳 MCP 真实数据的优惠券最优方案求解",
    )
    parser.add_argument("--store", help="门店编码 storeCode；不填则用 --city/--keyword 搜索")
    parser.add_argument("--city", help="城市名，例如 武汉")
    parser.add_argument("--keyword", help="地点关键词，例如 光谷")
    parser.add_argument("--be-code", dest="be_code", help="业务编码 beCode（外送/得来速需要）")
    parser.add_argument(
        "--way", choices=["pickup", "delivery", "drive"], default="pickup",
        help="取餐方式：pickup 到店自提 / delivery 麦乐送 / drive 得来速",
    )
    parser.add_argument(
        "--want", action="append", default=[], metavar="商品名或编码",
        help="想吃的商品，可重复传多次；支持商品名模糊匹配或商品编码精确指定",
    )
    parser.add_argument("--top", type=int, default=3, help="展示前几个方案，默认 3")
    parser.add_argument(
        "--claimable", action="store_true",
        help="把麦麦省「可领取但尚未领取」的券也纳入测算（只读，不会真的领券）",
    )
    parser.add_argument(
        "--verify", action="store_true",
        help="对最优方案调用官方 calculate-price 复核（会真实请求，不产生订单）",
    )
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    return parser


WAY_TO_CODES = {
    "pickup": (mcd.ORDER_TYPE_DINE_IN, mcd.BE_TYPE_PICKUP),
    "delivery": (mcd.ORDER_TYPE_DELIVERY, mcd.BE_TYPE_DELIVERY),
    "drive": (mcd.ORDER_TYPE_DINE_IN, mcd.BE_TYPE_DRIVE_THRU),
}


# --------------------------------------------------------------------------
# 门店定位
# --------------------------------------------------------------------------

def resolve_store(args, token):
    order_type, be_type = WAY_TO_CODES[args.way]

    if args.store:
        # 直接给了 storeCode，但如果同时给了城市就顺手补全门店名称
        name = "门店 %s" % args.store
        if args.city and args.keyword:
            for s in mcd.find_stores(args.city, args.keyword, be_type, token=token):
                if s["storeCode"] == str(args.store):
                    name = s["name"]
                    break
        return {
            "storeCode": str(args.store),
            "beCode": args.be_code or "",
            "name": name,
        }

    if args.city or args.keyword:
        if not (args.city and args.keyword):
            raise SystemExit("按位置搜索时 --city 与 --keyword 必须同时提供（MCP 接口要求）。")
        stores = mcd.find_stores(args.city, args.keyword, be_type, token=token)
        if not stores:
            raise SystemExit("没有找到门店，换个关键词试试。")
        picked = stores[0]
        picked["beCode"] = args.be_code or picked.get("beCode") or ""
        return picked

    raise SystemExit("请用 --store 指定门店编码，或用 --city + --keyword 搜索门店。")


# --------------------------------------------------------------------------
# 商品匹配
# --------------------------------------------------------------------------

def resolve_wanted(menu, wanted):
    """把用户输入的商品名/编码解析成菜单里的具体商品。

    优先按编码精确命中，其次按名称包含匹配，最后按字符重合度兜底。
    返回 ``(命中列表, 未命中列表)``。
    """
    by_code = {it["code"]: it for it in menu}
    hits, missed = [], []

    for token in wanted:
        token = (token or "").strip()
        if not token:
            continue

        if token in by_code:
            hits.append(by_code[token])
            continue

        # 名称包含匹配：优先取名字最短的（越短越可能是用户真正想要的那个）
        candidates = [it for it in menu if token in it["name"]]
        if not candidates:
            # 反向包含：用户输入的比菜单名还长，例如输入带规格的全名
            candidates = [it for it in menu if it["name"] and it["name"] in token]
        if candidates:
            # 名称最短的优先（越短越可能是用户真正想要的那个）；同长度取便宜的
            candidates.sort(key=lambda it: (len(it["name"]), -it.get("price_cents", 0)))
            hits.append(candidates[0])
            continue

        # 字符重合度兜底
        best, best_score = None, 0.0
        token_set = set(token)
        for it in menu:
            name = it["name"]
            if not name:
                continue
            overlap = len(token_set & set(name))
            score = overlap / max(1, len(token_set | set(name)))
            if score > best_score:
                best, best_score = it, score
        if best is not None and best_score >= 0.34:
            hits.append(best)
        else:
            missed.append(token)

    # 去重，保持顺序
    seen, unique = set(), []
    for it in hits:
        if it["code"] not in seen:
            seen.add(it["code"])
            unique.append(it)
    return unique, missed


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def run(args):
    token = mcd.load_token()
    order_type, be_type = WAY_TO_CODES[args.way]

    if not args.want:
        raise SystemExit("请用 --want 指定至少一个想吃的商品。")

    store = resolve_store(args, token)
    log("门店：%s（storeCode=%s，beCode=%s）"
        % (store["name"], store["storeCode"], store.get("beCode") or "无"))

    menu = mcd.get_menu(store["storeCode"], store.get("beCode"), order_type, be_type, token)
    log("菜单：%d 个商品" % len(menu))
    if not menu:
        raise SystemExit("菜单为空，可能是门店未营业或参数不对。")

    hits, missed = resolve_wanted(menu, args.want)
    if missed:
        log("⚠️ 以下商品没匹配上：%s" % "、".join(missed))
    if not hits:
        raise SystemExit("想吃的商品一个都没匹配到，换个说法试试。")
    log("已匹配：%s" % "、".join("%s（%d 分）" % (h["name"], h["price_cents"]) for h in hits))

    # 券：门店可用券 + 卡包券（+ 可选的可领取券）
    store_coupons = mcd.get_store_coupons(
        store["storeCode"], store.get("beCode"), order_type, be_type, token=token
    )
    log("门店可用券：%d 张" % len(store_coupons))

    my_coupons = []
    try:
        raw_mine = mcd.get_my_coupons(token=token)
        my_coupons = normalize_wallet_coupons(raw_mine)
        log("卡包券：%d 张" % len(my_coupons))
    except mcd.McdMcpError as exc:
        log("卡包券读取跳过（%s）" % exc)

    claimable = []
    if args.claimable:
        try:
            raw_avail = mcd.get_available_coupons(token=token)
            claimable = normalize_claimable_coupons(raw_avail)
            log("可领取券（仅测算，未实际领取）：%d 张" % len(claimable))
        except mcd.McdMcpError as exc:
            log("可领取券读取失败（%s）" % exc)

    all_coupons = store_coupons + my_coupons + claimable

    payload = {
        "intent": {"wanted": [h["name"] for h in hits]},
        "menu": menu,
        "coupons": all_coupons,
        "options": {"top_n": max(1, args.top)},
    }
    result = opt.solve(payload)

    # ---- 复核：拿优化器给出的方案去问官方计价接口 ----
    if args.verify:
        verify_plans(result, store, order_type, be_type, hits, token)

    return result, store, hits, order_type, be_type, token


def normalize_wallet_coupons(raw_list):
    """卡包券 -> 优化器 coupon 结构。"""
    out = []
    for raw in raw_list or []:
        if not isinstance(raw, dict):
            continue
        title = raw.get("title") or raw.get("couponName") or raw.get("name") or ""
        out.append(
            {
                "id": str(raw.get("couponId") or raw.get("id") or title),
                "code": str(raw.get("couponCode") or ""),
                "name": title,
                "desc": title,
                "source": "wallet",
                "tradeDateTime": raw.get("tradeDateTime") or raw.get("expireTime"),
            }
        )
    return out


def normalize_claimable_coupons(raw_list):
    """可领取券 -> 优化器 coupon 结构（只用于测算）。

    麦当劳 MCP 的 ``available-coupons`` 只返回券名与状态，没有任何金额字段，
    所以折扣规则完全依赖券名文案，交由优化器的正则解析层处理。
    """
    out = []
    for raw in raw_list or []:
        if not isinstance(raw, dict):
            continue
        name = raw.get("couponName") or raw.get("title") or ""
        if not name:
            continue
        out.append(
            {
                "id": "claimable:%s" % name,
                "name": name,
                "desc": name,
                "source": "claimable",
                "status": raw.get("couponStatus") or "",
            }
        )
    return out


def verify_plans(result, store, order_type, be_type, hits, token):
    """用官方 calculate-price 复核优化器给出的方案。

    这是本项目的关键闭环：我们不只要"算出"一个方案，还要让麦当劳自己的
    计价引擎确认这个方案能真的省下这些钱。
    """
    log("")
    log("─── 官方计价接口复核 ───")

    plans = result.get("plans") or []
    if not plans:
        log("没有方案可复核。")
        return

    for plan in plans:
        coupon_id = (plan.get("coupon") or {}).get("id") or ""
        coupon_code = None
        items = []
        for it in plan.get("items") or []:
            entry = {"productCode": it["code"], "quantity": it.get("qty", 1)}
            items.append(entry)

        # 把券挂到第一个商品上（MCP 的约定：券跟随商品行传入）
        if coupon_id and not str(coupon_id).startswith("claimable:"):
            items[0]["couponId"] = coupon_id
            if coupon_code:
                items[0]["couponCode"] = coupon_code

        try:
            bill = mcd.calculate_price(
                store["storeCode"], items, store.get("beCode"),
                order_type, be_type, token=token,
            )
        except mcd.McdMcpError as exc:
            log("  方案 #%s 复核失败：%s" % (plan.get("rank"), exc))
            continue

        ours = plan.get("payable_cents")
        theirs = bill["payable_cents"]
        match = "✅ 一致" if ours == theirs else "⚠️ 不一致（我们算 %d 分，官方 %d 分）" % (ours, theirs)
        log("  方案 #%s  我们算 %s 元 / 官方 %s 元  %s"
            % (plan.get("rank"), opt.fmt_cents(ours), opt.fmt_cents(theirs), match))


def log(message):
    print(message, file=sys.stderr)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = build_parser().parse_args(argv)
    result, store, hits, order_type, be_type, token = run(args)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print()
    print("=" * 66)
    print(result.get("summary", ""))
    print("=" * 66)
    print()

    baseline = result.get("baseline_no_coupon") or {}
    print("不用券直接买：%s 元（%s）"
          % (baseline.get("payable", "0.00"),
             "、".join(i["name"] for i in baseline.get("items", []))))
    print()

    plans = result.get("plans") or []
    if not plans:
        print("没有任何券能带来正向收益 —— 建议直接原价下单。")
    else:
        for plan in plans:
            print("【方案 %s】用「%s」" % (plan["rank"], plan["coupon"].get("name")))
            for it in plan["items"]:
                role = "想买" if it["role"] == "wanted" else "凑单"
                print("    - %s  %s 元 ×%s  [%s]"
                      % (it["name"], it["price"], it.get("qty", 1), role))
            print("    原价 %s 元 － 优惠 %s 元 ＝ 实付 %s 元（净省 %s 元）"
                  % (plan["original_total"], plan["discount"],
                     plan["payable"], plan["net_saving"]))
            if plan.get("advice"):
                print("    建议：%s" % plan["advice"])
            print()

    skipped = result.get("skipped_coupons") or []
    if skipped:
        print("未采用的券（%d 张）：" % len(skipped))
        for s in skipped[:6]:
            print("    · %s（%s）" % (s.get("name"), s.get("rule")))
        if len(skipped) > 6:
            print("    · ...还有 %d 张" % (len(skipped) - 6))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
