"""结算巡检。

每轮开头跑一次:把有仓位、已经解决的市场结算掉,算出真实盈亏。

这是整个项目里唯一能回答"这套投票到底行不行"的东西。投票理由写得再漂亮,
决策逻辑再清醒,没有结算数据就全是自我感觉良好。
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from .config import Config
from .store import Store

log = logging.getLogger(__name__)

_TIMEOUT = 20


def _fetch_market_state(gamma_host: str, market_id: str) -> dict[str, Any] | None:
    """按 conditionId 查单个市场的当前状态。

    ⚠ 必须显式 closed=true。Gamma 的 /markets 默认只返回**未关闭**的市场,
    而结算的前提就是市场已关闭 —— 不带这个参数,所有已解决的盘都查回空,
    _winning_side 拿到 None,永远不结算(实测:一晚上 7 笔持仓 0 结算就是这个)。
    先查 closed=true 拿已结算的;查不到再退回默认(市场还开着,本就不该结算)。
    """
    try:
        resp = requests.get(
            f"{gamma_host.rstrip('/')}/markets",
            params={"condition_ids": market_id, "closed": "true"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
        rows = payload if isinstance(payload, list) else payload.get("data", [])
        return rows[0] if rows else None
    except (requests.RequestException, ValueError, IndexError) as e:
        log.warning("查市场 %s 状态失败: %s", market_id[:20], e)
        return None


def _winning_side(raw: dict[str, Any]) -> str | None:
    """判断赢的是哪边。没解决就返回 None。

    Gamma 在市场解决后把 outcomePrices 定成 ["1","0"] 或 ["0","1"]。
    umaResolutionStatus / closed 也是信号,但价格最直接。
    """
    import json as _json

    if not raw.get("closed"):
        return None

    prices_raw = raw.get("outcomePrices")
    prices: list = []
    if isinstance(prices_raw, str):
        try:
            prices = _json.loads(prices_raw)
        except _json.JSONDecodeError:
            return None
    elif isinstance(prices_raw, list):
        prices = prices_raw

    if len(prices) != 2:
        return None
    try:
        yes = float(prices[0])
    except (TypeError, ValueError):
        return None

    # 结算后应该是 1.0 或 0.0。中间值说明还没最终定,别急着记账。
    if yes >= 0.99:
        return "YES"
    if yes <= 0.01:
        return "NO"
    return None


def settle_open_markets(cfg: Config, store: Store) -> int:
    """把已解决的持仓市场结算掉。返回结算了几个。"""
    dry = cfg.risk.dry_run
    open_mkts = store.unsettled_markets(dry)
    if not open_mkts:
        return 0

    gamma = cfg.data["gamma_host"]
    settled = 0
    for pos in open_mkts:
        raw = _fetch_market_state(gamma, pos["market_id"])
        if raw is None:
            continue
        winner = _winning_side(raw)
        if winner is None:
            continue  # 还没解决,或者还没最终定

        shares = pos.get("shares") or 0.0
        result = store.log_settlement(
            market_id=pos["market_id"],
            question=pos["question"] or raw.get("question", ""),
            winning_side=winner,
            our_side=pos["side"],
            cost_usd=pos["cost_usd"],
            shares=shares,
            dry_run=dry,
        )
        settled += 1
        log.info(
            "  结算 %s | 我方 %s vs 结果 %s → %s  本金 $%.2f 回收 $%.2f 盈亏 %+.2f",
            (pos["question"] or "")[:40],
            pos["side"],
            winner,
            "赢" if result["won"] else "输",
            pos["cost_usd"],
            result["payout_usd"],
            result["pnl_usd"],
        )
    return settled
