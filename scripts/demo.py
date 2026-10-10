#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦麦省钱军师 - 演示脚本

用一份示例菜单跑三个典型场景，直观展示"军师"和普通"优惠券列表"的区别：

场景一：已经够门槛，直接用券最省
场景二：差一点够门槛，凑单能省钱 —— 给出凑单方案
场景三：差得太多，凑单反而更贵 —— 明确劝退，别为了用券多花钱

用法：
    python scripts/demo.py          # 跑三个场景
    python scripts/demo.py --bench  # 跑性能基准

注意：示例菜单中的价格仅用于演示算法，与门店实际价格无关。
真实使用时菜单与优惠券数据由麦当劳 MCP 实时提供。
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from optimizer import solve  # noqa: E402

# --------------------------------------------------------------------------
# 示例菜单（价格仅供演示）
# --------------------------------------------------------------------------

MENU = [
    {"code": "B001", "name": "巨无霸", "price": 23.5, "category": "汉堡"},
    {"code": "B002", "name": "麦辣鸡腿堡", "price": 21.5, "category": "汉堡"},
    {"code": "B003", "name": "板烧鸡腿堡", "price": 22.0, "category": "汉堡"},
    {"code": "B004", "name": "麦香鱼", "price": 17.0, "category": "汉堡"},
    {"code": "B005", "name": "双层吉士汉堡", "price": 17.5, "category": "汉堡"},
    {"code": "B006", "name": "吉士汉堡", "price": 12.0, "category": "汉堡"},
    {"code": "S001", "name": "麦乐鸡（5块）", "price": 12.5, "category": "小食"},
    {"code": "S002", "name": "麦乐鸡（10块）", "price": 22.5, "category": "小食"},
    {"code": "S003", "name": "中薯条", "price": 11.0, "category": "小食"},
    {"code": "S004", "name": "大薯条", "price": 13.5, "category": "小食"},
    {"code": "S005", "name": "小薯条", "price": 8.0, "category": "小食"},
    {"code": "S006", "name": "麦辣鸡翅（2块）", "price": 13.0, "category": "小食"},
    {"code": "S007", "name": "麦麦脆汁鸡（1块）", "price": 14.0, "category": "小食"},
    {"code": "D001", "name": "可乐（中）", "price": 9.5, "category": "饮品"},
    {"code": "D002", "name": "可乐（大）", "price": 10.5, "category": "饮品"},
    {"code": "D003", "name": "雪碧（中）", "price": 9.5, "category": "饮品"},
    {"code": "D004", "name": "橙汁", "price": 12.0, "category": "饮品"},
    {"code": "D005", "name": "热咖啡", "price": 10.0, "category": "饮品"},
    {"code": "D006", "name": "纯牛奶", "price": 8.5, "category": "饮品"},
    {"code": "T001", "name": "圆筒冰淇淋", "price": 6.0, "category": "甜品"},
    {"code": "T002", "name": "麦旋风（奥利奥）", "price": 11.5, "category": "甜品"},
    {"code": "T003", "name": "香芋派", "price": 7.5, "category": "甜品"},
    {"code": "T004", "name": "苹果派", "price": 7.5, "category": "甜品"},
    {"code": "T005", "name": "新地冰淇淋", "price": 8.5, "category": "甜品"},
    {"code": "M001", "name": "巨无霸套餐（中）", "price": 39.0, "category": "套餐"},
    {"code": "M002", "name": "麦辣鸡腿堡套餐（中）", "price": 36.0, "category": "套餐"},
    {"code": "M003", "name": "麦乐鸡分享盒", "price": 32.0, "category": "套餐"},
    {"code": "M004", "name": "双人餐A", "price": 62.0, "category": "套餐"},
]

SCENARIOS = [
    {
        "title": "场景一 · 已经够门槛，直接用券",
        "wanted": ["巨无霸", "可乐（中）"],
        "coupons": [
            {"id": "C001", "name": "满30元减6元", "source": "store"},
            {"id": "C002", "name": "立减2元", "source": "my"},
        ],
    },
    {
        "title": "场景二 · 差一点够门槛，凑单划算",
        "wanted": ["麦乐鸡（5块）", "中薯条", "可乐（中）"],
        "coupons": [
            {"id": "C003", "name": "满35元减10元", "source": "store"},
        ],
    },
    {
        "title": "场景三 · 差太多，凑单不划算（应当劝退）",
        "wanted": ["圆筒冰淇淋"],
        "coupons": [
            {"id": "C004", "name": "满30元减5元", "source": "store"},
        ],
    },
]


def render(result):
    """把结果渲染成人类可读的文本。"""
    lines = []
    lines.append("  想吃：%s（原价 %s 元）" % (
        "、".join(result["wanted"]["matched"]) or "（无）",
        result["wanted"]["base_total"],
    ))
    if result["wanted"]["missed"]:
        lines.append("  未在菜单中找到：%s" % "、".join(result["wanted"]["missed"]))
    lines.append("  %s" % result["summary"])
    for plan in result["plans"][:3]:
        lines.append("")
        lines.append("  #%d 用券「%s」（%s）" % (
            plan["rank"], plan["coupon"]["name"], plan["coupon"]["rule"]))
        for item in plan["items"]:
            tag = "想买" if item["role"] == "wanted" else "凑单"
            qty = "×%d" % item["qty"] if item["qty"] > 1 else ""
            lines.append("      [%s] %s%s  %s 元" % (tag, item["name"], qty, item["price"]))
        lines.append("      合计 %s 元 － 优惠 %s 元 = 实付 %s 元（净省 %s 元）" % (
            plan["original_total"], plan["discount"], plan["payable"], plan["net_saving"]))
        lines.append("      提示：%s" % plan["advice"])
    return "\n".join(lines)


def run_scenarios():
    for scenario in SCENARIOS:
        payload = {
            "intent": {"wanted": scenario["wanted"]},
            "menu": MENU,
            "coupons": scenario["coupons"],
            "options": {"top_n": 3},
        }
        result = solve(payload)
        print("=" * 72)
        print(scenario["title"])
        print("=" * 72)
        print(render(result))
        print()


def run_bench():
    """性能基准：模拟一家门店的完整菜单 + 多张券。

    刻意让基础篮子卡在某个满减门槛之下，从而真正压到凑单 DP 这条最重的路径。
    """
    menu = []
    for i in range(300):
        menu.append({
            "code": "X%03d" % i,
            "name": "测试商品%03d" % i,
            "price": round(3.0 + (i % 47) * 0.7, 2),
            "category": "测试",
        })
    coupons = []
    for i in range(12):
        coupons.append({
            "id": "K%02d" % i,
            "name": "满%d元减%d元" % (20 + i * 3, 3 + i),
            "source": "store",
        })
    payload = {
        "intent": {"wanted": ["测试商品020", "测试商品021"]},
        "menu": menu,
        "coupons": coupons,
        "options": {"top_n": 5},
    }
    # 预热
    solve(payload)
    start = time.perf_counter()
    rounds = 20
    for _ in range(rounds):
        result = solve(payload)
    elapsed = (time.perf_counter() - start) / rounds
    print("规模：菜单 %d 项 / 优惠券 %d 张" % (len(menu), len(coupons)))
    print("单次求解平均耗时：%.1f ms" % (elapsed * 1000))
    print("最优方案：%s" % result["summary"])


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if "--bench" in sys.argv:
        run_bench()
    else:
        run_scenarios()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
