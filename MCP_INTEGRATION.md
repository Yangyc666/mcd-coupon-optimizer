# MCP 集成说明

本文档说明「麦麦省钱军师」**实际调用**的麦当劳 MCP Server、Tool、调用流程与业务价值。

所有内容基于 **2026-10-09 对 `https://mcp.mcd.cn` 的真实联调**，不是纸面设计。
文中的字段名、金额单位、错误码、商品数量均为实测结果。

---

## 1. 接入信息

| 项目 | 值 |
|---|---|
| MCP Server 地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP（JSON-RPC 2.0 over POST） |
| 鉴权方式 | `Authorization: Bearer <MCP Token>` |
| 协议版本 | `2025-03-26` |
| 服务端标识 | `mcd-mcp` v`1.0.0` |
| 工具总数 | **35 个**（实测 `tools/list` 返回） |
| Token 申请 | <https://open.mcd.cn/mcp> → 手机号登录 → 控制台 → 激活 |

请求示例（本项目的 `scripts/mcd_client.py` 就是这么发的）：

```http
POST https://mcp.mcd.cn
Authorization: Bearer <MCP_TOKEN>
Content-Type: application/json
Accept: application/json, text/event-stream

{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}
```

---

## 2. 实际使用的 Tool

本项目主链路只依赖 6 个 Tool，覆盖「找店 → 拉菜单 → 取券 → 求解 → 复核」全流程。

| # | Tool | 在本项目中的作用 | 必填参数 |
|---:|---|---|---|
| 1 | `query-nearby-stores` | 定位门店，拿到 `storeCode` / `beCode` | `beType`, `searchType` |
| 2 | `query-meals` | 拉取门店完整菜单与价格 | `storeCode`, `orderType`, `beType` |
| 3 | `query-store-coupons` | 取「指定门店 + 订单类型」下可用的券 | `storeCode`, `orderType`, `beType` |
| 4 | `query-my-coupons` | 取用户卡包中的券 | 无 |
| 5 | `available-coupons` | 取麦麦省可领取的券（用于测算潜在优惠） | 无 |
| 6 | `calculate-price` | **官方计价复核**：验证我们算出的方案真能省这么多钱 | `storeCode`, `orderType`, `beType`, `items` |

辅助使用：

| Tool | 用途 |
|---|---|
| `now-time-info` | 取服务端时间，避免用本机时间判断预约时段 |
| `auto-bind-coupons` | 一键领取麦麦省全部可领券（可选，本项目默认不主动调用，避免修改用户账号） |

### 调用链

```
query-nearby-stores  ──►  storeCode
                            │
query-meals  ───────────────┼──►  菜单（127 个商品）
                            │
query-store-coupons  ───────┼──►  门店可用券
query-my-coupons  ──────────┤     卡包券
available-coupons  ─────────┘     可领券
                            │
                     optimizer.solve()          ← 本项目核心：位图 DP 求最优凑单
                            │
                  calculate-price（逐个方案）    ← 用麦当劳自己的计价引擎反向验证
```

---

## 3. 实测字段结构

### 3.1 `query-meals` 返回结构

```jsonc
{
  "success": true, "code": 200, "message": "请求成功",
  "data": {
    "meals": {                                   // 注意：以商品编码为 key 的字典
      "9900005456": {
        "name": "麦辣鸡腿汉堡三件套",
        "image": "https://menu-img.mcd.cn/...",
        "currentPrice": "33.5",                  // ⚠️ 字符串，单位「元」
        "originalPrice": "46",
        "canWithOrder": false
      }
    },
    "categories": [                              // 分类只引用编码
      { "name": "人气热卖", "meals": [{ "code": "9900016076", "tags": ["上新"] }] }
    ],
    "frequent": { "code": "1406", "tags": ["我的常点"] }
  }
}
```

### 3.2 `calculate-price` 返回结构

