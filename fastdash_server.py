#!/usr/bin/env python3
"""Read-only Fast Market dashboard server.

This process intentionally knows nothing about the retired slow-market database,
model overrides, wallet credentials, or order submission.
"""
from __future__ import annotations

import argparse
import json
import math
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


HTML_PATH = Path(__file__).parent / "fast_dashboard.html"
STATE_PATH = Path(__file__).parent / "fast_state.json"
STATE_DIR: Path | None = None
STATE_CACHE_SECONDS = 2.0
_STATE_CACHE_LOCK = threading.Lock()
_STATE_CACHE_AT = 0.0
_STATE_CACHE_VALUE: dict[str, Any] | None = None
_STATE_CACHE_BODY: bytes | None = None


def read_state() -> dict[str, Any]:
    if STATE_DIR is not None:
        paths = sorted(STATE_DIR.glob("fast_state_*.json"))
        states = [_read_one(path) for path in paths]
        states = [state for state in states if state.get("asset")]
        if states:
            result = aggregate_states(states)
            incident_path = STATE_DIR / "fast_portfolio_incident.json"
            if incident_path.exists():
                result["incident"] = _read_one(incident_path)
            replay_path = STATE_DIR / "fast_replay_report.json"
            if replay_path.exists():
                result["replay"] = _read_one(replay_path)
            challenger_path = STATE_DIR / "fast_challenger_state.json"
            if challenger_path.exists():
                result["challenger"] = _read_one(challenger_path)
            return result
    return _read_one(STATE_PATH)


def reset_state_cache() -> None:
    """Clear the process-local dashboard cache (also useful for isolated tests)."""
    global _STATE_CACHE_AT, _STATE_CACHE_VALUE, _STATE_CACHE_BODY
    with _STATE_CACHE_LOCK:
        _STATE_CACHE_AT = 0.0
        _STATE_CACHE_VALUE = None
        _STATE_CACHE_BODY = None


def cached_state() -> tuple[dict[str, Any], bytes]:
    """Aggregate and serialize at most once per cache window.

    The browser polls frequently enough to make repeated JSON reads, portfolio
    aggregation and serialization a material CPU consumer on the small VPS.
    Serving the same immutable snapshot for two seconds keeps the dashboard
    responsive without touching trading or feed freshness gates.
    """
    global _STATE_CACHE_AT, _STATE_CACHE_VALUE, _STATE_CACHE_BODY
    now = time.monotonic()
    with _STATE_CACHE_LOCK:
        if (
            _STATE_CACHE_VALUE is None
            or _STATE_CACHE_BODY is None
            or now - _STATE_CACHE_AT >= STATE_CACHE_SECONDS
        ):
            value = read_state()
            body = json.dumps(
                value, ensure_ascii=False, default=str
            ).encode("utf-8")
            _STATE_CACHE_VALUE = value
            _STATE_CACHE_BODY = body
            _STATE_CACHE_AT = time.monotonic()
        return _STATE_CACHE_VALUE, _STATE_CACHE_BODY


