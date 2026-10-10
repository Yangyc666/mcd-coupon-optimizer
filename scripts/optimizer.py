#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦麦省钱军师 (MCD Coupon Optimizer) - 核心求解器

纯标准库实现，零第三方依赖。

输入：一份 JSON（stdin 或命令行传入文件路径），包含菜单、可用优惠券、
      以及用户想吃的商品。
输出：按实付金额升序排列的方案列表，以及"这张券到底值不值得用"的判断。

设计要点
--------
1. 所有金额以「分」为单位用整数运算，杜绝浮点误差累积。
2. 优惠券规则采用两层解析：优先读取结构化字段；失败则回退到对券名/描述
   做正则提取。真实场景里券往往只有一句人话描述，这一层是必需的。
3. "凑单"在数学上是一个「子集和 ≥ 目标值且尽可能小」的问题。本模块用
   动态规划求解，并做了两处剪枝（见 cheapest_topup 的注释）。
4. 主动判断"凑单不划算"：为凑门槛而多买的东西若比券的优惠还贵，就直接
   劝退，而不是机械地把券用掉。

假设与边界（未经真实接口验证前请勿视作事实）
--------------------------------------------
* 默认「一单只能用一张优惠券」。麦当劳实际下单规则以官方为准，
  若实测支持叠加，需在 compute_discount 处扩展。
* 券的适用范围若无法从数据中判定，则按"全场通用"处理，并在输出中标注
  该推断，避免用户误信。
