"""结算逻辑测试。

这块是唯一能回答"这套投票到底有没有 edge"的地方,但它平时跑不到 ——
必须等真实市场解决。所以用合成数据在这里测死:解析和算术错了会静默地
把胜负记反,而你要等好几天才可能发现。

  .venv/bin/python tests/test_settle.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from polyvote.settle import _winning_side
from polyvote.store import Store

failures = []


def check(name, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{name}: 得到 {got!r}, 期望 {want!r}")
    print(f"  {'ok ' if ok else 'ERR'} {name:<26} → {got!r:<7} (期望 {want!r})")


print("=== _winning_side 解析 ===")
# Gamma 把 outcomePrices 编码成 JSON 字符串,但不同 endpoint 有时给真数组,两种都要接
check("未收盘", _winning_side({"closed": False, "outcomePrices": '["1","0"]'}), None)
check("YES 赢 (JSON字符串)", _winning_side({"closed": True, "outcomePrices": '["1","0"]'}), "YES")
check("NO 赢 (JSON字符串)", _winning_side({"closed": True, "outcomePrices": '["0","1"]'}), "NO")
check("YES 赢 (真数组)", _winning_side({"closed": True, "outcomePrices": ["1", "0"]}), "YES")
# 收盘了但价格还没定死 → 别急着记账,等最终值
check("收盘但价 0.5/0.5", _winning_side({"closed": True, "outcomePrices": '["0.5","0.5"]'}), None)
check("收盘但价 0.97 未最终", _winning_side({"closed": True, "outcomePrices": '["0.97","0.03"]'}), None)
check("价 0.995 算 YES", _winning_side({"closed": True, "outcomePrices": '["0.995","0.005"]'}), "YES")
check("脏数据不崩", _winning_side({"closed": True, "outcomePrices": "garbage"}), None)
check("缺字段不崩", _winning_side({"closed": True}), None)
check("空字典不崩", _winning_side({}), None)

print()
print("=== 结算盈亏算术 ===")
db = tempfile.mktemp(suffix=".db")
s = Store(db)
try:
    # Polymarket 份额是全有或全无:赢的每份 $1,输的归零。
    # 买 YES $4.00 拿到 10 份 (均价 0.40)
    r = s.log_settlement(
        market_id="m1", question="赢的那笔", winning_side="YES", our_side="YES",
        cost_usd=4.0, shares=10.0, dry_run=True,
    )
    print(f"  赢: 本金 $4.00 / 10份 → 回收 ${r['payout_usd']:.2f} 盈亏 {r['pnl_usd']:+.2f} (应 +6.00)")
    check("赢的盈亏", round(r["pnl_usd"], 6), 6.0)
    check("赢的回收", round(r["payout_usd"], 6), 10.0)

    r = s.log_settlement(
        market_id="m2", question="输的那笔", winning_side="NO", our_side="YES",
        cost_usd=4.0, shares=10.0, dry_run=True,
    )
    print(f"  输: 本金 $4.00 / 10份 → 回收 ${r['payout_usd']:.2f} 盈亏 {r['pnl_usd']:+.2f} (应 -4.00)")
    check("输的盈亏", round(r["pnl_usd"], 6), -4.0)
    check("输的回收", round(r["payout_usd"], 6), 0.0)

    # 买 NO 且 NO 赢 —— 方向映射别搞反
    r = s.log_settlement(
        market_id="m3", question="买NO赢了", winning_side="NO", our_side="NO",
        cost_usd=3.0, shares=5.0, dry_run=True,
    )
    check("买NO且NO赢 = 赢", r["won"], True)

    print()
    p = s.pnl_summary(True)
    print(f"  汇总: {p['settled']}笔 {p['wins']}胜{p['losses']}负 "
          f"胜率{p['win_rate']:.0%} 盈亏{p['pnl_usd']:+.2f} ROI {p['roi']:.1%}")
    check("已结算笔数", p["settled"], 3)
    check("胜场", p["wins"], 2)
    check("负场", p["losses"], 1)
    check("总盈亏", round(p["pnl_usd"], 6), 4.0)  # +6 -4 +2

    # dry_run 和实盘的账必须分开,不能混
    check("实盘账是空的", s.pnl_summary(False)["settled"], 0)

    # 同一市场重复结算不能重复计账
    s.log_settlement(
        market_id="m1", question="赢的那笔", winning_side="YES", our_side="YES",
        cost_usd=4.0, shares=10.0, dry_run=True,
    )
    check("重复结算不翻倍", s.pnl_summary(True)["settled"], 3)
finally:
    try:
        os.unlink(db)
    except OSError:
        pass

print()
if failures:
    print(f"!! {len(failures)} 个用例挂了:")
    for f in failures:
        print("   -", f)
    sys.exit(1)
print("全部通过")