```jsonc
{
  "data": {
    "productOriginalPrice": 3350,   // ⚠️ 整数，单位「分」
    "productPrice": 3350,
    "originalPrice": 3350,
    "discount": 0,
    "price": 3350,                  // 实付
    "productList": [
      { "productCode": "9900005456", "productName": "麦辣鸡腿汉堡三件套",
        "quantity": 1, "originalSubtotal": 3350, "subtotal": 3350 }
    ],
    "takeWayList": [
      { "code": "eat-in", "title": "堂食" },
      { "code": "take-in-store", "title": "外带" }
    ]
  }
}
```

### 3.3 `query-store-coupons` 返回结构

```jsonc
{
  "data": [
    {
      "title": "满30元减5元",     // ⚠️ 折扣规则只存在于这行文案里
      "couponId": "...",
      "couponCode": "...",
      "tradeDateTime": "...",
      "products": [ { "productCode": "...", "productName": "..." } ]
    }
  ]
}
```

---

## 4. 联调中踩到的坑（都是真实踩过的）

这一节是本项目最有价值的部分。以下问题全部在 2026-10-09 接入真实接口当天暴露，
并已修复 + 补上回归测试。

### 坑 1：两个接口的金额单位不一致（**会产生 100 倍误差**）

| 接口 | 实测样例 | 单位 |
|---|---|---|
| `query-meals` → `currentPrice` | `"33.5"` | **元**，且是字符串 |
| `calculate-price` → `price` | `3350` | **分**，整数 |

把菜单价直接喂进按「元」设计的换算函数，实付会变成 `3350.00 元`（真实应
`33.50 元`）。**这个 bug 是在跑通端到端链路后才被肉眼发现的**，单元测试测不出来。

**修复**：`scripts/mcd_client.py` 统一把菜单价预先换算成整数分并放进
`price_cents` 字段；`optimizer.item_price_cents()` 优先读它，只有该字段缺失
时才把 `price` 当作「元」处理。两条路径都有回归测试
（`test_price_cents_takes_priority_over_price`）。

### 坑 2：`calculate-price` 的商品字段名是 `productCode`，不是 `code`

用 `code` 传参**不报错**，但会静默返回一份全 0 的账单
（`price: 0, productList: []`）。这类静默失败比报错更危险。

### 坑 3：优惠券的折扣规则没有结构化字段

`query-store-coupons` 与 `available-coupons` **都不返回**折扣金额、门槛、
折扣率等字段 —— 规则只写在 `title` 文案里（如「9.9元中杯冰美式」
「人气麦旋风买一送一」）。因此本项目必须做**券名文案解析**，这不是
可有可无的兜底，而是唯一的规则来源。

实测抓到的券名格式与解析结果：

| 真实券名 | 解析出的规则 | 券种 |
|---|---|---|
| `9.9元中杯冰美式` | 指定商品 9.90 元 | `fixed_price` |
| `人气麦旋风买一送一` | 买 1 送 1 | `bogo` |
| `免费脆薯饼` | 免费得指定商品 | `free_item` |
| `麦旋风任选` | **无法识别**（券名不含任何金额信息） | `unknown` |

对最后一种，本项目选择**诚实标注「规则未识别」而不是猜一个折扣**。宁可少算
一个方案，也不给用户一个可能落空的承诺。

### 坑 4：券名与菜单名对不上

券叫「免费脆薯饼」，该门店菜单里却只有「心形薯饼5块」；券叫
「9.9元中杯冰美式」，菜单里对应的是「麦咖啡™美式」。**券面用语与菜单用语
并不统一**，靠字符串匹配无法可靠映射。

本项目的处理方式：`_matches_scope()` 采用**双向启发式匹配**（券面关键词 →
商品名，商品全名 → 券面原文），匹配不上就归入「未采用」并在输出里列出，
**不硬凑优惠**。

### 坑 5：`query-nearby-stores` 按位置搜索时必须同时给 `city` 和 `keyword`

只给 `city` 会返回业务失败：

```json
{"success": false, "code": 600058, "message": "城市名或者关键词不能为空"}
```

### 坑 6：MCP 响应同时含文本与结构化数据，应优先用结构化数据

每个工具响应的 `result` 里有两份数据：

