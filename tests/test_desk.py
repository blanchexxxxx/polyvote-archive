"""desk 机械层测试:算术扣扳机 + Kelly 定仓。

嫁接自操盘手研究(2026-07-19)。核心不变量:
  - 触发是算术,不是 LLM 心情
  - 跳过必须带数字理由(可 grep 可调参)
  - 疑虑缩仓位(×conf/10),不弃权
  - edge 按可成交价算,不用中间价

  .venv/bin/python tests/test_desk.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from polyvote.desk import propose

failures = []


def check(name, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{name}: 得到 {got!r}, 期望 {want!r}")
    print(f"  {'ok ' if ok else 'ERR'} {name:<40} → {got!r} (期望 {want!r})")


class R:
    min_edge = 0.06
    min_vote_agreement = 2
    max_fair_price_spread = 0.25
    min_price_for_buy = 0.05
    max_price_for_buy = 0.90
    max_notional_per_trade_usd = 5.0
    paper_bankroll_usd = 100.0
    kelly_fraction = 0.25


def T(p=0.55, lead="YES", agree=3, spread=0.03, conf=6.0):
    return {"mean_fair_price": p, "lead": lead, "agreement": agree,
            "fair_price_spread": spread, "mean_confidence": conf,
            "yes": agree if lead == "YES" else 0, "no": agree if lead == "NO" else 0,
            "abstain": 0}


print("=== 触发条件 ===")
# p=0.55, ask=0.47 → edge=0.08 ≥ 0.06 → 触发
prop, why = propose(T(p=0.55), bid=0.45, ask=0.47, risk_cfg=R)
check("边际够 → 触发", prop is not None, True)
check("方向", prop.side, "YES")
check("用可成交价(卖一)不用中间价", prop.exec_price, 0.47)
check("edge = p - ask", prop.edge, 0.08)

# edge 不够 → 带数字的理由
prop, why = propose(T(p=0.50), bid=0.45, ask=0.47, risk_cfg=R)
check("edge 不够 → 不触发", prop, None)
check("理由带数字", "edge +0.030 < min_edge 0.06" in why, True)

# NO 方向: p=0.30, bid=0.42 → 买NO成本 1-0.42=0.58, edge = bid - p = 0.12
prop, why = propose(T(p=0.30, lead="NO"), bid=0.42, ask=0.44, risk_cfg=R)
check("NO 方向触发", prop is not None, True)
check("NO 成本 = 1 - bid", prop.exec_price, 0.58)
check("NO edge = bid - p", prop.edge, 0.12)

# 同向票不足
prop, why = propose(T(agree=1), bid=0.45, ask=0.47, risk_cfg=R)
check("同向票不足 → 理由", "agreement 1 < 2" in why, True)

# 分歧过大
prop, why = propose(T(spread=0.30), bid=0.45, ask=0.47, risk_cfg=R)
check("分歧过大 → 理由", "spread" in why, True)

# 盘口缺边
prop, why = propose(T(), bid=None, ask=0.47, risk_cfg=R)
check("盘口缺边 → 不触发", prop, None)

print()
print("=== Kelly 定仓 ===")
# p=0.55 ask=0.47: f* = (0.55-0.47)/(1-0.47) = 0.08/0.53 = 0.1509
# size = 100 × 0.1509 × 0.25 × 6/10 = 2.264
prop, _ = propose(T(p=0.55, conf=6.0), bid=0.45, ask=0.47, risk_cfg=R)
check("f* 算术", round(prop.kelly_f, 4), round(0.08 / 0.53, 4))
check("size = bankroll*f*Kelly分数*conf/10", prop.size_usd, round(100 * (0.08/0.53) * 0.25 * 0.6, 2))

# 信心高 → 仓位大(疑虑缩仓位的反面)
p_low, _ = propose(T(p=0.55, conf=4.0), bid=0.45, ask=0.47, risk_cfg=R)
p_high, _ = propose(T(p=0.55, conf=9.0), bid=0.45, ask=0.47, risk_cfg=R)
check("信心 9 仓位 > 信心 4", p_high.size_usd > p_low.size_usd, True)

# 巨 edge 也被单笔上限压住
prop, _ = propose(T(p=0.80, conf=10.0), bid=0.45, ask=0.47, risk_cfg=R)
check("单笔上限 $5 压住", prop.size_usd <= 5.0, True)

# 太小的仓位 → 不下(地板 $1)
prop, why = propose(T(p=0.532, conf=1.0), bid=0.45, ask=0.47, risk_cfg=R)
if prop is None:
    check("低信心薄edge → 地板拦下", "floor" in why or "edge" in why, True)
else:
    check("低信心薄edge → 仓位≥$1", prop.size_usd >= 1.0, True)

print()
if failures:
    print(f"!! {len(failures)} 个用例挂了:")
    for f in failures:
        print("   -", f)
    sys.exit(1)
print("全部通过")