"""

from __future__ import annotations

import json
import re
import sys

__version__ = "0.1.0"

# --------------------------------------------------------------------------
# 金额工具：一律用「分」做整数运算
# --------------------------------------------------------------------------


def to_cents(value) -> int:
    """把元金额（可能是 float/str/None）转成分。四舍五入到分。"""
    if value is None:
        return 0
    if isinstance(value, int):
        return value * 100
    if isinstance(value, float):
        return int(round(value * 100))
    text = str(value).strip().replace(",", "").replace("￥", "").replace("¥", "")
    text = text.replace("元", "").strip()
    if not text:
        return 0
    try:
        return int(round(float(text) * 100))
    except ValueError:
        return 0


def fmt_cents(cents: int) -> str:
    """分 -> 便于阅读的元字符串。"""
    return "%.2f" % (cents / 100.0)


# --------------------------------------------------------------------------
# 优惠券模型与解析
# --------------------------------------------------------------------------

KIND_THRESHOLD_REDUCE = "threshold_reduce"  # 满 X 减 Y
KIND_PERCENT = "percent"                    # X 折
KIND_CASH = "cash"                          # 无门槛立减 / 代金券
KIND_FIXED_PRICE = "fixed_price"            # 指定商品 N 元
KIND_NTH = "nth"                            # 第 N 份优惠（如第二份半价）
KIND_BOGO = "bogo"                          # 买 N 送 M（含买一送一）
KIND_FREE_ITEM = "free_item"                # 免费 / 0 元得某商品
KIND_UNKNOWN = "unknown"

CN_DIGITS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _token_to_int(token) -> int:
    """把「一」「十二」「2」这类 token 转成整数，无法识别返回 0。

    相比 ``int(token)`` 更安全：中文数字、混合写法都不会抛异常。
    """
    token = (token or "").strip()
    if not token:
        return 0
    if token.isdigit():
        return int(token)
    if token in CN_DIGITS:
        return CN_DIGITS[token]
    if "十" in token:
        head, _, tail = token.partition("十")
        tens = CN_DIGITS.get(head, 1) if head else 1
        ones = CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    total = 0
    for ch in token:
        if ch not in CN_DIGITS:
            return 0
        total = total * 10 + CN_DIGITS[ch]
    return total

RE_FULL_REDUCE = re.compile(r"满\s*(\d+(?:\.\d+)?)\s*元?\s*(?:可|立)?\s*减\s*(\d+(?:\.\d+)?)")
RE_THRESHOLD = re.compile(r"满\s*(\d+(?:\.\d+)?)\s*元")
RE_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*折")
RE_HALF = re.compile(r"半价")
RE_NTH = re.compile(r"第\s*([0-9]+|[一二两三四五六七八九十])\s*份")
RE_FIXED = re.compile(r"(\d+(?:\.\d+)?)\s*元\s*(?:购|换|享|得)")
RE_CASH = re.compile(r"(?:立减|直减|减)\s*(\d+(?:\.\d+)?)\s*元")

# 「买 N 送 M」——麦当劳券面常见写法，如「人气麦旋风买一送一」
RE_BOGO = re.compile(
    r"买\s*([0-9]+|[一二两三四五六七八九十]+)\s*送\s*([0-9]+|[一二两三四五六七八九十]+)"
)
# 「免费X」「0 元X」——白送一件商品。
# 负向后顾 `(?<!\d)` 是必需的：否则「满 30 元享 8.5 折」里的「0元」会被
# 误判成免费券（这个 bug 是被单元测试抓到的）。
RE_FREE_ITEM = re.compile(r"(?:免费|(?<!\d)0\s*元|零元|免单)")
# 「9.9元中杯冰美式」——定价式写法：数字+元 紧跟商品名，后面不接数字。
# 必须放在 RE_FULL_REDUCE / RE_CASH 之后判定，否则会误吃「满30元减5元」。
RE_FIXED_LEAD = re.compile(r"(\d+(?:\.\d+)?)\s*元\s*((?![0-9])[^\s0-9元]{2,})")


def item_price_cents(item) -> int:
    """读取单个商品的单价（**分**）。

    ⚠️ 单位陷阱：麦当劳 MCP 两个接口的金额单位不一致 —— ``query-meals``
    返回「元」字符串（``"33.5"``），``calculate-price`` 返回「分」整数
    （``3350``）。因此上层的 MCP 适配层会把菜单价预先换算成 ``price_cents``
    （分）；本函数优先读它，避免再经 :func:`to_cents` 被放大 100 倍。

    只有当 ``price_cents`` 缺失时，才把 ``price`` 当作「元」交给
    :func:`to_cents` 换算 —— 这样既兼容纯离线的手写 JSON 用例，也兼容
    真实接口数据。
    """
    if not isinstance(item, dict):
        return 0
    explicit = item.get("price_cents")
    if explicit is not None:
        try:
            return int(explicit)
        except (TypeError, ValueError):
            pass
    return to_cents(item.get("price"))


class Coupon(object):
    """一张优惠券及其解析出的规则。"""

    def __init__(self, raw):
        raw = raw if isinstance(raw, dict) else {}
        self.raw = raw
        self.id = str(raw.get("id") or raw.get("couponId") or raw.get("code") or "")
        self.name = str(raw.get("name") or raw.get("title") or raw.get("couponName") or "")
        self.desc = str(
            raw.get("desc") or raw.get("description") or raw.get("remark")
            or raw.get("rule") or raw.get("useRule") or ""
        )
        self.source = str(raw.get("source") or "unknown")
        self.scope_keywords = []
        self.scope_inferred = True
        # 券面原文，供 _matches_scope 的「商品全名 → 券面」反向匹配使用
        self.scope_text = self._text()
        self._parse()

    # -- 内部：解析规则 ----------------------------------------------------
    def _text(self) -> str:
        return (self.name + " " + self.desc).strip()

    def _parse(self):
        text = self._text()

        # 第 1 层：结构化字段
        min_spend = _first_number(
            self.raw, ("minSpend", "min_spend", "threshold", "condition",
                       "limitAmount", "useCondition", "minAmount")
        )
        face = _first_number(
            self.raw, ("value", "amount", "reduce", "discountAmount",
                       "faceValue", "parValue")
        )

        # 第 2 层：从文本里提取
        m = RE_FULL_REDUCE.search(text)
        if m:
            self.kind = KIND_THRESHOLD_REDUCE
            self.min_spend_cents = to_cents(m.group(1))
            self.reduce_cents = to_cents(m.group(2))
        else:
            m_thr = RE_THRESHOLD.search(text)
            m_pct = RE_PERCENT.search(text)
            m_nth = RE_NTH.search(text)
            m_fix = RE_FIXED.search(text)
            m_cash = RE_CASH.search(text)
            m_bogo = RE_BOGO.search(text)
            m_free = RE_FREE_ITEM.search(text)
            m_fixed_lead = RE_FIXED_LEAD.search(text)

            # 判定顺序有意为之：先识别特征最强的券面写法（买赠、免费），
            # 再处理门槛/折扣类，最后才用「N 元 + 商品名」这种宽松模式兜底，
            # 避免把「满30元减5元」误判成定价券。
            if m_bogo:
                self.kind = KIND_BOGO
                self.buy_n = _token_to_int(m_bogo.group(1)) or 1
                self.free_n = _token_to_int(m_bogo.group(2)) or 1
                self.min_spend_cents = to_cents(m_thr.group(1)) if m_thr else 0
            elif m_free:
                self.kind = KIND_FREE_ITEM
                self.min_spend_cents = to_cents(m_thr.group(1)) if m_thr else 0
            elif m_nth:
                self.kind = KIND_NTH
                token = m_nth.group(1)
                self.nth = _token_to_int(token) or 2
                if RE_HALF.search(text):
                    self.percent = 0.5
                elif m_pct:
                    self.percent = _percent_to_ratio(m_pct.group(1))
                else:
                    self.percent = 0.5
                self.min_spend_cents = to_cents(m_thr.group(1)) if m_thr else 0
            elif m_pct:
                self.kind = KIND_PERCENT
                self.percent = _percent_to_ratio(m_pct.group(1))
                self.min_spend_cents = to_cents(m_thr.group(1)) if m_thr else 0
            elif m_fix:
                self.kind = KIND_FIXED_PRICE
                self.fixed_price_cents = to_cents(m_fix.group(1))
                self.min_spend_cents = to_cents(m_thr.group(1)) if m_thr else 0
            elif m_cash:
                self.kind = KIND_CASH
                self.reduce_cents = to_cents(m_cash.group(1))
                self.min_spend_cents = to_cents(m_thr.group(1)) if m_thr else 0
            elif m_fixed_lead:
                # 「9.9元中杯冰美式」：定价式券，商品名紧跟「元」之后
                self.kind = KIND_FIXED_PRICE
                self.fixed_price_cents = to_cents(m_fixed_lead.group(1))
                self.min_spend_cents = to_cents(m_thr.group(1)) if m_thr else 0
            elif min_spend or face:
                # 只有结构化字段可用
                self.min_spend_cents = min_spend
                if face >= 100 and face % 100 == 0:
                    # 形如 500 表示 5 元
                    self.kind = KIND_THRESHOLD_REDUCE
                    self.reduce_cents = int(face)
                else:
                    self.kind = KIND_CASH
                    self.reduce_cents = int(face)
            else:
                self.kind = KIND_UNKNOWN
                self.min_spend_cents = 0

        # 统一兜底：结构化字段若给出更精确的门槛，以它为准
        if min_spend and self.kind in (KIND_THRESHOLD_REDUCE, KIND_PERCENT, KIND_CASH):
            self.min_spend_cents = min_spend
        if self.kind == KIND_THRESHOLD_REDUCE and not self.reduce_cents and face:
            self.reduce_cents = int(face)

        self.scope_keywords = _extract_scope_keywords(self._text())

    def is_usable(self) -> bool:
        return self.kind != KIND_UNKNOWN

    def describe_rule(self) -> str:
        if self.kind == KIND_THRESHOLD_REDUCE:
            return "满 %s 元减 %s 元" % (fmt_cents(self.min_spend_cents), fmt_cents(self.reduce_cents))
        if self.kind == KIND_PERCENT:
            return "满 %s 元享 %s 折" % (fmt_cents(self.min_spend_cents), _ratio_to_text(self.percent))
        if self.kind == KIND_CASH:
            return "立减 %s 元" % fmt_cents(self.reduce_cents)
        if self.kind == KIND_FIXED_PRICE:
            return "指定商品 %s 元" % fmt_cents(getattr(self, "fixed_price_cents", 0))
        if self.kind == KIND_NTH:
            return "第 %d 份 %s" % (self.nth or 2, _ratio_to_text(self.percent))
        if self.kind == KIND_BOGO:
            return "买 %d 送 %d" % (getattr(self, "buy_n", 1), getattr(self, "free_n", 1))
        if self.kind == KIND_FREE_ITEM:
            return "免费得指定商品"
        return "规则未识别（券名：%s）" % (self.name or "(空)")

    def as_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "rule": self.describe_rule(),
            "min_spend_cents": self.min_spend_cents,
            "source": self.source,
            "scope_keywords": self.scope_keywords,
        }


def _first_number(raw: dict, keys) -> int:
    """从多个候选字段名里找出第一个能解析成金额的（分）。"""
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            value = raw[key]
            if isinstance(value, (int, float)):
                # 结构化分数字段通常已是"分"；但常见写法是元。
                # 判定：大于 100 且为整数视作分，否则视作元。
                if isinstance(value, int) and value >= 100:
                    return int(value)
                return to_cents(value)
            return to_cents(value)
    return 0


def _percent_to_ratio(token: str) -> float:
    """把「8.5 折」转成 0.85；把「85」这类非法值兜底为 0.5。"""
    try:
        value = float(token)
    except (TypeError, ValueError):
        return 0.5
    if value >= 10:      # 写成 85 折显然是笔误，按 85% 处理
        value = value / 10.0
    if value <= 0 or value >= 10:
        return 0.5
    return round(value / 10.0, 4)


def _ratio_to_text(ratio: float) -> str:
    return ("%.1f" % (ratio * 10)).rstrip("0").rstrip(".")


# 与券规则无关的词，避免被误当成商品关键词
_STOPWORDS = (
    "优惠券", "代金券", "券", "满", "减", "折", "立减", "元", "专享", "限时",
    "特惠", "可用", "指定", "门店", "全场", "通用", "第", "份", "半价", "免费",
    "赠", "换购", "套餐", "优惠", "活动", "新客", "会员", "领取", "使用",
    # 券面常见但绝不可能是商品名的词（实测自麦当劳真实券名）
    "买", "送", "任选", "免单", "零元", "人气", "抢购", "尝鲜", "一份", "两份",
)


def _extract_scope_keywords(text: str):
    """从券名/描述里粗提可能的商品关键词（用于判断券是否只对某商品生效）。

    这是一个启发式：命中与否只作为"提示"，不作为硬约束，避免因为文本描述
    不完整而误判为不可用。
    """
    if not text:
        return []
    cleaned = text
    for word in _STOPWORDS:
        cleaned = cleaned.replace(word, " ")
    cleaned = re.sub(r"[\d\.]+", " ", cleaned)
    cleaned = re.sub(r"[，。、；：！？,;:!?()（）\[\]【】\s]+", " ", cleaned)
    words = [w for w in cleaned.split(" ") if len(w) >= 2]
    # 去重且保持顺序
    seen, out = set(), []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:5]


# --------------------------------------------------------------------------
# 求解：凑单子问题
# --------------------------------------------------------------------------


def cheapest_topup(candidates, need_cents, max_pool=120, max_repeat=6):
    """从候选商品中选一个子集，使其总价 >= need_cents 且超出最少。

    参数
    ----
    candidates : list[(index, price_cents)]  已去重的候选商品（价格升序）
    need_cents : int  还差多少分才够到门槛
    max_pool   : int  参与 DP 的商品条目数上限（同一商品的多份算多条）
    max_repeat : int  同一商品最多买几份

    返回
    ----
    (sum_cents, [index, ...]) 或 None（凑不出）

    算法
    ----
    这是一个「子集和 >= 目标值且最小」的问题。本实现用了三层优化：

    剪枝 1（候选集）：最优解中的任一元素，其单价不会超过 need（除非解只有
        一个元素）。因为若解含多件且有一件比 need 还贵，单独买那一件就已达标
        且更省。故候选池 = {单价 < need} ∪ {单价 >= need 中最便宜的一件}。

    剪枝 2（状态上界，可证明）：设最优解 S 的总和为 T，取 S 中任意元素 p，
        必有 T - p < need —— 否则去掉 p 后仍是可行解且更小，与"最优"矛盾。
        因此 T < need + max(候选单价)。本实现即以此为 DP 状态上界，
        比"所有商品价格之和"小得多。

    剪枝 3（位图 DP）：用 Python 原生大整数当 bitset，`reach |= reach << price`
        一步完成整轮状态转移，位运算在 C 层执行，比字典逐状态遍历快一到两个
        数量级；同时保留每轮快照以便回溯出具体买了哪几件。
    """
    if need_cents <= 0:
        return (0, [])

    singles = [(i, c) for i, c in candidates if c >= need_cents]
    upper = min((c for _, c in singles), default=None)

    # 现实中凑单可以买多份同一商品（例如两个甜筒），所以按 need//单价 估算
    # 需要几份并允许重复；再按单价升序截断，兼顾真实性与状态规模。
    pool = []
    for i, c in candidates:
        if 0 < c < need_cents:
            repeat = min(max_repeat, max(1, need_cents // c))
            for _ in range(repeat):
                pool.append((i, c))
    pool.sort(key=lambda pair: pair[1])
    pool = pool[:max_pool]

    if not pool and upper is None:
        return None

    max_price = max((c for _, c in pool), default=0)
    cap = need_cents + max_price
    if upper is not None:
        cap = min(cap, upper)

    mask = (1 << (cap + 1)) - 1
    snapshots = [1]
    reach = 1
    for _, price in pool:
        reach |= reach << price
        reach &= mask          # 丢弃超出上界的位，防止整数无界膨胀
        snapshots.append(reach)

    best = None
    shifted = reach >> need_cents
    if shifted:
        lowest = (shifted & -shifted).bit_length() - 1
        best = need_cents + lowest

    # 单件直达是否更优
    if upper is not None and (best is None or upper < best):
        return (upper, [singles[0][0]])

    if best is None:
        return None

    # 回溯：从后往前判断某一件是否"必需"
    picked, cursor = [], best
    for i in range(len(pool) - 1, -1, -1):
        if (snapshots[i] >> cursor) & 1:
            continue            # 只用前 i 件就能凑出 cursor，当前这件不需要
        picked.append(pool[i][0])
        cursor -= pool[i][1]
    return (best, picked)


# --------------------------------------------------------------------------
# 求解：单张券的最优方案
# --------------------------------------------------------------------------


def _matches_scope(coupon: Coupon, item: dict) -> bool:
    """券是否适用于该商品。

    双向启发式匹配（任一命中即视为适用）：

    1. **券面关键词 → 商品名**：券里提到「脆薯饼」，商品叫「脆薯饼（1块）」。
    2. **商品全名 → 券面原文**：券名叫「人气麦旋风买一送一」，商品就叫
       「麦旋风」。纯单向匹配时这类券永远匹配不上，这是接入真实券面后
       才暴露出来的问题。

    始终不返回"硬排除"：若无任何关键词，视为全场通用。宁可把适用范围
    判宽，也不要把可用券误判成不可用。
    """
    if not coupon.scope_keywords:
        return True
    name = str(item.get("name") or "")
    haystack = name + " " + str(item.get("category") or "") + " " + " ".join(item.get("tags") or [])
    if any(word in haystack for word in coupon.scope_keywords):
        return True
    scope_text = getattr(coupon, "scope_text", "") or ""
    if name and scope_text and name in scope_text:
        return True
    return False


def evaluate(coupon: Coupon, basket, base_cents):
    """给定篮子和券，算出优惠额（分）与是否适用。

    返回 (适用?, 优惠分, 原因说明)
    """
    if not coupon.is_usable():
        return (False, 0, "券规则无法识别，建议跳过")
    if base_cents < coupon.min_spend_cents:
        return (False, 0, "未达门槛 %s 元" % fmt_cents(coupon.min_spend_cents))

    if coupon.kind == KIND_THRESHOLD_REDUCE:
        return (True, min(coupon.reduce_cents, base_cents), "")

    if coupon.kind == KIND_CASH:
        return (True, min(coupon.reduce_cents, base_cents), "")

    if coupon.kind == KIND_PERCENT:
        discount = int(round(base_cents * (1.0 - coupon.percent)))
        return (True, min(discount, base_cents), "")

    if coupon.kind == KIND_FIXED_PRICE:
        target = getattr(coupon, "fixed_price_cents", 0)
        best, hit = 0, None
        for item in basket:
            if not _matches_scope(coupon, item):
                continue
            price = item_price_cents(item)
            if price > target and price - target > best:
                best, hit = price - target, item
        if hit is None:
            return (False, 0, "篮子中没有该券适用的商品")
        return (True, min(best, base_cents), "适用于「%s」" % hit.get("name"))

    if coupon.kind == KIND_NTH:
        nth = coupon.nth or 2
        matched = [it for it in basket if _matches_scope(coupon, it)]
        if len(matched) < nth:
            return (False, 0, "需要该商品 %d 份，当前不足" % nth)
        # 取第 nth 份的价格，按折扣率减免
        price = item_price_cents(matched[nth - 1])
        discount = int(round(price * (1.0 - coupon.percent)))
        return (True, min(discount, base_cents), "第 %d 份优惠" % nth)

    if coupon.kind == KIND_BOGO:
        buy_n = getattr(coupon, "buy_n", 1) or 1
        free_n = getattr(coupon, "free_n", 1) or 1
        need = buy_n + free_n
        matched = [it for it in basket if _matches_scope(coupon, it)]
        if len(matched) < need:
            return (False, 0, "买 %d 送 %d 需要该商品 %d 份，当前只有 %d 份"
                    % (buy_n, free_n, need, len(matched)))
        # 免掉其中最便宜的一份，属于保守口径（不会高估优惠）
        prices = sorted(item_price_cents(it) for it in matched)
        discount = prices[0]
        return (True, min(discount, base_cents), "买 %d 送 %d" % (buy_n, free_n))

    if coupon.kind == KIND_FREE_ITEM:
        best, hit = 0, None
        for item in basket:
            if not _matches_scope(coupon, item):
                continue
            price = item_price_cents(item)
            if price > best:
                best, hit = price, item
        if hit is None:
            return (False, 0, "购物车里没有该券可用的商品")
        return (True, min(best, base_cents), "免费得「%s」" % hit.get("name"))

    return (False, 0, "暂不支持的券类型")


def plan_for_coupon(coupon, menu, wanted_indexes, wanted_cents, options):
    """为一张券求解最优购买方案。返回方案 dict 或 None。"""
    allow_topup = options.get("allow_topup", True)

    base_items = [menu[i] for i in wanted_indexes]
    base_cents = sum(item_price_cents(it) for it in base_items)

    # 方案 A：不凑单，直接用券（若达标）
    ok, discount, note = evaluate(coupon, base_items, base_cents)
    best_plan = None
    if ok:
        best_plan = {
            "coupon": coupon.as_dict(),
            "items": [_item_view(it, "wanted") for it in base_items],
            "original_total_cents": base_cents,
            "discount_cents": discount,
            "payable_cents": base_cents - discount,
            "topup_cents": 0,
            "advice": note or "无需凑单，直接用券即可",
        }

    # 方案 B：凑单到达门槛
    if allow_topup and coupon.min_spend_cents > base_cents:
        need = coupon.min_spend_cents - base_cents
        used = set(wanted_indexes)
        # 凑单只考虑未在篮子里的、且不是券定向的商品（避免重复计数）
        pool_pairs = []
        for i, item in enumerate(menu):
            if i in used:
                continue
            price = item_price_cents(item)
            if price > 0:
                pool_pairs.append((i, price))
        pool_pairs.sort(key=lambda p: p[1])

        topup = cheapest_topup(pool_pairs, need, max_pool=options.get("max_pool", 80))
        if topup:
            top_sum, top_idx = topup
            extra_items = [menu[i] for i in top_idx]
            full_basket = base_items + extra_items
            full_cents = base_cents + top_sum
            ok2, discount2, note2 = evaluate(coupon, full_basket, full_cents)
            if ok2:
                plan_items = _merge_items(
                    [_item_view(it, "wanted") for it in base_items]
                    + [_item_view(it, "topup") for it in extra_items]
                )
                topup_views = [v for v in plan_items if v.get("role") == "topup"]
                plan = {
                    "coupon": coupon.as_dict(),
                    "items": plan_items,
                    "original_total_cents": full_cents,
                    "discount_cents": discount2,
                    "payable_cents": full_cents - discount2,
                    "topup_cents": top_sum,
                    "advice": "为达到满 %s 元门槛，加购 %s（共 %s 元）" % (
                        fmt_cents(coupon.min_spend_cents),
                        _items_text(topup_views),
                        fmt_cents(top_sum),
                    ),
                }
                if best_plan is None or plan["payable_cents"] < best_plan["payable_cents"]:
                    best_plan = plan

    if best_plan is None:
        return None

    # 关键判断：凑单后若实付反而比"不去用这张券"更贵，则明确劝退
    if best_plan["topup_cents"] > 0:
        naive = base_cents
        if best_plan["payable_cents"] > naive:
            return None
        net_saving = naive - best_plan["payable_cents"]
        best_plan["net_saving_cents"] = net_saving
        if net_saving <= 0:
            best_plan["advice"] += "；但净省不足 1 分，不建议为用券而加购"
    else:
        best_plan["net_saving_cents"] = best_plan["discount_cents"]

    return best_plan


def _item_view(item, role):
    return {
        "code": item.get("code") or item.get("id") or "",
        "name": item.get("name") or "",
        "price_cents": item_price_cents(item),
        "role": role,
        "qty": 1,
    }


def _merge_items(views):
    """把同名同角色的条目合并成带数量的形式（凑单可能买多份）。"""
    merged, index = [], {}
    for view in views:
        key = (view.get("code"), view.get("name"), view.get("role"))
        if key in index:
            merged[index[key]]["qty"] += 1
        else:
            index[key] = len(merged)
            merged.append(view)
    return merged


def _items_text(views):
    """把商品列表拼成人话，如「甜筒×2、薯条」。"""
    return "、".join(
        "%s×%d" % (v["name"], v["qty"]) if v.get("qty", 1) > 1 else str(v["name"])
        for v in views
    )


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def normalize_menu(raw_menu):
    """把菜单规范成内部格式。字段名做容错。"""
    out = []
    for item in raw_menu or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("productName") or item.get("title") or ""
        price = None
        for key in ("price", "salePrice", "currentPrice", "unitPrice", "amount"):
            if item.get(key) not in (None, ""):
                price = item.get(key)
                break
        out.append({
            "code": item.get("code") or item.get("id") or item.get("productCode") or "",
            "name": name,
            "price": price,
            # MCP 适配层会把「元字符串」预先换算成整数分放在这里；
            # item_price_cents() 优先读它，避免二次换算造成 100 倍误差。
            "price_cents": item.get("price_cents"),
            "category": item.get("category") or item.get("categoryName") or "",
            "tags": item.get("tags") if isinstance(item.get("tags"), list) else [],
            "raw": item,
        })
    return out


def match_wanted(menu, wanted_names):
    """把用户想吃的商品名（模糊）匹配到菜单项。返回 (索引列表, 未命中的名字)。"""
    idx, missed = [], []
    for want in wanted_names or []:
        want = str(want).strip()
        if not want:
            continue
        hit = None
        for i, item in enumerate(menu):
            if want and want in str(item.get("name") or ""):
                hit = i
                break
        if hit is None:
            for i, item in enumerate(menu):
                name = str(item.get("name") or "")
                # 中文按 2 字滑窗做粗匹配
                for pos in range(0, max(0, len(want) - 1)):
                    if want[pos:pos + 2] and want[pos:pos + 2] in name:
                        hit = i
                        break
                if hit is not None:
                    break
        if hit is None:
            missed.append(want)
        elif hit not in idx:
            idx.append(hit)
    return idx, missed


def solve(payload) -> dict:
    """主入口：接收原始 dict，返回结果 dict。"""
    options = payload.get("options") or {}
    menu = normalize_menu(payload.get("menu"))
    wanted_names = payload.get("intent", {}).get("wanted") or payload.get("wanted") or []
    idx, missed = match_wanted(menu, wanted_names)

    base_items = [menu[i] for i in idx]
    base_cents = sum(item_price_cents(it) for it in base_items)

    raw_coupons = []
    raw_coupons.extend(payload.get("coupons") or [])
    raw_coupons.extend(payload.get("my_coupons") or [])

    coupons = []
    seen_ids = set()
    for raw in raw_coupons:
        try:
            c = Coupon(raw)
        except Exception:
            continue
        if c.id in seen_ids:
            continue
        seen_ids.add(c.id)
        coupons.append(c)

    plans = []
    skipped = []
    for c in coupons:
        plan = plan_for_coupon(c, menu, idx, base_cents, options)
        if plan:
            plans.append(plan)
        else:
            skipped.append({"id": c.id, "name": c.name, "rule": c.describe_rule()})

    plans.sort(key=lambda p: (p["payable_cents"], -p.get("net_saving_cents", 0)))
    top_n = int(options.get("top_n", 5))
    plans = plans[:top_n]

    for rank, plan in enumerate(plans, 1):
        plan["rank"] = rank

    result = {
        "ok": True,
        "version": __version__,
        "assumption": "默认一单仅使用一张优惠券；如实际可叠加需调整算法。",
        "wanted": {
            "matched": [menu[i]["name"] for i in idx],
            "missed": missed,
            "base_total": fmt_cents(base_cents),
        },
        "baseline_no_coupon": {
            "items": [_item_view(it, "wanted") for it in base_items],
            "payable_cents": base_cents,
            "payable": fmt_cents(base_cents),
        },
        "plans": [_plan_view(p) for p in plans],
        "skipped_coupons": skipped,
    }

    if plans:
        best = plans[0]
        baseline = base_cents + best["topup_cents"]
        saved = baseline - best["payable_cents"]
        result["summary"] = "最省方案：用【%s】，实付 %s 元，比原价省 %s 元。" % (
            best["coupon"]["name"] or best["coupon"]["id"],
            fmt_cents(best["payable_cents"]),
            fmt_cents(saved),
        )
        if best["topup_cents"] > 0:
            result["summary"] += "（含凑单加购 %s 元）" % fmt_cents(best["topup_cents"])
    else:
        result["summary"] = "没有找到比原价更划算的用券方案，建议直接按原价购买。"

    return result


def _plan_view(plan):
    return {
        "rank": plan.get("rank"),
        "coupon": plan["coupon"],
        "items": [
            {
                "code": it["code"],
                "name": it["name"],
                "price": fmt_cents(it["price_cents"]),
                "qty": it.get("qty", 1),
                "role": it["role"],
            }
            for it in plan["items"]
        ],
        "original_total": fmt_cents(plan["original_total_cents"]),
        "discount": fmt_cents(plan["discount_cents"]),
        "payable": fmt_cents(plan["payable_cents"]),
        "payable_cents": plan["payable_cents"],
        "net_saving": fmt_cents(plan.get("net_saving_cents", 0)),
        "topup_total": fmt_cents(plan["topup_cents"]),
        "advice": plan["advice"],
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    if argv and not argv[0].startswith("-"):
        with open(argv[0], "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    else:
        payload = json.load(sys.stdin)

    result = solve(payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