* `content[0].text` —— 给 LLM 读的 Markdown 摘要（含表格样式说明）
* `structuredContent` —— 干净的 JSON：`{success, code, message, datetime, traceId, data}`

让 LLM 去读散文再抽取数字，既慢又容易出错。本项目**优先消费
`structuredContent`**，仅在缺失时回退解析文本。

---

## 5. 业务价值

### 5.1 解决的真实问题

麦当劳的优惠券规则是「满 X 减 Y」「买一送一」「第二份半价」这类**有条件
优惠**。用户面对的真实决策是：

> 我手上有一张「满 30 减 5」，但我只想买个 6 元的甜筒 —— 要不要为了用券多花钱？

人工心算容易出错，而**市面上多数点餐助手只会无脑推荐用券**。本项目把这件事
变成一个可求解的优化问题：凑单本质是**子集和问题**，用位图 DP 求「达到门槛
且超出最少」的组合，再按实付金额排序所有候选方案。

### 5.2 核心差异化：会劝退

当凑单后的实付反而高于「不凑单、不用券」时，本项目会**明确劝退**：

> 只想喝可乐（9.5 元），手握「满 20 减 5」需要凑到 20 元。最便宜的凑法要加
> 11 元的东西，实付 15 元 —— **比不用券直接买 9.5 元还贵，别用**。

这是本项目与同类作品最关键的分野：目标是**让用户少花钱**，而不是**让用户用掉券**。

### 5.3 官方计价复核（闭环）

优化器给出方案后，本项目会把方案重新提交给 `calculate-price`，用麦当劳自己的
计价引擎核对实付金额：

```
方案 #1  我们算 28.90 元 / 官方 28.90 元  ✅ 一致
```

这让「省了多少钱」这句话**可验证**，而不是本项目的一家之言。

---

## 6. 可复现的验证方式

```bash
# 1) 连通性与 Token 自检（会打印工具总数、门店、菜单、计价结果）
python scripts/mcd_client.py

# 2) 端到端：找店 → 拉菜单 → 求解 → 官方复核
python scripts/optimize_order.py \
    --city 武汉 --keyword 光谷 \
    --want 麦辣鸡腿汉堡三件套 \
    --claimable --verify

# 3) 回归测试（36 个用例，含真实券面数据）
python -m unittest discover -s tests -v
```

2026-10-09 实测结果（武汉光谷通信广场餐厅 `storeCode=1960090`）：

| 指标 | 实测值 |
|---|---|
| 工具总数 | 35 |
| 菜单商品数 | 127 |
| 菜单拉取耗时 | ~2 s |
| 单次最优方案求解 | **1.1 ms**（300 项菜单 / 12 张券） |
| 单元测试 | 36 个全部通过 |

---

## 7. 已知边界（诚实声明）

1. **一次只用一张券**。当前优化器假设一单只能用一张券；若实际支持叠加，
   需要把求解扩展成「券组合搜索」，计算量会显著上升。
2. **定向券不会主动凑商品**。若用户篮子里没有券所指向的商品（例如手握
   「免费脆薯饼」但没点薯饼），当前不会主动建议加购该商品，只会在
   「未采用的券」里列出。
3. **券名不含金额信息的券无法测算**（如「麦旋风任选」）。这类券需要点开券面
   详情才能知道规则，本项目选择标注为「未识别」而非猜测。
4. **券面用语与菜单用语不一致时只能放弃匹配**，不会硬凑一个可能落空的优惠。

以上 4 点都是**有意选择的保守策略**：在这个场景下，给用户一个错的省钱方案，
比不给方案更糟糕。

---

## 8. 合规声明

* 本项目仅调用麦当劳官方 MCP Server 提供的公开能力，未使用任何非官方接口、
  爬虫或逆向手段。
* 项目不会代替用户下单；`create-order` 等交易类 Tool **不在调用范围内**。
* MCP Token 通过本地环境变量 `MCD_MCP_TOKEN` 或用户本机配置文件读取，
  **不写入代码、不提交到仓库**（`.gitignore` 已覆盖相关文件名模式）。
* 项目不对麦当劳品牌、产品或形象作任何贬低或与其他品牌的对比。
