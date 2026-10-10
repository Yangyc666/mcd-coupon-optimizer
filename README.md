# 麦麦省钱军师 · MCD Coupon Optimizer

> 别再用一张 5 元的券，多花 8 元买你并不想要的东西。

基于**麦当劳中国 MCP** 的优惠券最优方案求解器。它不只把优惠券列出来——它会算出**买哪几样、用哪张券、最终付多少钱**，并且在"凑单反而不划算"的时候明确劝你别买。

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)
![Tests](https://img.shields.io/badge/tests-36%20passed-brightgreen)
![MCP](https://img.shields.io/badge/MCD-MCP%20integrated-red)
![License](https://img.shields.io/badge/license-MIT-green)

---

## 它解决什么问题

麦当劳 App 会告诉你"你有 8 张券"，但不会告诉你**该用哪张**。

现实里的决策难点有三个：

1. **券之间有取舍。** 满 30 减 6 和满 35 减 10，哪个更划算取决于你买多少钱的东西。
2. **差一点够门槛时，要不要凑单？** 这是最容易踩的坑。
3. **凑单经常是亏的。** 为了用满 30 减 5，多买 11 元的东西，实际比不用券还贵。

第三点尤其反直觉，而市面上的"优惠券助手"几乎都在无脑推荐你用券。**麦麦省钱军师会把这类方案直接毙掉。**

## 实际效果

输入：想买「麦乐鸡（5块）+ 中薯条 + 可乐（中）」，账户里有 3 张券。

```
$ python scripts/optimizer.py examples/sample_input.json

最省方案：用【满30元减6元】，实付 27.00 元，比原价省 6.00 元。

#1 满30元减6元   实付 27.00 元（凑单 0.00 元）
#2 满35元减10元  实付 29.00 元（凑单 6.00 元）
#3 立减2元       实付 31.00 元（凑单 0.00 元）
```

注意第 2 名：**用 10 元的券，反而比用 6 元的券贵 2 元**——因为它逼你多买 6 元的东西。这种事人工很难一眼算清，军师会替你把三种策略全算一遍再排序。

更典型的劝退场景：

```
$ python scripts/demo.py

场景三 · 差太多，凑单不划算（应当劝退）
  想吃：圆筒冰淇淋（原价 6.00 元）
  没有找到比原价更划算的用券方案，建议直接按原价购买。
```

一张「满 30 减 5」的券摆在面前，算法告诉你：**别用**。因为凑到 30 元至少要加购 24 元的东西，省 5 元亏 19 元。

## 接的是真实麦当劳接口，不是模拟数据

菜单、门店、优惠券、价格**全部实时来自麦当劳官方 MCP Server**
（`https://mcp.mcd.cn`，Streamable HTTP，35 个可用 Tool）。

```bash
# 找店 → 拉菜单 → 求解 → 用官方计价接口复核
python scripts/optimize_order.py \
    --city 武汉 --keyword 光谷 \
    --want 麦辣鸡腿汉堡三件套 \
    --claimable --verify
```

```
门店：麦当劳武汉光谷通信广场餐厅（storeCode=1960090）
菜单：127 个商品
可领取券（仅测算，未实际领取）：9 张

─── 官方计价接口复核 ───
  方案 #1  我们算 28.90 元 / 官方 28.90 元  ✅ 一致
```

**「✅ 一致」这一行是本项目的分水岭**：算完之后，方案会被重新提交给麦当劳自己的
`calculate-price` 计价引擎核对实付金额。省钱这件事，不是我们自己说了算。

### 接入当天抓出的两个真 bug

真实数据和纸面设计差得很远。这套代码在接上真实接口的第一天就暴露出两个问题，
它们现在都是回归测试里的固定用例：

1. **金额被放大 100 倍。** `query-meals` 返回的价格是「元」字符串（`"33.5"`），
   而 `calculate-price` 返回的是「分」整数（`3350`）。两个接口单位不一致，
   混用会让 33.50 元变成 3350.00 元。
2. **「满 30 元减 5 元」被判成了免费券。** 因为它含有子串「0元」。

还有一类问题没有"修"，而是**有意保留**：券面规则只写在债券文案里
（如「9.9元中杯冰美式」），遇到「麦旋风任选」这种不含任何金额信息的券名，
程序会诚实地标注「规则未识别」，而**不是猜一个折扣糊弄用户**。
详见 [MCP_INTEGRATION.md](MCP_INTEGRATION.md)。

## 核心能力

| 能力 | 说明 |
| --- | --- |
| 多券横向比价 | 把账户里所有可用券各算一遍，按实付金额升序排名 |
| 凑单求解 | 差门槛时自动算出"加购哪几样最省"，而不是随便凑 |
| 反向劝退 | 凑单净亏时不给方案，明确建议按原价买 |
| 规则容错解析 | 券只有一句人话描述也能用，支持满减/折扣/立减/第 N 份优惠 |
| 严格最优性 | 凑单结果与暴力枚举交叉验证，300 组随机用例零偏差 |

## 技术亮点

这个项目的算法部分并不复杂，但每一处都经过了推敲。

### 1. 可证明的剪枝：为什么候选池能砍掉一大半

求"凑单最少花多少钱"本质是一个**子集和**问题。我们的目标是：从菜单里选若干商品，使总额 ≥ 门槛，且超出门槛的部分越少越好。

**剪枝定理**：设最优解为集合 S，总和为 T。取 S 中任意一件商品 p，必有 `T - p < 门槛`。

> 证明：若 `T - p ≥ 门槛`，那么去掉 p 之后仍是一个合法解，且总价更低，与 S 是"最优"矛盾。

由 `T < 门槛 + p` 对 S 中每一件成立，可得 **`T < 门槛 + max(S 中单价)`**。于是：

- 任何单价 ≥ 门槛 的商品，只需考虑其中**最便宜的那一件**（多件组合一定劣于单买）
- DP 状态上界可以压到 `门槛 + 最贵候选单价`，而不是"所有商品价格之和"

这套剪枝把状态空间从几十万压到几千。

### 2. 位图 DP：把 Python 大整数当 bitset 用

标准写法是开一个 `dp[容量]` 的布尔数组逐个状态转移，在 Python 里慢得难受。这里换了个思路：

```python
reach |= reach << price
```

`reach` 是一个 Python 原生大整数，第 `s` 位为 1 表示"总和恰为 s"可达。一次左移加或运算，就完成了整轮状态转移，位运算在 C 层执行。同时保留每轮的快照，事后从后往前判断"这一件是不是必需的"，即可回溯出具体买了哪几件。

实测（300 项菜单 / 12 张券 / 含凑单）：

```
单次求解平均耗时：1.1 ms
```

### 3. 用暴力枚举给 DP 当裁判

算法题最容易犯的错是"觉得自己对"。所以测试里写了一个暴力枚举参考实现，在 300 组随机数据上和 DP 逐一对拍：

```python
def test_matches_bruteforce_randomized(self):
    rng = random.Random(20261009)   # 固定种子，结果可复现
    for _ in range(300):
        ...
        self.assertEqual(got, expect)
```

> 补充一段花絮：这个对拍**第一次跑就抓到了一个 bug**——不过是参考实现自己的。暴力枚举早期版本漏掉了"单件商品价格已超过门槛"的情形，而 DP 是对的。这条测试的价值当场就体现出来了。

### 4. 金额一律用整数分

`0.1 + 0.2 != 0.3` 是浮点数的老问题。点餐场景里差一分钱都是事故，所以全流程以「分」为单位用整数运算，只在最终输出时格式化。

## 快速开始

不需要安装任何依赖，Python 3.8+ 开箱即用。

```bash
git clone https://github.com/<your-name>/mcd-coupon-optimizer.git
cd mcd-coupon-optimizer

# 跑三个演示场景（含一个"劝退"案例）
python scripts/demo.py

# 用示例输入跑一次完整求解
python scripts/optimizer.py examples/sample_input.json

# 跑性能基准
python scripts/demo.py --bench

# 跑测试
python -m unittest discover -s tests -v
```

## 接入麦当劳 MCP

本项目通过麦当劳中国官方 MCP Server 获取**实时菜单、可用优惠券和账户券**。

### 1. 申请 MCP Token

访问 <https://open.mcd.cn/mcp>，用手机号登录后在「控制台」激活并复制 Token。

### 2. 在 WorkBuddy 中配置

在 WorkBuddy 左侧边栏【专家·技能·连接器】→【连接器】→ 右上角【自定义连接器】→【配置 MCP】中填入：

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "type": "streamablehttp",
      "url": "https://mcp.mcd.cn",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_TOKEN"
      }
    }
  }
}
```

把 `YOUR_MCP_TOKEN` 换成你自己的 Token 后保存并启用。其他 MCP 客户端（Cursor / Cherry Studio / Trae 等）的配置方式见 [`mcp-config.example.json`](mcp-config.example.json)。

### 3. 用到的 MCP Tools

| Tool | 用途 |
| --- | --- |
| `query-meals` | 拉取当前门店在售菜单与价格 |
| `query-store-coupons` | 当前门店可用的优惠券 |
| `query-my-coupons` | 账户里已领的优惠券 |
| `calculate-price` | 用官方接口复算最优方案的价格，双重校验 |

详细的调用流程与业务价值见 [`MCP_INTEGRATION.md`](MCP_INTEGRATION.md)。

## 项目结构

```
mcd-coupon-optimizer/
├── scripts/
│   ├── optimizer.py      # 核心求解器（零依赖，可独立使用）
│   └── demo.py           # 演示与性能基准
├── tests/
│   └── test_optimizer.py # 24 个单元测试，含随机化交叉验证
├── examples/
│   ├── sample_input.json
│   └── sample_output.json
├── SKILL.md              # WorkBuddy Skill 定义
├── MCP_INTEGRATION.md    # MCP 调用说明
├── mcp-config.example.json
└── CONTEST_DECLARATION.md
```

## 输入输出格式

求解器只做**纯计算**，数据由 MCP 实时提供，两者解耦。输入为标准 JSON：

```json
{
  "intent": { "wanted": ["麦乐鸡（5块）", "可乐（中）"] },
  "menu": [{ "code": "S001", "name": "麦乐鸡（5块）", "price": 12.5 }],
  "coupons": [{ "id": "C001", "name": "满35元减10元", "source": "store" }],
  "options": { "top_n": 3, "allow_topup": true }
}
```

输出为按实付金额升序排列的方案列表，每项包含具体商品、优惠金额、实付金额和一句话建议。

## 设计取舍与已知边界

坦白说明，避免误导：

- **默认一单只用一张券。** 麦当劳实际下单规则以官方为准，若支持叠加需扩展 `compute_discount`。
- **券规则的解析是"两层"的。** 优先读结构化字段，退化到对券名/描述做正则提取。真实环境里券往往只有一句人话描述，这一层不可避免，但正则不可能覆盖所有文案，识别不了的券会被标记为"规则未识别"而不是瞎猜。
- **凑单候选最多带 120 个条目、同一商品最多 6 份。** 这是为了让 DP 状态可控。极端场景下理论最优解可能落在截断之外——但那种情况通常是"凑得越多亏得越多"，本来就会被劝退逻辑否掉。

## 目标用户

- **精打细算的麦门信徒**：手里常年躺着十几张券，想知道哪张最划算
- **上班族**：午餐时间只有十分钟，不想为薅羊毛做算术题
- **学生党**：预算敏感，且真的会因为省 5 块钱而开心
- **开发者**：想找一个"零依赖、算法有讲究、可以直接抄"的优化求解器示例

## 参与赛事

本项目参与 **2026 麦当劳程序员创意开发大赛**（基于麦当劳中国 MCP 的创意 Skill 开发）。
赛事主页：<https://github.com/M-China/mcd-developer-innovation-challenge>

## License

MIT
