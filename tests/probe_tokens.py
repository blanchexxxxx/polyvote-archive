"""实测 token 消耗 + 缓存命中。

订阅制下规划配额靠的是 token,不是 cost_usd 折算。这个脚本回答两件事:

1. 一轮真实消耗多少 token(分 input / cache write / cache read / output)
2. **一次性调用之间缓存命不命中** —— 每次 `claude -p` 都是全新 session,
   写进去的缓存下次能不能读到? 如果读不到,那每次的 cache write 就是纯浪费,
   而且"缓存保活"这个思路根本不成立。

  .venv/bin/python tests/probe_tokens.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from polyvote import llm
from polyvote.brain import VOTER_SYSTEM, VOTE_SCHEMA

MODEL = os.environ.get("PROBE_MODEL", "claude-fable-5")
GAP_S = int(os.environ.get("PROBE_GAP", "0"))

PROMPT = """问题: Will it rain in Tokyo on 2026-07-20?

结算规则:
Resolves YES if measurable precipitation (>=0.1mm) is recorded at the Tokyo
(Otemachi) JMA station on 2026-07-20 JST.

── 研究简报 ──
简报: 未能获取该日期的预报数据。
查到的事实 (窗口内 0 条 / 背景 1 条):
  [背景]   东京7月平均降水日数约 12 天/月  (JMA climatology, historical)

⚠ 窗口内证据 0 条 —— 以上全是背景资料。

── 背景 ──
  3.0 天后结算。今天是 2026-07-17。
  该市场 24h 成交量 $50,000,流动性 $10,000

请给出 YES 发生的概率。你看不到市场报价,这是刻意的。"""


def one(label: str) -> llm.Usage:
    t0 = time.time()
    r = llm.call_claude(
        PROMPT,
        model=MODEL,
        schema=VOTE_SCHEMA,
        system=VOTER_SYSTEM,
        effort="medium",
        tools=None,
        max_turns=10,
    )
    u = r.usage
    dt = time.time() - t0
    print(
        f"  {label:<12} in={u.input_tokens:>6,}  "
        f"cache_w={u.cache_write:>6,}  cache_r={u.cache_read:>7,}  "
        f"out={u.output_tokens:>5,}  |  合计 {u.total:>7,}  "
        f"等效 {u.billable_equiv:>8,.0f}  ${r.cost_usd or 0:.3f}  {dt:.0f}s"
    )
    if r.rate_limit:
        print(f"               限流信息: {r.rate_limit}")
    return u


print(f"模型 {MODEL} · 同一个 prompt 连打三次,看缓存命不命中")
print("(每次 claude -p 都是全新 session —— 缓存是服务端按 prompt 前缀命中的)")
print()

us = []
for i in range(3):
    if i and GAP_S:
        print(f"  …等 {GAP_S}s(测 TTL)")
        time.sleep(GAP_S)
    us.append(one(f"第 {i+1} 次"))

print()
print("=" * 78)
first, rest = us[0], us[1:]
hit = [u for u in rest if u.cache_read > 0]

if hit:
    saved = (first.billable_equiv - sum(u.billable_equiv for u in hit) / len(hit))
    print(f"✅ 缓存命中。首次等效 {first.billable_equiv:,.0f} → 后续均值 "
          f"{sum(u.billable_equiv for u in hit)/len(hit):,.0f} "
          f"(省 {saved/first.billable_equiv:.0%})")
    print()
    print("   意味着'缓存保活'成立:只要相邻两轮间隔 < TTL,系统提示那段就是")
    print("   cache read(0.1×)而不是 cache write(1.25×) —— 差 12 倍。")
    print("   → 缩短轮询间隔反而可能比拉长更省。")
else:
    print(f"❌ 缓存没命中。三次的 cache_read 都是 0,cache_write 每次都在付。")
    print()
    print(f"   每次白写 {first.cache_write:,} tokens 的缓存,从来没人读。")
    print("   → '缓存保活'这条路不成立,省不了。要降成本只能砍 prompt 或砍频率。")

print()
print(f"单票实测: 合计 {first.total:,} tokens (等效 {first.billable_equiv:,.0f})")