def _read_one(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "ready": False,
            "mode": "SHADOW",
            "reason": "fast engine has not written a state snapshot yet",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "logs": [],
        }
    except (json.JSONDecodeError, OSError) as exc:
        return {
            "ready": False,
            "mode": "SHADOW",
            "reason": f"fast state unreadable: {type(exc).__name__}",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "logs": [],
        }


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _strategy_monitor(bot: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Derive four read-only signal lanes from fields already emitted by the engine.

    Only EDGE is connected to the independent execution simulator. The other
    lanes are diagnostics, deliberately labelled MONITOR so the UI never
    presents an untracked calculation as a filled strategy.
    """
    feeds = bot.get("feeds") or {}
    books = bot.get("books") or {}
    up = books.get("up") or {}
    down = books.get("down") or {}
    market = bot.get("market") or {}
    observer = bot.get("observer") or {}
    decision = observer.get("decision") or bot.get("decision") or {}

    open_price = _finite(feeds.get("open_price"))
    estimate = _finite(feeds.get("estimated_resolution_price"))
    binance = _finite((feeds.get("binance") or {}).get("price"))
    basis = _finite(feeds.get("basis_usd"))
    chain_ts = _finite((feeds.get("chainlink") or {}).get("source_ts"))
    binance_ts = _finite((feeds.get("binance") or {}).get("source_ts"))
    up_bid, up_ask = _finite(up.get("best_bid")), _finite(up.get("best_ask"))
    down_bid, down_ask = _finite(down.get("best_bid")), _finite(down.get("best_ask"))

    price_move_bps = (
        (estimate / open_price - 1) * 10_000
        if estimate is not None and open_price not in {None, 0} else None
    )
    basis_bps = (
        basis / binance * 10_000
        if basis is not None and binance not in {None, 0} else None
    )
    up_mid = (
        (up_bid + up_ask) / 2
        if up_bid is not None and up_ask is not None else None
    )
    down_mid = (
        (down_bid + down_ask) / 2
        if down_bid is not None and down_ask is not None else None
    )
    selected_fair = _finite(decision.get("fair_probability"))
    selected_side = str(decision.get("outcome") or "")
    fair_up = (
        selected_fair if selected_side == "UP"
        else 1 - selected_fair if selected_side == "DOWN" and selected_fair is not None
        else None
    )
    model_gap_pp = (
        (fair_up - up_mid) * 100
        if fair_up is not None and up_mid is not None else None
    )
    source_skew_ms = (
        abs(chain_ts - binance_ts) * 1000
        if chain_ts is not None and binance_ts is not None else None
    )

    def lane(
        lane_id: str,
        label: str,
        status: str,
        side: str,
        metric: float | None,
        unit: str,
        reason: str,
        tracked: bool = False,
    ) -> dict[str, Any]:
        return {
            "id": lane_id,
            "label": label,
            "status": status,
            "side": side,
            "metric": round(metric, 4) if metric is not None else None,
            "unit": unit,
            "reason": reason,
            "tracked_execution": tracked,
        }

    edge = _finite(decision.get("net_edge"))
    edge_action = str(decision.get("action") or "WAIT")
    edge_reason = ", ".join(decision.get("reasons") or []) or "all gates passed"
    momentum_side = "UP" if (price_move_bps or 0) > 0 else "DOWN" if price_move_bps is not None else "—"
    basis_side = "CHAIN>BIN" if (basis_bps or 0) > 0 else "BIN>CHAIN" if basis_bps is not None else "—"
    micro_side = "UP" if (model_gap_pp or 0) > 0 else "DOWN" if model_gap_pp is not None else "—"
    lanes = [
        lane(
            "EDGE", "Fee-aware edge",
            "SIGNAL" if edge_action == "CANDIDATE" else "GATED",
            selected_side or "—", edge * 100 if edge is not None else None, "pp",
            edge_reason, tracked=bool(observer.get("enabled")),
        ),
        lane(
            "MOM", "Open momentum",
            "SIGNAL" if price_move_bps is not None and abs(price_move_bps) >= 3 else "WATCH",
            momentum_side, price_move_bps, "bps", "estimated resolution vs window open",
        ),
        lane(
            "BASIS", "Venue basis",
            "SIGNAL" if basis_bps is not None and abs(basis_bps) >= 1 else "WATCH",
            basis_side, basis_bps, "bps", "Chainlink minus Binance",
        ),
        lane(
            "MICRO", "Model vs book",
            "SIGNAL" if model_gap_pp is not None and abs(model_gap_pp) >= 3 else "WATCH",
            micro_side, model_gap_pp, "pp", "fair UP probability minus CLOB midpoint",
        ),
    ]
    diagnostics = {
        "price_move_bps": round(price_move_bps, 4) if price_move_bps is not None else None,
        "basis_bps": round(basis_bps, 4) if basis_bps is not None else None,
        "fair_up": round(fair_up, 6) if fair_up is not None else None,
        "up_mid": round(up_mid, 6) if up_mid is not None else None,
        "down_mid": round(down_mid, 6) if down_mid is not None else None,
        "ask_overround_pp": round((up_ask + down_ask - 1) * 100, 4)
        if up_ask is not None and down_ask is not None else None,
        "source_skew_ms": round(source_skew_ms, 3) if source_skew_ms is not None else None,
        "book_age_ms": max(
            _number(up.get("age_ms")), _number(down.get("age_ms"))
        ),
        "seconds_remaining": _finite(market.get("seconds_remaining")),
        "cvd": {"status": "NO_FEED", "value": None},
        "liquidations": {"status": "NO_FEED", "value": None},
    }
    return lanes, diagnostics


def aggregate_states(states: list[dict[str, Any]]) -> dict[str, Any]:
    """Create a read-only portfolio view without rewriting any bot ledger."""
    now = time.time()
    bots: list[dict[str, Any]] = []
    recent_trades: list[dict[str, Any]] = []
    recent_observations: list[dict[str, Any]] = []
    observer_totals = {
        "capital_usd": 0.0,
        "candidates": 0,
        "fills": 0,
        "rejects": 0,
        "open_observations": 0,
        "settled": 0,
        "wins": 0,
        "pnl_usd": 0.0,
        "staked_usd": 0.0,
        "fees_usd": 0.0,
        "latency_weighted_ms": 0.0,
        "latency_samples": 0,
    }
    totals = {
        "initial_capital_usd": 0.0,
        "cash_usd": 0.0,
        "locked_usd": 0.0,
        "market_value_usd": 0.0,
        "equity_usd": 0.0,
        "realized_pnl_usd": 0.0,
        "daily_pnl_usd": 0.0,
        "fees_usd": 0.0,
        "max_drawdown_usd": 0.0,
        "staked_usd": 0.0,
        "fills": 0,
        "settled": 0,
        "wins": 0,
        "rejects": 0,
        "open_positions": 0,
    }
    timestamps: list[float] = []
    for state in sorted(states, key=lambda item: str(item.get("asset"))):
        account = state.get("account") or {}
        updated = str(state.get("updated_at") or "")
        try:
            timestamp = datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            timestamp = 0.0
        timestamps.append(timestamp)
        online = timestamp > 0 and now - timestamp <= 3
        open_positions = account.get("open_positions") or []
        bot = dict(state)
        bot["online"] = online
        bot["state_age_seconds"] = round(max(0.0, now - timestamp), 3) if timestamp else None
        bot["open_position_count"] = len(open_positions)
        bot["strategy_lanes"], bot["diagnostics"] = _strategy_monitor(bot)
        bots.append(bot)
        for key in (
            "initial_capital_usd", "cash_usd", "locked_usd", "market_value_usd",
            "equity_usd", "realized_pnl_usd", "daily_pnl_usd", "fees_usd",
            "max_drawdown_usd", "staked_usd",
        ):
            totals[key] += _number(account.get(key))
        for key in ("fills", "settled", "wins", "rejects"):
            totals[key] += int(account.get(key) or 0)
        totals["open_positions"] += len(open_positions)
        for trade in account.get("recent_trades") or []:
            recent_trades.append({**trade, "asset": state.get("asset")})
        observer = state.get("observer") or {}
        for key in (
            "candidates", "fills", "rejects", "open_observations", "settled", "wins"
        ):
            observer_totals[key] += int(observer.get(key) or 0)
        for key in ("pnl_usd", "staked_usd", "fees_usd"):
            observer_totals[key] += _number(observer.get(key))
        observer_latency = observer.get("average_latency_ms")
        observer_fills = int(observer.get("fills") or 0)
        if observer_latency is not None and observer_fills:
            observer_totals["latency_weighted_ms"] += (
                _number(observer_latency) * observer_fills
            )
            observer_totals["latency_samples"] += observer_fills
        for observation in observer.get("recent_observations") or []:
            recent_observations.append({
                **observation,
                "asset": state.get("asset"),
            })

    for key, value in list(totals.items()):
        if isinstance(value, float):
            totals[key] = round(value, 6)
    totals["pnl_usd"] = totals["realized_pnl_usd"]
    totals["win_rate"] = (
        round(totals["wins"] / totals["settled"], 6) if totals["settled"] else None
    )
    totals["roi"] = (
        round(totals["pnl_usd"] / totals["staked_usd"], 6)
        if totals["staked_usd"] else None
    )
    totals["recent_trades"] = sorted(
        recent_trades, key=lambda row: _number(row.get("created_ts")), reverse=True
    )[:50]
    freshest = max(timestamps) if timestamps else 0
    stalest = min(timestamps) if timestamps else 0
    online_count = sum(1 for bot in bots if bot["online"])
    observer_settled = int(observer_totals["settled"])
    observer_wins = int(observer_totals["wins"])
    observer_latency = (
        observer_totals["latency_weighted_ms"]
        / observer_totals["latency_samples"]
        if observer_totals["latency_samples"] else None
    )
    observer_summary = {
        key: round(value, 6) if isinstance(value, float) else value
        for key, value in observer_totals.items()
        if key not in {"latency_weighted_ms", "latency_samples"}
    }
    observer_starts = [
        _number((bot.get("observer") or {}).get("started_ts"))
        for bot in bots
        if _number((bot.get("observer") or {}).get("started_ts")) > 0
    ]
    observer_started_ts = min(observer_starts) if observer_starts else None
    observer_runtime_hours = (
        max(0.0, (now - observer_started_ts) / 3600)
        if observer_started_ts else 0.0
    )
    observer_summary.update({
        "enabled_bot_count": sum(
            1 for bot in bots if (bot.get("observer") or {}).get("enabled")
        ),
        "accuracy": (
            round(observer_wins / observer_settled, 6)
            if observer_settled else None
        ),
        "roi": (
            round(
                _number(observer_totals["pnl_usd"])
                / _number(observer_totals["staked_usd"]),
                6,
            )
            if observer_totals["staked_usd"] else None
        ),
        "average_latency_ms": (
            round(observer_latency, 3) if observer_latency is not None else None
        ),
        "recent_observations": sorted(
            recent_observations,
            key=lambda row: _number(row.get("created_ts")),
            reverse=True,
        )[:50],
        "promotion_gate": {
            "minimum_settled": 500,
            "minimum_runtime_hours": 336,
            "runtime_hours": round(observer_runtime_hours, 3),
            "requires_frozen_holdout": True,
        },
    })
    return {
        "schema": "fast-portfolio-state-v1",
        "ready": online_count == len(bots) and all(bot.get("ready") for bot in bots),
        "mode": "SHADOW",
        "updated_at": datetime.fromtimestamp(stalest or freshest, timezone.utc).isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "portfolio": {
            **totals,
            "bot_count": len(bots),
            "execution_engine_count": len(bots),
            "strategy_lane_count": sum(
                len(bot.get("strategy_lanes") or []) for bot in bots
            ),
            "online_bot_count": online_count,
            "active_market_count": sum(1 for bot in bots if bot.get("market")),
            "under_50ms_count": sum(
                1 for bot in bots if (bot.get("network") or {}).get("under_50ms")
            ),
            "halted_bot_count": sum(1 for bot in bots if bot.get("external_halt_reason")),
            "halt_reasons": sorted({
                str(bot.get("external_halt_reason"))
                for bot in bots if bot.get("external_halt_reason")
            }),
        },
        "observer": observer_summary,
        "bots": bots,
    }
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/", "/index.html", "/fast", "/fast/", "/fast/index.html"}:
            try:
                self._send(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, b"fast_dashboard.html not found", "text/plain")
            return
        if path == "/api/fast/state":
            _state, body = cached_state()
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/healthz":
            state, _body = cached_state()
            self._json(200 if state.get("ready") else 503, {
                "ready": bool(state.get("ready")),
                "updated_at": state.get("updated_at"),
                "portfolio": {
                    key: (state.get("portfolio") or {}).get(key)
                    for key in (
                        "equity_usd", "fills", "settled", "online_bot_count",
                        "under_50ms_count", "halted_bot_count", "halt_reasons",
                    )
                },
                "observer": {
                    key: (state.get("observer") or {}).get(key)
                    for key in (
                        "candidates", "fills", "rejects", "open_observations",
                        "settled", "wins", "accuracy", "pnl_usd",
                        "average_latency_ms", "promotion_gate",
                    )
                },
                "challenger": {
                    key: (state.get("challenger") or {}).get(key)
                    for key in (
                        "experiment_id", "status", "equity_usd", "pnl_usd",
                        "fills", "settled", "wins", "roi", "promotion_gate",
                    )
                },
            })
            return
        if path in {"/slow", "/slow/", "/api/state"}:
            self._send(410, b"slow market retired", "text/plain; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        self._send(405, b"read-only dashboard", "text/plain; charset=utf-8")


def main() -> None:
    global STATE_PATH, STATE_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8920)
    parser.add_argument("--fast-state", default="fast_state.json")
    parser.add_argument("--state-dir", default=None)
    args = parser.parse_args()
    STATE_PATH = Path(args.fast_state)
    STATE_DIR = Path(args.state_dir) if args.state_dir else None
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Fast dashboard: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
