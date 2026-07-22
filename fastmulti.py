#!/usr/bin/env python3
"""Run the independent fast-market shadow bots as one supervised portfolio."""
from __future__ import annotations

import argparse
import asyncio
import copy
import logging
import signal as os_signal
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from fastbot import FastBot
from polyvote.fastconfig import FastConfig, FastConfigError, load_fast_config
from polyvote.fastanalysis import build_portfolio_replay
from polyvote.faststate import write_state


def load_portfolio(path: str | Path) -> tuple[dict[str, Any], list[FastConfig]]:
    portfolio_path = Path(path).resolve()
    raw = yaml.safe_load(portfolio_path.read_text(encoding="utf-8")) or {}
    total = float(raw.get("total_capital_usd", 0))
    if total <= 0:
        raise FastConfigError("total_capital_usd must be positive")
    base_path = Path(str(raw.get("base_config", "fast_config.yaml")))
    if not base_path.is_absolute():
        base_path = portfolio_path.parent / base_path
    base = load_fast_config(base_path)
    rows = raw.get("bots") or []
    if not rows:
        raise FastConfigError("portfolio must define at least one bot")

    configs: list[FastConfig] = []
    seen: set[str] = set()
    allocated = 0.0
    for row in rows:
        asset = str(row.get("asset", "")).upper()
        if not asset or asset in seen:
            raise FastConfigError(f"invalid or duplicate portfolio asset: {asset!r}")
        seen.add(asset)
        allocation = float(row.get("allocation_usd", 0))
        if allocation <= 0:
            raise FastConfigError(f"{asset} allocation_usd must be positive")
        allocated += allocation
        risk = copy.deepcopy(base.risk)
        risk.update(row.get("risk") or {})
        risk["initial_capital_usd"] = allocation
        if float(risk.get("max_window_usd", 0)) > allocation:
            raise FastConfigError(f"{asset} max_window_usd exceeds its allocation")
        feeds = copy.deepcopy(base.feeds)
        feeds.update({
            "chainlink_symbol": str(row.get("chainlink_symbol", f"{asset.lower()}/usd")),
            "binance_symbol": str(row.get("binance_symbol", f"{asset.lower()}usdt")),
        })
        storage = {
            "state": f"fast_state_{asset.lower()}.json",
            "decisions": f"fast_decisions_v5_{asset.lower()}.jsonl",
            "ledger": f"fast_shadow_v5_{asset.lower()}.db",
            "reviews": f"fast_reviews_v5_{asset.lower()}.json",
        }
        configs.append(replace(
            base,
            asset=asset,
            feeds=feeds,
            risk=risk,
            storage=storage,
            path=portfolio_path,
        ))
    if abs(allocated - total) > 1e-8:
        raise FastConfigError(
            f"bot allocations total ${allocated:.2f}, expected ${total:.2f}"
        )
    portfolio = {
        "total_capital_usd": total,
        "max_daily_loss_usd": float(raw.get("max_daily_loss_usd", total * 0.2)),
        "max_open_positions": int(raw.get("max_open_positions", len(configs))),
        "incident_path": str(portfolio_path.parent / "fast_portfolio_incident.json"),
        "latch_on_daily_loss": bool(raw.get("latch_on_daily_loss", True)),
        "replay_report": str(
            portfolio_path.parent / str(raw.get("replay_report", "fast_replay_report.json"))
        ),
        "observer": {
            "enabled": bool((raw.get("observer") or {}).get("enabled", True)),
            "min_selected_probability": float(
                (raw.get("observer") or {}).get("min_selected_probability", 0.55)
            ),
            "max_spread": float(
                (raw.get("observer") or {}).get("max_spread", 0.05)
            ),
            "virtual_order_usd": float(
                (raw.get("observer") or {}).get("virtual_order_usd", 5.0)
            ),
            "ledger": str(
                (raw.get("observer") or {}).get(
                    "ledger", "fast_observer_{asset}.db"
                )
            ),
        },
    }
    return portfolio, configs


