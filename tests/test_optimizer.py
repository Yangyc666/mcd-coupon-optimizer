#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦麦省钱军师 - 单元测试

运行方式（在项目根目录）：
    python -m unittest discover -s tests -v

测试覆盖：
* 金额整数化（含浮点陷阱）
* 优惠券文本解析（满减 / 折扣 / 立减 / 第二份）
* 凑单子集和：与暴力枚举交叉验证，确保 DP 结果是最优解
* 端到端：无需凑单 / 凑单划算 / 凑单不划算（应劝退）
"""

from __future__ import annotations

import os
import random
import sys
import unittest
from itertools import product

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

from optimizer import (  # noqa: E402
    Coupon,
    KIND_PERCENT,
    KIND_THRESHOLD_REDUCE,
    cheapest_topup,
    solve,
    to_cents,
)

# --------------------------------------------------------------------------
# 测试用固定数据
# --------------------------------------------------------------------------

MENU = [
    {"code": "A1", "name": "麦乐鸡（5块）", "price": 12.5, "category": "小食"},
    {"code": "A2", "name": "中薯条", "price": 11.0, "category": "小食"},
    {"code": "A3", "name": "可乐（中）", "price": 9.5, "category": "饮品"},
    {"code": "A4", "name": "圆筒冰淇淋", "price": 6.0, "category": "甜品"},
    {"code": "A5", "name": "香芋派", "price": 7.5, "category": "甜品"},
    {"code": "A6", "name": "巨无霸", "price": 23.0, "category": "汉堡"},
]

COUPON_20_5 = {"id": "C1", "name": "满20元减5元", "source": "store"}
COUPON_15_8 = {"id": "C2", "name": "满15元减8元", "source": "my"}
COUPON_DIRECT = {"id": "C3", "name": "立减3元", "source": "my"}
COUPON_PERCENT = {"id": "C4", "name": "满30元享8.5折", "source": "store"}


def brute_force_min_at_least(candidates, need, max_repeat=3):
    """暴力枚举参考实现：每件商品可取 0..max_repeat 份，求最小和 >= need。

    注意单件价格已 >= need 时只需考虑买 0 份或 1 份——再多买只会更贵。
    早期版本在这里漏掉了「单件直达」的情形，导致与 DP 结果不一致，
    这个 bug 正是被 test_matches_bruteforce 抓出来的。
    """
    if need <= 0:
        return 0
    best = None
    ranges = []
    for _, price in candidates:
        if price >= need:
            ceiling = 1
        else:
            ceiling = min(max_repeat, max(1, need // price))
        ranges.append(range(0, ceiling + 1))
    for combo in product(*ranges):
        total = sum(price * count for (_, price), count in zip(candidates, combo))
        if total >= need and (best is None or total < best):
            best = total
    return best


class TestMoney(unittest.TestCase):
    def test_to_cents_basic(self):
        self.assertEqual(to_cents(12.5), 1250)
        self.assertEqual(to_cents(0), 0)
        self.assertEqual(to_cents(None), 0)
        self.assertEqual(to_cents("9.9"), 990)
        self.assertEqual(to_cents("￥9.90"), 990)

    def test_no_float_drift(self):
        """0.1 + 0.2 这类浮点问题不应出现在金额计算里。"""
        total = to_cents(0.1) + to_cents(0.2)
        self.assertEqual(total, 30)
        self.assertEqual(total / 100.0, 0.3)


class TestCouponParsing(unittest.TestCase):
    def test_full_reduce_from_text(self):
        c = Coupon({"id": "X", "name": "满30元减5元"})
        self.assertEqual(c.kind, KIND_THRESHOLD_REDUCE)
        self.assertEqual(c.min_spend_cents, 3000)
        self.assertEqual(c.reduce_cents, 500)

    def test_full_reduce_no_yuan_word(self):
        c = Coupon({"id": "X", "name": "满28减6"})
        self.assertEqual(c.min_spend_cents, 2800)
        self.assertEqual(c.reduce_cents, 600)

    def test_percent(self):
        c = Coupon({"id": "X", "name": "全场8.5折"})
        self.assertEqual(c.kind, KIND_PERCENT)
        self.assertAlmostEqual(c.percent, 0.85)

    def test_direct_cash(self):
        c = Coupon({"id": "X", "name": "新客立减3元"})
        self.assertEqual(c.reduce_cents, 300)

    def test_structured_fields_win(self):
        c = Coupon({"id": "X", "name": "优惠券", "threshold": 25, "value": 400})
        self.assertEqual(c.min_spend_cents, 2500)
        self.assertEqual(c.reduce_cents, 400)

    def test_unparseable_is_marked_unknown(self):
        c = Coupon({"id": "X", "name": "神秘优惠券"})
        self.assertFalse(c.is_usable())

    def test_scope_keywords(self):
        c = Coupon({"id": "X", "name": "麦乐鸡第二份半价"})
        self.assertIn("麦乐鸡", c.scope_keywords)


class TestTopup(unittest.TestCase):
    def test_no_topup_needed(self):
        self.assertEqual(cheapest_topup([(0, 600)], 0), (0, []))

    def test_single_item_is_cheaper_than_combo(self):
        """need=1050，候选有 1100 的单品，也有 600+750 的组合；应选 1100。"""
        cands = [(0, 600), (1, 750), (2, 1100)]
        total, picked = cheapest_topup(cands, 1050)
        self.assertEqual(total, 1100)
        self.assertEqual(len(picked), 1)

    def test_repeat_is_allowed(self):
        """need=1200，最便宜的是 600，应买两份。"""
        cands = [(0, 600), (1, 1100), (2, 750)]
        total, picked = cheapest_topup(cands, 1200)
        self.assertEqual(total, 1200)
        self.assertEqual(len(picked), 2)

    def test_returns_none_when_impossible(self):
        # 所有候选都远超 need 时不至于无解（单件直达），
        # 真正的无解是候选池为空
        self.assertIsNone(cheapest_topup([], 500))

    def test_matches_bruteforce(self):
        """核心正确性：DP 结果必须与暴力枚举一致。"""
        cases = [
            ([(0, 600), (1, 750), (2, 1100)], 1050),
            ([(0, 450), (1, 620), (2, 1330)], 900),
            ([(0, 300), (1, 300), (2, 700)], 1000),
            ([(0, 250), (1, 480), (2, 990)], 1234),
        ]
        for cands, need in cases:
            with self.subTest(cands=cands, need=need):
                result = cheapest_topup(cands, need, max_repeat=3)
                got = result[0] if result else None
                expect = brute_force_min_at_least(cands, need, max_repeat=3)
                self.assertEqual(got, expect)

    def test_matches_bruteforce_randomized(self):
        """随机化交叉验证：300 组随机数据，DP 必须与暴力枚举结果完全一致。

        固定随机种子，保证测试可复现。
        """
        rng = random.Random(20261009)
        for _ in range(300):
            size = rng.randint(1, 5)
            cands = [(i, rng.randrange(100, 2500, 50)) for i in range(size)]
            need = rng.randrange(100, 3000, 50)
            with self.subTest(cands=cands, need=need):
                result = cheapest_topup(cands, need, max_repeat=3)
                got = result[0] if result else None
                expect = brute_force_min_at_least(cands, need, max_repeat=3)
                self.assertEqual(got, expect, "cands=%r need=%r" % (cands, need))


class TestEndToEnd(unittest.TestCase):
    def _payload(self, wanted, coupons, **options):
        return {
            "intent": {"wanted": wanted},
            "menu": MENU,
            "coupons": coupons,
            "options": options,
        }

    def test_no_topup_required(self):
        """麦乐鸡+可乐=22 元，满 20 减 5 直接可用，实付 17 元。"""
        result = solve(self._payload(["麦乐鸡", "可乐"], [COUPON_20_5]))
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["plans"]), 1)
        best = result["plans"][0]
        self.assertEqual(best["payable"], "17.00")
        self.assertEqual(best["topup_total"], "0.00")
        self.assertIn("无需凑单", best["advice"])

    def test_topup_is_worth_it(self):
        """只想喝可乐(9.5)，满15减8 需凑 5.5 元：加个甜筒(6) 实付 7.5，净省 2 元。"""
        result = solve(self._payload(["可乐"], [COUPON_15_8]))
        plans = result["plans"]
        self.assertEqual(len(plans), 1)
        best = plans[0]
        self.assertEqual(best["payable"], "7.50")
        self.assertEqual(best["topup_total"], "6.00")
        self.assertEqual(best["net_saving"], "2.00")
        self.assertIn("圆筒冰淇淋", best["advice"])

    def test_topup_not_worth_it_should_be_rejected(self):
        """只想喝可乐(9.5)，满20减5 需凑 10.5 元，最少也要加 11 元的东西，
        实付 15.5 反而比原价 9.5 更贵 —— 必须劝退，不给方案。"""
        result = solve(self._payload(["可乐"], [COUPON_20_5]))
        self.assertEqual(len(result["plans"]), 0)
        self.assertIn("建议直接按原价购买", result["summary"])

    def test_direct_cash_coupon(self):
        result = solve(self._payload(["可乐"], [COUPON_DIRECT]))
        best = result["plans"][0]
        self.assertEqual(best["payable"], "6.50")

    def test_percent_coupon(self):
        """巨无霸+薯条=34 元，8.5 折省 5.1 元，实付 28.9 元。"""
        result = solve(self._payload(["巨无霸", "中薯条"], [COUPON_PERCENT]))
        best = result["plans"][0]
        self.assertEqual(best["payable"], "28.90")

    def test_ranks_by_payable(self):
        """多张券同时可用时，应按实付金额升序排列。"""
        result = solve(self._payload(["麦乐鸡", "可乐", "中薯条"], [COUPON_DIRECT, COUPON_20_5]))
        payables = [p["payable_cents"] for p in result["plans"]]
        self.assertEqual(payables, sorted(payables))

    def test_missed_wanted_item_is_reported(self):
        result = solve(self._payload(["不存在的商品"], [COUPON_20_5]))
        self.assertIn("不存在的商品", result["wanted"]["missed"])

    def test_empty_menu(self):
        result = solve({"intent": {"wanted": []}, "menu": [], "coupons": []})
        self.assertTrue(result["ok"])
        self.assertEqual(result["plans"], [])

    def test_dedup_same_coupon_id(self):
        result = solve(self._payload(["麦乐鸡", "可乐"], [COUPON_20_5, dict(COUPON_20_5)]))
        self.assertEqual(len(result["plans"]), 1)


# --------------------------------------------------------------------------
# 真实券面回归测试
#
# 下面每条用例的券名/商品名都来自 2026-10-09 对麦当劳 MCP（mcp.mcd.cn）的
# 真实抓取，不是编造的样例。接入真实接口当天就靠这批用例抓出了两个 bug：
# 一个是菜单金额被二次换算放大 100 倍，一个是「满30元」里的「0元」被误判
# 成免费券。因此它们必须长期留在回归集里。
# --------------------------------------------------------------------------

import optimizer as opt  # noqa: E402


class TestRealWorldCoupons(unittest.TestCase):
    """对接麦当劳真实券面/菜单数据后的回归用例。"""

    def test_price_cents_takes_priority_over_price(self):
        """⚠️ 单位回归：MCP 的 query-meals 给「元」字符串、calculate-price 给「分」。

        适配层会把菜单价预先换算成分放进 price_cents。若优化器无视它、
        又把 price 当元换算一次，实付会凭空放大 100 倍（33.50 → 3350.00）。
        """
        # 两条路径必须给出同一个答案
        self.assertEqual(opt.item_price_cents({"price": "33.5"}), 3350)
        self.assertEqual(opt.item_price_cents({"price_cents": 3350}), 3350)
        # 同时存在时，显式的 price_cents 优先
        self.assertEqual(
            opt.item_price_cents({"price": "33.5", "price_cents": 3350}), 3350
        )
        self.assertEqual(opt.item_price_cents({}), 0)
        self.assertEqual(opt.item_price_cents(None), 0)

    def test_menu_price_cents_flows_through_solve(self):
        """端到端：带 price_cents 的菜单不应被放大 100 倍。"""
        payload = {
            "intent": {"wanted": ["麦辣鸡腿汉堡三件套"]},
            "menu": [
                {
                    "code": "9900005456",
                    "name": "麦辣鸡腿汉堡三件套",
                    "price": "33.5",
                    "price_cents": 3350,
                }
            ],
            "coupons": [],
        }
        result = solve(payload)
        self.assertEqual(result["baseline_no_coupon"]["payable_cents"], 3350)
        self.assertEqual(result["baseline_no_coupon"]["payable"], "33.50")

    def test_bogo_rule_parsed(self):
        """「人气麦旋风买一送一」（真实券名）。"""
        c = Coupon({"id": "B1", "name": "人气麦旋风买一送一"})
        self.assertEqual(c.kind, opt.KIND_BOGO)
        self.assertEqual(c.buy_n, 1)
        self.assertEqual(c.free_n, 1)

    def test_bogo_applies_with_two_units(self):
        c = Coupon({"id": "B1", "name": "人气麦旋风买一送一"})
        basket = [
            {"name": "经典麦旋风", "price_cents": 1550},
            {"name": "经典麦旋风", "price_cents": 1550},
        ]
        ok, discount, _ = opt.evaluate(c, basket, 3100)
        self.assertTrue(ok)
        self.assertEqual(discount, 1550)

    def test_bogo_rejected_with_one_unit(self):
        c = Coupon({"id": "B1", "name": "人气麦旋风买一送一"})
        basket = [{"name": "经典麦旋风", "price_cents": 1550}]
        ok, discount, note = opt.evaluate(c, basket, 1550)
        self.assertFalse(ok)
        self.assertIn("2 份", note)

    def test_free_item_rule_and_discount(self):
        """「免费脆薯饼」（真实券名）。"""
        c = Coupon({"id": "F1", "name": "免费脆薯饼"})
        self.assertEqual(c.kind, opt.KIND_FREE_ITEM)
        ok, discount, _ = opt.evaluate(c, [{"name": "脆薯饼", "price_cents": 800}], 800)
        self.assertTrue(ok)
        self.assertEqual(discount, 800)

    def test_free_item_without_matching_product_is_rejected(self):
        c = Coupon({"id": "F1", "name": "免费脆薯饼"})
        ok, discount, _ = opt.evaluate(c, [{"name": "可乐", "price_cents": 950}], 950)
        self.assertFalse(ok)
        self.assertEqual(discount, 0)

    def test_fixed_price_lead_form(self):
        """「9.9元中杯冰美式」：元后面直接跟商品名，没有购/换/享/得。"""
        c = Coupon({"id": "P1", "name": "9.9元中杯冰美式"})
        self.assertEqual(c.kind, opt.KIND_FIXED_PRICE)
        self.assertEqual(c.fixed_price_cents, 990)

    def test_threshold_reduce_not_mistaken_for_free_item(self):
        """⚠️ 回归：「满30元减5元」里含有子串「0元」，不是免费券。"""
        c = Coupon({"id": "R1", "name": "满30元减5元"})
        self.assertEqual(c.kind, opt.KIND_THRESHOLD_REDUCE)
        self.assertEqual(c.min_spend_cents, 3000)
        self.assertEqual(c.reduce_cents, 500)

    def test_percent_not_mistaken_for_free_item(self):
        """⚠️ 回归：「满30元享8.5折」同样含「0元」。"""
        c = Coupon({"id": "R2", "name": "满30元享8.5折"})
        self.assertEqual(c.kind, opt.KIND_PERCENT)

    def test_scope_reverse_match(self):
        """回归：券说「麦旋风买一送一」，商品叫「经典麦旋风」，应匹配上。

        纯单向匹配（关键词 ⊆ 商品名）时这类券永远匹配不上。
        """
        c = Coupon({"id": "S1", "name": "人气麦旋风买一送一"})
        self.assertTrue(opt._matches_scope(c, {"name": "经典麦旋风"}))
        self.assertFalse(opt._matches_scope(c, {"name": "可乐"}))

    def test_unknown_coupon_stays_unknown(self):
        """「麦旋风任选」不含任何金额信息，诚实标记为未识别，不瞎猜。"""
        c = Coupon({"id": "U1", "name": "麦旋风任选"})
        self.assertEqual(c.kind, opt.KIND_UNKNOWN)
        self.assertFalse(c.is_usable())


if __name__ == "__main__":
    unittest.main(verbosity=2)
