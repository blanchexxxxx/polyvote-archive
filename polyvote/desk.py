"""交易台的机械层:算术扣扳机 + Kelly 定仓。

出处:操盘手研究(2026-07-19)扫了 7 个能跑的开源交易 bot
(TradingAgents / CloddsBot / valory trader / Willmin kalshi / guberm polymarket
/ OctagonAI / PrimoAgent),元发现只有一句:

  没有一个让 LLM 决定"要不要交易"。LLM 产出 p 和 confidence;
  扣扳机的是算术(edge ≥ min_edge 就下),疑虑由仓位吸收,不由弃权吸收。

我们此前 102 轮全 SKIP 正是反面教材:决策者被问"该不该交易",而对 LLM
谨慎永远是安全答案。改版分工:

  代码(这里)   —— 触发与定仓。edge 按真实盘口可成交价算,不用中间价。
  LLM(brain)   —— 只留否决权:引用具体证据才能拦,"市场大概知道些什么"不算。
  风控(risk)   —— 最终闸门不变。

防坑双保险(St. Louis 假 edge 案,机械规则会直接开枪的那种):
  1. book_prefilter 在分析前就丢掉已收敛/陈旧盘口 —— 假 edge 进不了这里
  2. LLM 否决层拦研究缺陷("三票同源于一份可疑简报")
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class Proposal:
    """机械层的下单提案。产生它的每一步都写进 audit,可回放可调参。"""
    side: str            # YES | NO
    exec_price: float    # 真实可成交价(买YES=卖一;买NO=1-买一)
    edge: float          # p_mean 相对 exec_price 的优势
    p_mean: float        # 有效票的平均估计概率
    mean_conf: float     # 计票票的平均信心 (0-10)
    kelly_f: float       # 全 Kelly 比例
    size_usd: float      # 最终建议仓位(¼Kelly × 信心,已 clamp)
    audit: str           # 一行算术痕迹


def propose(
    tally: dict[str, Any],
    bid: float | None,
    ask: float | None,
    risk_cfg: Any,
) -> tuple[Proposal | None, str]:
    """算术扣扳机。返回 (提案, 机器可查的跳过理由)。

    跳过理由必须带数字("edge +0.021 < min_edge 0.06"),绝不许是感觉 ——
    这样跑一周后 grep 一下就知道哪个参数拦了多少单,调参只动一个数。
    (valory 的 _no_trade(reason) 模式)
    """
    p = tally.get("mean_fair_price")
    if p is None:
        return None, "no valid votes (all failed)"

    agree = tally.get("agreement", 0)
    min_agree = risk_cfg.min_vote_agreement
    if agree < min_agree:
        return None, f"agreement {agree} < {min_agree}"

    lead = tally.get("lead")
    if lead not in ("YES", "NO"):
        return None, f"lead is {lead}"

    spread_disp = tally.get("fair_price_spread", 0.0)
    if spread_disp > risk_cfg.max_fair_price_spread:
        return None, (
            f"analyst spread {spread_disp:.3f} > {risk_cfg.max_fair_price_spread}"
        )

    if bid is None or ask is None:
        return None, "book missing a side (should have been prefiltered)"

    # edge 用可成交价:买 YES 付卖一;买 NO 的成本 ≈ 1 - YES买一
    if lead == "YES":
        exec_price = ask
        edge = p - exec_price
    else:
        exec_price = 1.0 - bid
        edge = bid - p  # = (1-p) - (1-bid)

    if edge < risk_cfg.min_edge:
        return None, f"edge {edge:+.3f} < min_edge {risk_cfg.min_edge} ({lead} @ {exec_price:.3f} vs p={p:.3f})"

    if not (risk_cfg.min_price_for_buy <= exec_price <= risk_cfg.max_price_for_buy):
        return None, f"exec price {exec_price:.3f} outside [{risk_cfg.min_price_for_buy}, {risk_cfg.max_price_for_buy}]"

    # Kelly。买入二元合约价 a、真实概率 p_win 时: f* = (p_win - a) / (1 - a)
    p_win = p if lead == "YES" else 1.0 - p
    kelly_f = (p_win - exec_price) / (1.0 - exec_price)
    if kelly_f <= 0:
        return None, f"kelly {kelly_f:.4f} <= 0"

    # ¼Kelly × 信心 —— 疑虑缩仓位而不是弃权(CloddsBot: kelly = edge·conf·0.25;
    # valory 的 threshold ramp 同理:信心低 → 仓位小,不是不下)
    mean_conf = tally.get("mean_confidence") or 5.0
    bankroll = getattr(risk_cfg, "paper_bankroll_usd", 100.0)
    frac = getattr(risk_cfg, "kelly_fraction", 0.25)
    size = bankroll * kelly_f * frac * (mean_conf / 10.0)
    size = min(size, risk_cfg.max_notional_per_trade_usd)

    if size < 1.0:
        return None, f"size ${size:.2f} < $1 floor (kelly {kelly_f:.3f} × conf {mean_conf:.0f}/10)"

    audit = (
        f"{lead} @ {exec_price:.3f} | p_mean={p:.3f} edge={edge:+.3f} "
        f"| f*={kelly_f:.3f} × {frac} × conf {mean_conf:.0f}/10 "
        f"× bankroll ${bankroll:.0f} → ${size:.2f}"
    )
    return (
        Proposal(
            side=lead, exec_price=round(exec_price, 4), edge=round(edge, 4),
            p_mean=round(p, 4), mean_conf=round(mean_conf, 2),
            kelly_f=round(kelly_f, 4), size_usd=round(size, 2), audit=audit,
        ),
        "",
    )
