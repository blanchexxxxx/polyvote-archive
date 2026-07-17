"""风控闸门。

这一层的存在意义:LLM 说什么都不算数,这里说了算。
决策者的 prompt 里写了硬约束,但 prompt 是建议,这里是强制。
一个跑飞的模型、一个 prompt 注入、一次 schema 漂移,都要在这里被挡住。

规则:任何一条不通过 → 整单拒绝,不做"砍到上限后放行"这种事。
砍到上限听着温和,实际是把一个明显失控的决策变成一个小额失控的决策 ——
真正该做的是拒绝并记录,让人看到模型在乱来。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .brain import Decision
from .config import Config
from .market import Market

log = logging.getLogger(__name__)


@dataclass
class Verdict:
    allowed: bool
    reasons: list[str]

    def __bool__(self) -> bool:
        return self.allowed


def check(
    cfg: Config,
    market: Market,
    decision: Decision,
    tally_result: dict[str, Any],
    *,
    spent_today_usd: float,
    position_in_market_usd: float,
    open_positions: int,
) -> Verdict:
    r = cfg.risk
    bad: list[str] = []

    if decision.action == "SKIP":
        return Verdict(False, ["决策为 SKIP"])

    if decision.action == "SELL":
        if position_in_market_usd <= 0:
            bad.append("要求 SELL 但该市场没有持仓")
        return Verdict(not bad, bad or ["SELL 通过"])

    # ── 以下是 BUY ──
    if decision.confidence < r.min_decider_confidence:
        bad.append(
            f"决策者信心 {decision.confidence} < 下限 {r.min_decider_confidence}"
        )

    if tally_result["agreement"] < r.min_vote_agreement:
        bad.append(
            f"同向票 {tally_result['agreement']} < 下限 {r.min_vote_agreement}"
        )

    if tally_result["lead"] == "TIE":
        bad.append("投票平局")
    elif tally_result["lead"] != decision.side:
        bad.append(
            f"决策方向 {decision.side} 与多数票 {tally_result['lead']} 相反"
        )

    spread = tally_result.get("fair_price_spread", 0.0)
    if spread > r.max_fair_price_spread:
        bad.append(
            f"分析师估值分歧 {spread:.3f} > 上限 {r.max_fair_price_spread} —— "
            "没人真的知道答案,均值是假的精确"
        )

    if tally_result.get("failed"):
        log.warning(
            "  注意: 有 %d 个投票人调用失败,本轮共识建立在剩下的票上",
            tally_result["failed"],
        )

    if decision.size_usd <= 0:
        bad.append(f"下单金额 ${decision.size_usd} 非正数")
    elif decision.size_usd > r.max_notional_per_trade_usd:
        bad.append(
            f"下单金额 ${decision.size_usd:.2f} > 单笔上限 "
            f"${r.max_notional_per_trade_usd}"
        )

    if spent_today_usd + decision.size_usd > r.max_notional_per_day_usd:
        bad.append(
            f"今日已用 ${spent_today_usd:.2f} + 本单 ${decision.size_usd:.2f} "
            f"> 日上限 ${r.max_notional_per_day_usd}"
        )

    if position_in_market_usd + decision.size_usd > r.max_position_per_market_usd:
        bad.append(
            f"该市场现有仓位 ${position_in_market_usd:.2f} + 本单 "
            f"${decision.size_usd:.2f} > 单市场上限 ${r.max_position_per_market_usd}"
        )

    if position_in_market_usd == 0 and open_positions >= r.max_open_positions:
        bad.append(f"未平仓市场已达 {open_positions} 个,到上限 {r.max_open_positions}")

    if not (r.min_price_for_buy <= decision.limit_price <= r.max_price_for_buy):
        bad.append(
            f"限价 {decision.limit_price} 不在 "
            f"[{r.min_price_for_buy}, {r.max_price_for_buy}] 内"
        )

    # 限价离盘口太远就是白挂,还占着仓位额度
    market_price = market.yes_price if decision.side == "YES" else market.no_price
    if decision.limit_price > market_price * 1.15 + 0.02:
        bad.append(
            f"限价 {decision.limit_price:.3f} 比市价 {market_price:.3f} 高太多 —— "
            "多半是模型算错了方向或读错了报价"
        )

    if bad:
        for b in bad:
            log.warning("  风控拒绝: %s", b)
    return Verdict(not bad, bad or ["全部通过"])
