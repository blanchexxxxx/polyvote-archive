"""市场选择漏斗的测试。

这套漏斗是 24h 实测(98 轮全 SKIP)之后的改版,每个用例对着当时的一个病:
  - 冷却: 71/98 轮重复分析同两个盘
  - 盘口体检: St. Louis 陈旧中间价 0.815 vs 真实 bid 0.999,假 edge -0.79
  - 巨量盘掐除: 百万级世界杯盘定价太尖,分析必弃权

  .venv/bin/python tests/test_funnel.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from polyvote.market import Market, book_prefilter, filter_markets, select_candidates

failures = []


def check(name, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{name}: 得到 {got!r}, 期望 {want!r}")
    print(f"  {'ok ' if ok else 'ERR'} {name:<38} → {got!r} (期望 {want!r})")


def mk(mid, q="q", px=0.5, vol=50_000, days=7, liq=20_000):
    return Market(
        id=mid, question=q, description="", slug=mid, outcomes=["Yes", "No"],
        prices=[px, 1 - px], token_ids=[mid + "-y", mid + "-n"],
        volume_24h=vol, liquidity=liq,
        end_date=datetime.now(timezone.utc) + timedelta(days=days),
    )


F = {
    "price_min": 0.10, "price_max": 0.90,
    "min_volume_24h_usd": 5_000, "max_volume_24h_usd": 400_000,
    "min_liquidity_usd": 5_000,
    "min_days_to_resolution": 1, "max_days_to_resolution": 45,
    "cooldown_hours": 8, "reprice_threshold": 0.04,
    "deprioritize_keywords": ["vs."],
    "book_max_spread": 0.06, "book_max_ask": 0.95,
}

now = datetime.now(timezone.utc)

print("=== 静态筛: 巨量盘被掐掉 ===")
kept = filter_markets([mk("big", vol=3_000_000), mk("mid", vol=50_000)], F)
check("百万级盘被筛掉", [m.id for m in kept], ["mid"])

print()
print("=== 冷却 ===")
seen = {"a": (now - timedelta(hours=2), 0.50)}
cands, cooled = select_candidates([mk("a", px=0.51)], seen, F)
check("2h前分析过+价格没动 → 冷却", (len(cands), cooled), (0, 1))

cands, cooled = select_candidates([mk("a", px=0.56)], seen, F)
check("价格挪 6 分 → 提前解锁", (len(cands), cooled), (1, 0))

seen_old = {"a": (now - timedelta(hours=9), 0.50)}
cands, cooled = select_candidates([mk("a", px=0.51)], seen_old, F)
check("过了 8h 冷却期 → 解锁", (len(cands), cooled), (1, 0))

cands, cooled = select_candidates([mk("a", px=0.51)], seen, F, open_ids=frozenset(["a"]))
check("持仓盘永不冷却", (len(cands), cooled), (1, 0))

print()
print("=== 打分排序 ===")
seen2 = {"old_flat": (now - timedelta(hours=9), 0.50)}
ms = [
    mk("old_flat", px=0.50, vol=50_000),          # 老盘没动静: 0+1+1 = 2
    mk("fresh", px=0.50, vol=50_000),             # 新盘: 2+1+1 = 4
    mk("vs_game", q="A vs. B game", vol=50_000),  # 新但单场: 2+1+1-2 = 2
]
cands, _ = select_candidates(ms, seen2, F)
check("新的非单场盘排最前", cands[0].id, "fresh")
check("单场对决被降权", cands[-1].id in ("vs_game", "old_flat"), True)

print()
print("=== 盘口体检 (堵陈旧中间价陷阱) ===")
BOOKS = {
    "healthy-y":   {"bids": [{"price": "0.48"}], "asks": [{"price": "0.52"}]},
    "converged-y": {"bids": [{"price": "0.999"}], "asks": []},        # St. Louis 案发现场
    "wide-y":      {"bids": [{"price": "0.30"}], "asks": [{"price": "0.60"}]},
    "oneside-y":   {"bids": [], "asks": [{"price": "0.52"}]},
}
fake_fetch = lambda host, tid: BOOKS.get(tid)  # noqa: E731

cands = [mk("converged", px=0.815), mk("wide"), mk("oneside"), mk("healthy")]
kept, drops = book_prefilter(cands, F, need=3, clob_host="x", fetch=fake_fetch)
check("只有健康盘通过", [m.id for m in kept], ["healthy"])
check("收敛盘被记录", any("已收敛" in k for k in drops), True)
check("宽点差被记录", any("点差" in k for k in drops), True)
check("单边盘被记录", any("单边" in k for k in drops), True)

kept, _ = book_prefilter([mk(f"h{i}") for i in range(9)], F, need=2,
                         clob_host="x",
                         fetch=lambda h, t: BOOKS["healthy-y"])
check("凑够 need 就停(不浪费调用)", len(kept), 2)

print()
if failures:
    print(f"!! {len(failures)} 个用例挂了:")
    for f in failures:
        print("   -", f)
    sys.exit(1)
print("全部通过")