async def portfolio_risk_loop(
    bots: list[FastBot], portfolio: dict[str, Any], stopping: asyncio.Event
) -> None:
    """Apply a portfolio-level gate across otherwise isolated ledgers."""
    limit = float(portfolio["max_daily_loss_usd"])
    max_positions = int(portfolio["max_open_positions"])
    incident_path = Path(str(portfolio["incident_path"]))
    latched = bool(portfolio.get("latch_on_daily_loss", True) and incident_path.exists())
    last_reason: str | None = None
    while not stopping.is_set():
        daily_pnl = sum(float(bot.account_state.get("daily_pnl_usd", 0)) for bot in bots)
        open_positions = sum(
            len(bot.account_state.get("open_positions") or []) for bot in bots
        )
        reason = None
        if latched:
            reason = "portfolio_incident_latched"
        elif daily_pnl <= -limit:
            reason = "portfolio_daily_loss_gate"
        elif open_positions >= max_positions:
            reason = "portfolio_open_position_gate"
        for bot in bots:
            bot.external_halt_reason = reason
        if reason != last_reason:
            logging.getLogger("polyvote.fast").warning(
                "portfolio gate changed: %s (daily_pnl=%.4f open_positions=%d)",
                reason or "clear", daily_pnl, open_positions,
            )
            if reason and reason != "portfolio_incident_latched":
                incident = {
                    "schema": "fast-portfolio-incident-v1",
                    "triggered_at": time.time(),
                    "trigger": reason,
                    "portfolio": {
                        "daily_pnl_usd": round(daily_pnl, 6),
                        "open_positions": open_positions,
                        "daily_loss_limit_usd": limit,
                    },
                    "bots": [{
                        "asset": bot.cfg.asset,
                        "equity_usd": bot.account_state.get("equity_usd"),
                        "daily_pnl_usd": bot.account_state.get("daily_pnl_usd"),
                        "fills": bot.account_state.get("fills"),
                        "settled": bot.account_state.get("settled"),
                    } for bot in bots],
                    "lesson": (
                        "correlated entries exhausted small per-asset allocations before "
                        "individual loss gates could react to settlement"
                    ),
                    "active_safeguards": {
                        "max_orders_per_asset_window": 1,
                        "portfolio_daily_loss_usd": limit,
                        "portfolio_max_open_positions": max_positions,
                    },
                    "next_action": (
                        "keep the champion halted; compare strict-edge and tight-execution "
                        "challengers before any new campaign"
                    ),
                }
                await asyncio.to_thread(
                    write_state, incident_path, incident
                )
                if reason == "portfolio_daily_loss_gate" and portfolio.get(
                    "latch_on_daily_loss", True
                ):
                    latched = True
            last_reason = reason
        await asyncio.sleep(0.5)


async def portfolio_replay_loop(
    configs: list[FastConfig], report_path: Path, stopping: asyncio.Event
) -> None:
    ledgers = [(cfg.asset, cfg.ledger_path) for cfg in configs]
    while not stopping.is_set():
        report = await asyncio.to_thread(build_portfolio_replay, ledgers)
        await asyncio.to_thread(write_state, report_path, report)
        try:
            await asyncio.wait_for(stopping.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass


async def async_main(path: str) -> None:
    portfolio, configs = load_portfolio(path)
    total = float(portfolio["total_capital_usd"])
    bots = [FastBot(cfg, portfolio["observer"]) for cfg in configs]
    stopping = asyncio.Event()

    def stop_all() -> None:
        stopping.set()
        for bot in bots:
            bot.stop.set()

    loop = asyncio.get_running_loop()
    for sig in (os_signal.SIGINT, os_signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_all)
        except (NotImplementedError, RuntimeError):
            pass

    logging.getLogger("polyvote.fast").info(
        "starting %d shadow bots with $%.2f total capital: %s",
        len(bots), total, ", ".join(cfg.asset for cfg in configs),
    )
    tasks = [asyncio.create_task(bot.run(), name=f"bot-{bot.cfg.asset}") for bot in bots]
    tasks.append(asyncio.create_task(
        portfolio_risk_loop(bots, portfolio, stopping), name="portfolio-risk"
    ))
    tasks.append(asyncio.create_task(
        portfolio_replay_loop(
            configs, Path(str(portfolio["replay_report"])), stopping
        ),
        name="portfolio-replay",
    ))
    done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    if not stopping.is_set():
        failed = next(iter(done))
        error = failed.exception()
        stop_all()
        await asyncio.gather(*tasks, return_exceptions=True)
        if error:
            raise error
        raise RuntimeError(f"{failed.get_name()} stopped unexpectedly")
    await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket fast shadow portfolio")
    parser.add_argument("--portfolio", default="fast_portfolio.yaml")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        asyncio.run(async_main(args.portfolio))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
