"""缓存命中率仪表的测试。

公式和分类阈值抄自 mory 的 RailCacheWidget:
  ratio = Σcache_read / Σ(cache_read + cache_write + new)   —— out 不进分母
  单次: cache_read==0 → miss; cache_read > cache_write*2 → hit; 否则 warm

配色**没有**抄 —— mory 那套 hit #9bd3a0 / miss #e6a2a2 在 CVD 模拟下
ΔE 只有 3.5(deutan),红绿色盲看这两个是一个颜色,而这恰是最核心的两个状态。
观测台改用 status 色 + 文字标签(色盲靠文字读,颜色只做冗余强化)。

  .venv/bin/python tests/test_cache_meter.py
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
import dashboard  # noqa: E402
from polyvote import llm  # noqa: E402
from polyvote.store import Store  # noqa: E402

failures = []


def check(name, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{name}: 得到 {got!r}, 期望 {want!r}")
    print(f"  {'ok ' if ok else 'ERR'} {name:<34} → {got!r:<9} (期望 {want!r})")


db = tempfile.mktemp(suffix=".db")
s = Store(db)
try:
    print("=== 空库不崩 ===")
    cs = s.cache_stats()
    check("空库 calls", cs["calls"], 0)
    check("空库 ratio (不能除零)", cs["ratio"], 0.0)

    print()
    print("=== 命中率算术 ===")
    # 真实数据: 之前实测的一次 fable 投票
    s.log_call(
        role="vote", actor="fable", engine="claude", model="claude-fable-5",
        usage=llm.Usage(input_tokens=2490, cache_write=18389, cache_read=15634,
                        output_tokens=300),
        cost_usd=0.423,
    )
    cs = s.cache_stats()
    # 15634 / (15634 + 18389 + 2490) = 15634 / 36513 = 0.428175…
    check("ratio", round(cs["ratio"], 4), 0.4282)
    check("out 不进分母", cs["out"], 300)

    # 冷启: 全是 cache_write,没有 read
    s.log_call(
        role="research", actor="researcher", engine="claude", model="claude-haiku-4-5",
        usage=llm.Usage(input_tokens=1000, cache_write=15000, cache_read=0,
                        output_tokens=500),
    )
    cs = s.cache_stats()
    # 15634 / (15634+18389+2490 + 0+15000+1000) = 15634/52513
    check("加一次冷启后 ratio 下降", round(cs["ratio"], 4), 0.2977)

    print()
    print("=== 单次分类 (dashboard 侧) ===")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    d = dashboard.cache_stats(conn)
    conn.close()
    by_actor = {r["actor"]: r for r in d["recent"]}
    # fable: read 15634 > write 18389 * 2? → 15634 > 36778? 否 → warm
    check("读<写×2 → warm", by_actor["fable"]["cls"], "warm")
    # researcher: read == 0 → miss
    check("读==0 → miss", by_actor["researcher"]["cls"], "miss")

    # 真正的热命中: read 远大于 write
    s.log_call(
        role="vote", actor="opus", engine="claude", model="claude-opus-4-8",
        usage=llm.Usage(input_tokens=200, cache_write=500, cache_read=18000,
                        output_tokens=400),
    )
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    d = dashboard.cache_stats(conn)
    conn.close()
    hot = [r for r in d["recent"] if r["actor"] == "opus"][0]
    check("读>写×2 → hit", hot["cls"], "hit")

    print()
    print("=== gap 是相对更旧那次算的 (TTL 探针) ===")
    # recent 是 DESC 排的,gap 应该指向更旧的一次
    gaps = [(r["actor"], r["gap_s"]) for r in d["recent"]]
    print(f"  {gaps}")
    check("最旧的一条没有 gap", d["recent"][-1]["gap_s"], None)
    check("其余都有 gap", all(r["gap_s"] is not None for r in d["recent"][:-1]), True)

    print()
    print("=== round_id 回填 ===")
    check("回填前 round_id 为空", s.cache_stats()["calls"], 3)
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM llm_calls WHERE round_id IS NULL").fetchone()[0]
    conn.close()
    check("3 条都还没归属", n, 3)
    s.attach_calls_to_round(42)
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM llm_calls WHERE round_id = 42").fetchone()[0]
    conn.close()
    check("回填后都挂到 round 42", n, 3)
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
