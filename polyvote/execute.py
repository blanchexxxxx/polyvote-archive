"""下单执行。

dry_run=True  → 只按当前盘口记一笔模拟成交,不碰网络,不碰钱包。
dry_run=False → 走 py-clob-client 真下单。

私钥只从环境变量 POLYVOTE_PRIVATE_KEY 读,不进 config.yaml,不进日志,
不进 SQLite。config.py 在 dry_run=false 且没有私钥时直接拒绝启动。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .brain import Decision
from .config import Config
from .market import Market, best_ask, best_bid, fetch_book

log = logging.getLogger(__name__)


@dataclass
class Fill:
    ok: bool
    price: float
    size_usd: float
    order_id: str | None = None
    status: str | None = None
    raw: Any = None
    error: str | None = None


_client = None


def _get_client(cfg: Config):
    """惰性建 CLOB 客户端。dry-run 时永远不会走到这里。"""
    global _client
    if _client is not None:
        return _client

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON
    except ImportError as e:
        raise RuntimeError(
            "实盘需要 py-clob-client:  pip install py-clob-client"
        ) from e

    ex = cfg.execution
    key = cfg.private_key
    if not key:
        raise RuntimeError("POLYVOTE_PRIVATE_KEY 没设")

    kwargs: dict[str, Any] = {
        "host": ex.get("clob_host", "https://clob.polymarket.com"),
        "key": key,
        "chain_id": ex.get("chain_id", POLYGON),
    }
    if ex.get("funder_address"):
        kwargs["funder"] = ex["funder_address"]
        kwargs["signature_type"] = ex.get("signature_type", 1)

    client = ClobClient(**kwargs)
    # L2 凭据是从私钥派生的,不需要单独保存
    client.set_api_creds(client.create_or_derive_api_creds())
    _client = client
    log.info("CLOB 客户端就绪 (host=%s)", kwargs["host"])
    return client


def _sim_price(cfg: Config, market: Market, decision: Decision) -> float:
    """模拟成交价:优先用真实盘口的对手价,拿不到就退回中间价。

    退回中间价会让 dry-run 的成绩偏乐观 (忽略了点差),日志里会标出来。
    """
    clob_host = cfg.execution.get("clob_host", "https://clob.polymarket.com")
    try:
        token_id = market.token_id_for(decision.side)
    except ValueError:
        return market.yes_price if decision.side == "YES" else market.no_price

    book = fetch_book(clob_host, token_id)
    ask = best_ask(book)
    if ask is not None:
        return ask
    log.warning("  拿不到 %s 的卖盘,模拟价退回中间价(偏乐观)", decision.side)
    return market.yes_price if decision.side == "YES" else market.no_price


def execute(cfg: Config, market: Market, decision: Decision) -> Fill:
    if cfg.risk.dry_run:
        price = _sim_price(cfg, market, decision)
        if decision.action == "BUY" and price > decision.limit_price:
            return Fill(
                ok=False,
                price=price,
                size_usd=0.0,
                error=f"模拟未成交: 卖一 {price:.3f} 高于限价 {decision.limit_price:.3f}",
            )
        log.info(
            "  [DRY-RUN] 模拟 %s %s $%.2f @ %.3f (未真实下单)",
            decision.action,
            decision.side,
            decision.size_usd,
            price,
        )
        return Fill(ok=True, price=price, size_usd=decision.size_usd, status="dry-run")

    return _execute_live(cfg, market, decision)


def _execute_live(cfg: Config, market: Market, decision: Decision) -> Fill:
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL
    except ImportError as e:
        return Fill(ok=False, price=0, size_usd=0, error=f"py-clob-client 缺失: {e}")

    try:
        client = _get_client(cfg)
        token_id = market.token_id_for(decision.side)

        book = fetch_book(cfg.execution.get("clob_host"), token_id)
        if decision.action == "BUY":
            touch = best_ask(book)
            price = min(decision.limit_price, touch) if touch else decision.limit_price
            shares = round(decision.size_usd / price, 2) if price > 0 else 0
            op = BUY
        else:
            touch = best_bid(book)
            price = max(decision.limit_price, touch) if touch else decision.limit_price
            shares = round(decision.size_usd / price, 2) if price > 0 else 0
            op = SELL

        if shares <= 0:
            return Fill(ok=False, price=price, size_usd=0, error=f"份额算出来是 {shares}")

        order = client.create_order(
            OrderArgs(price=round(price, 3), size=shares, side=op, token_id=token_id)
        )
        otype = getattr(OrderType, cfg.execution.get("order_type", "GTC"), OrderType.GTC)
        resp = client.post_order(order, otype)

        ok = bool(resp.get("success", False)) if isinstance(resp, dict) else False
        oid = resp.get("orderID") if isinstance(resp, dict) else None
        status = resp.get("status") if isinstance(resp, dict) else None
        log.info(
            "  [LIVE] %s %s %.2f 份 @ %.3f → success=%s id=%s status=%s",
            decision.action,
            decision.side,
            shares,
            price,
            ok,
            oid,
            status,
        )
        return Fill(
            ok=ok,
            price=price,
            size_usd=shares * price,
            order_id=oid,
            status=status,
            raw=resp,
            error=None if ok else f"下单被拒: {resp}",
        )
    except Exception as e:  # noqa: BLE001 — 下单挂了绝不能把主循环带崩
        log.exception("  [LIVE] 下单异常")
        return Fill(ok=False, price=0, size_usd=0, error=f"{type(e).__name__}: {e}")
