#!/usr/bin/env python3
"""Real-feed, shadow-execution runner for BTC five-minute markets.

This process deliberately does not accept private keys.  It proves feed quality,
book consistency and fee-adjusted edge before a separate live executor is enabled.
"""
from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import logging
import math
import signal as os_signal
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from polyvote.fastbook import BookPair, LocalBook
from polyvote.fastconfig import FastConfig, load_fast_config
from polyvote.fastledger import FastLedger
from polyvote.fastmarket import (
    FastMarket,
    apply_clob_market_info,
    apply_market_processing_info,
    crypto_fee_per_share,
    market_slug,
    parse_fast_market,
    resolved_fast_outcome,
    select_any_market_payload,
    select_market_payload,
    window_start_epoch,
)
from polyvote.fastobserver import FastObserverLedger
from polyvote.fastreview import run_due_reviews
from polyvote.faststate import append_decision, write_state
from polyvote.faststrategy import Decision, FastSignalModel, evaluate_entry

log = logging.getLogger("polyvote.fast")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_seconds(value: Any) -> float | None:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    ts = ts / 1000 if ts > 10_000_000_000 else ts
    return ts if math.isfinite(ts) and ts > 0 else None


def _price(payload: dict[str, Any]) -> float | None:
    for key in ("value", "price", "p"):
        try:
            value = float(payload[key])
        except (KeyError, TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


class MetricWindow:
    def __init__(self, size: int = 240):
        self.values: deque[float] = deque(maxlen=size)

    def add(self, value: float) -> None:
        if math.isfinite(value) and value >= 0:
            self.values.append(float(value))

    def state(self) -> dict[str, Any]:
        if not self.values:
            return {"samples": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
        values = sorted(self.values)
        pick = lambda p: values[min(len(values) - 1, int((len(values) - 1) * p))]
        return {
            "samples": len(values),
            "p50_ms": round(pick(0.50), 2),
            "p95_ms": round(pick(0.95), 2),
            "max_ms": round(values[-1], 2),
        }


class ProbeTimingDiagnostics:
    """Low-overhead phase and TTFB counts for the existing HTTP probe."""

    _TTFB_BUCKETS = (
        ("under_100_ms", 100.0),
        ("100_to_140_ms", 140.0),
        ("140_to_180_ms", 180.0),
        ("180_to_500_ms", 500.0),
        ("at_least_500_ms", math.inf),
    )

    def __init__(self, size: int = 240):
        self.samples: deque[str] = deque(maxlen=size)
        self.counts: Counter[str] = Counter()
        self.latest: dict[str, float] | None = None

    @classmethod
    def _bucket(cls, ttfb_ms: float) -> str:
        for name, upper_bound in cls._TTFB_BUCKETS:
            if ttfb_ms < upper_bound:
                return name
        raise AssertionError("unreachable TTFB bucket")

    def add(
        self,
        *,
        submitted: float,
        worker_started: float,
        worker_completed: float,
        coroutine_completed: float,
        ttfb_ms: float,
    ) -> None:
        bucket = self._bucket(ttfb_ms)
        if len(self.samples) == self.samples.maxlen:
            expired = self.samples[0]
            self.counts[expired] -= 1
        self.samples.append(bucket)
        self.counts[bucket] += 1
        self.latest = {
            "submitted_monotonic": submitted,
            "worker_started_monotonic": worker_started,
            "worker_completed_monotonic": worker_completed,
            "coroutine_completed_monotonic": coroutine_completed,
            "socket_ttfb_ms": ttfb_ms,
        }

    def state(self) -> dict[str, Any]:
        return {
            "samples": len(self.samples),
            "latest": (
                None
                if self.latest is None
                else {key: round(value, 6) for key, value in self.latest.items()}
            ),
            "ttfb_buckets": {
                name: self.counts[name] for name, _ in self._TTFB_BUCKETS
            },
        }


class PersistentClobProbe:
    """Dedicated keep-alive probe, isolated from the market-data event loop."""

    def __init__(self, host: str = "clob.polymarket.com"):
        self.host = host
        self.connection: http.client.HTTPSConnection | None = None
        self.lock = threading.Lock()

    def measure(self, submitted_at: float | None = None) -> dict[str, float]:
        worker_started = time.perf_counter()
        queue_wait_ms = (
            max(0.0, worker_started - submitted_at) * 1000
            if submitted_at is not None
            else 0.0
        )
        with self.lock:
            began = time.perf_counter()
            try:
                if self.connection is None:
                    self.connection = http.client.HTTPSConnection(self.host, timeout=5)
                self.connection.request("GET", "/time", headers={"Connection": "keep-alive"})
                response = self.connection.getresponse()
                headers_received = time.perf_counter()
                response.read()
                finished = time.perf_counter()
                if response.status != 200:
                    raise OSError(f"HTTP {response.status}")
                return {
                    "total_ms": (finished - began) * 1000,
                    "queue_wait_ms": queue_wait_ms,
                    "socket_ttfb_ms": (headers_received - began) * 1000,
                    "body_read_ms": (finished - headers_received) * 1000,
                    "worker_started_monotonic": worker_started,
                    "worker_completed_monotonic": finished,
                }
            except Exception:
                self.close()
                raise

    def measure_ms(self) -> float:
        return self.measure()["total_ms"]

    def close(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            finally:
                self.connection = None


class FastBot:
    def __init__(self, cfg: FastConfig, observer: dict[str, Any] | None = None):
        self.cfg = cfg
        self.signal = FastSignalModel(cfg.strategy)
        self.market: FastMarket | None = None
        self.books: BookPair | None = None
        self.market_ws_task: asyncio.Task | None = None
        self.prefetched_market: FastMarket | None = None
        self.prefetched_books: BookPair | None = None
        self.prefetched_ws_task: asyncio.Task | None = None
        self.stop = asyncio.Event()
        self.session: aiohttp.ClientSession | None = None
        self.logs: deque[dict[str, Any]] = deque(maxlen=30)
        self.clob_http = MetricWindow()
        self.clob_http_queue_wait = MetricWindow()
        self.clob_http_socket_ttfb = MetricWindow()
        self.clob_http_body_read = MetricWindow()
        self.clob_http_timing = ProbeTimingDiagnostics()
        self.decision_compute = MetricWindow()
        self.event_loop_lag = MetricWindow()
        self.http_probe = PersistentClobProbe()
        self.reconnects = {"rtds": 0, "market": 0}
        self.last_decision: Decision | None = None
        self.shadow_orders_this_window = 0
        self.shadow_committed_by_condition: dict[str, float] = {}
        self.shadow_pending_by_condition: dict[str, float] = {}
        self.execution_reservations: dict[asyncio.Task, tuple[str, float]] = {}
        self.last_shadow_entry_monotonic = 0.0
        self.market_discovery_ms: float | None = None
        self.geoblock: dict[str, Any] | None = None
        self.audit_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self.started_at = _utc_now()
        self.ledger = FastLedger(
            cfg.ledger_path,
            float(cfg.risk.get("initial_capital_usd", 10.0)),
        )
        self.account_state = self.ledger.state()
        self.recovered_intents = self.ledger.cancel_pending_intents()
        if self.recovered_intents:
            self.account_state = self.ledger.state()
        self.execution_tasks: set[asyncio.Task] = set()
        self.gate_counts: dict[str, Counter[str]] = {}
        self.last_gate_sample: dict[str, tuple[float, tuple[str, ...]]] = {}
        self.last_ledger_refresh = 0.0
        self.last_gate_flush = 0.0
        self.settlement_retry_after: dict[str, float] = {}
        self.feed_rejects: Counter[str] = Counter()
        self.review_state = self.ledger.recent_reviews(1)
        self.external_halt_reason: str | None = None
        supplied_observer = observer or {}
        self.observer_cfg = {
            "enabled": bool(supplied_observer.get("enabled", False)),
            "min_selected_probability": float(
                supplied_observer.get("min_selected_probability", 0.55)
            ),
            "max_spread": float(supplied_observer.get("max_spread", 0.05)),
            "virtual_order_usd": float(
                supplied_observer.get("virtual_order_usd", 5.0)
            ),
            "max_orders_per_window": 1,
        }
        self.observer: FastObserverLedger | None = None
        self.observer_state: dict[str, Any] = {}
        self.observer_last_decision: Decision | None = None
        self.observer_orders_this_window = 0
        self.observer_seen_conditions: set[str] = set()
        self.observer_execution_tasks: set[asyncio.Task] = set()
        self.observer_settlement_retry_after: dict[str, float] = {}
        if self.observer_cfg["enabled"]:
            pattern = str(supplied_observer.get("ledger", "fast_observer_{asset}.db"))
            observer_path = Path(pattern.format(asset=cfg.asset.lower()))
            if not observer_path.is_absolute():
                observer_path = cfg.path.parent / observer_path
            self.observer = FastObserverLedger(observer_path, cfg.asset)
            self.observer.reject_pending_after_restart()
            self.observer_seen_conditions = self.observer.condition_ids()
            self.observer_state = self.observer.state()

    def event(self, level: str, message: str, **fields: Any) -> None:
        item = {"ts": _utc_now(), "level": level, "message": message, **fields}
        self.logs.appendleft(item)
        getattr(log, level if level in {"info", "warning", "error"} else "info")(message)

    async def run(self) -> None:
        if self.cfg.mode != "shadow":
            raise SystemExit(
                "Fast engine is shadow-only until its user-channel ledger and live canary gates pass. "
                "Do not bypass this guard with a private key."
            )
        timeout = aiohttp.ClientTimeout(total=10, connect=5)
        connector = aiohttp.TCPConnector(limit=12, ttl_dns_cache=300, keepalive_timeout=30)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            self.session = session
            await self._geoblock_probe(session)
            tasks = [
                asyncio.create_task(self._rtds_loop(session), name="rtds"),
                asyncio.create_task(self._market_discovery_loop(session), name="discovery"),
                asyncio.create_task(self._network_probe_loop(), name="probe"),
                asyncio.create_task(self._loop_lag_loop(), name="loop-lag"),
                asyncio.create_task(self._settlement_loop(session), name="settlement"),
                asyncio.create_task(self._review_loop(), name="review"),
                asyncio.create_task(self._state_loop(), name="state"),
                asyncio.create_task(self._audit_loop(), name="audit"),
            ]
            for task in tasks:
                task.add_done_callback(self._task_done)
            try:
                await self.stop.wait()
            finally:
                for task in tasks:
                    task.cancel()
                if self.market_ws_task:
                    self.market_ws_task.cancel()
                if self.prefetched_ws_task:
                    self.prefetched_ws_task.cancel()
                for task in self.execution_tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if self.market_ws_task:
                    await asyncio.gather(self.market_ws_task, return_exceptions=True)
                if self.prefetched_ws_task:
                    await asyncio.gather(self.prefetched_ws_task, return_exceptions=True)
                if self.execution_tasks:
                    await asyncio.gather(*self.execution_tasks, return_exceptions=True)
                for task in self.observer_execution_tasks:
                    task.cancel()
                if self.observer_execution_tasks:
                    await asyncio.gather(
                        *self.observer_execution_tasks, return_exceptions=True
                    )
                await asyncio.to_thread(
                    self.ledger.cancel_pending_intents,
                    "service_shutdown_cancel",
                )
                await self._flush_gate_counts()
                self.http_probe.close()
                self.session = None

    def _task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            self.event("error", f"task {task.get_name()} stopped: {type(exc).__name__}: {exc}")
        elif task.get_name() in {
            "rtds", "discovery", "probe", "loop-lag", "settlement", "review", "state", "audit"
        }:
            self.event("error", f"critical task {task.get_name()} stopped unexpectedly")
        if task.get_name() in {
            "rtds", "discovery", "probe", "loop-lag", "settlement", "review", "state", "audit"
        }:
            self.stop.set()

    async def _geoblock_probe(self, session: aiohttp.ClientSession) -> None:
        try:
            async with session.get(self.cfg.endpoints["geoblock"]) as response:
                self.geoblock = await response.json(content_type=None)
                self.event("info", f"network country: {self.geoblock.get('country', 'unknown')}")
        except Exception as exc:  # noqa: BLE001
            self.geoblock = {"error": f"{type(exc).__name__}: {exc}"}
            self.event("warning", "geoblock preflight unavailable")

    async def _fetch_market(self, session: aiohttp.ClientSession, start: int) -> FastMarket | None:
        slug = market_slug(self.cfg.asset, start, self.cfg.window_seconds)
        began = time.perf_counter()
        for endpoint in ("events", "markets"):
            try:
                async with session.get(
                    f"{self.cfg.endpoints['gamma'].rstrip('/')}/{endpoint}", params={"slug": slug}
                ) as response:
                    response.raise_for_status()
                    raw = select_market_payload(await response.json(content_type=None))
                    if raw:
                        self.market_discovery_ms = (time.perf_counter() - began) * 1000
                        market = parse_fast_market(raw, start, self.cfg.window_seconds)
                        try:
                            url = (
                                f"{self.cfg.endpoints['clob'].rstrip('/')}"
                                f"/clob-markets/{market.condition_id}"
                            )
                            async with session.get(url) as info_response:
                                info_response.raise_for_status()
                                market = apply_clob_market_info(
                                    market,
                                    await info_response.json(content_type=None),
                                )
                        except Exception as exc:  # noqa: BLE001
                            self.event(
                                "warning",
                                f"CLOB market metadata unavailable: {type(exc).__name__}",
                            )
                        try:
                            delay_url = (
                                f"{self.cfg.endpoints['clob'].rstrip('/')}"
                                f"/markets/{market.condition_id}"
                            )
                            async with session.get(delay_url) as delay_response:
                                delay_response.raise_for_status()
                                market = apply_market_processing_info(
                                    market,
                                    await delay_response.json(content_type=None),
                                )
                        except Exception as exc:  # noqa: BLE001
                            self.event(
                                "warning",
                                f"CLOB processing delay unavailable: {type(exc).__name__}",
                            )
                        return market
            except Exception as exc:  # noqa: BLE001
                self.event("warning", f"market discovery {endpoint} failed: {type(exc).__name__}")
        return None

    async def _market_discovery_loop(self, session: aiohttp.ClientSession) -> None:
        while True:
            now = time.time()
            start = window_start_epoch(now, self.cfg.window_seconds)
            if self.market is None or self.market.start_epoch != start:
                if self.prefetched_market and self.prefetched_market.start_epoch == start:
                    await self._activate_market(
                        session,
                        self.prefetched_market,
                        self.prefetched_books,
                        self.prefetched_ws_task,
                    )
                else:
                    await self._clear_prefetch()
                    market = await self._fetch_market(session, start)
                    if market:
                        await self._activate_market(session, market)

            if self.market:
                prefetch_seconds = float(self.cfg.feeds.get("prefetch_seconds", 20))
                next_start = self.market.start_epoch + self.cfg.window_seconds
                if (
                    now >= self.market.end_epoch - prefetch_seconds
                    and (
                        self.prefetched_market is None
                        or self.prefetched_market.start_epoch != next_start
                    )
                ):
                    market = await self._fetch_market(session, next_start)
                    if market:
                        books = BookPair(market.up_token, market.down_token)
                        self.prefetched_market = market
                        self.prefetched_books = books
                        self.prefetched_ws_task = asyncio.create_task(
                            self._market_ws_loop(session, market, books),
                            name="market-ws-prefetch",
                        )
                        self.prefetched_ws_task.add_done_callback(self._task_done)
                        self.event("info", f"prefetched next market: {market.slug}")
            await asyncio.sleep(0.5 if self.market is None else 1.0)

    async def _activate_market(
        self,
        session: aiohttp.ClientSession,
        market: FastMarket,
        books: BookPair | None = None,
        warm_task: asyncio.Task | None = None,
    ) -> None:
        if self.market and market.condition_id == self.market.condition_id:
            return
        old_task = self.market_ws_task
        if self.market:
            await self._flush_gate_counts(self.market.condition_id)
        self.market = market
        self.books = books or BookPair(market.up_token, market.down_token)
        self.market_ws_task = warm_task
        self.prefetched_market = None
        self.prefetched_books = None
        self.prefetched_ws_task = None
        self.signal.reset_window()
        self.signal.capture_open(market)
        self.shadow_orders_this_window = 0
        self.observer_orders_this_window = int(
            market.condition_id in self.observer_seen_conditions
        )
        self.shadow_committed_by_condition[market.condition_id] = 0.0
        self.shadow_pending_by_condition[market.condition_id] = 0.0
        self.last_shadow_entry_monotonic = 0.0
        self.last_decision = None
        self.event(
            "info",
            f"active market: {market.slug}",
            warm_handoff=warm_task is not None,
            taker_delay_ms=market.taker_delay_ms,
        )
        await asyncio.to_thread(
            self.ledger.ensure_window,
            market.to_dict(),
            self.signal.open_price,
            self.signal.open_source_ts,
        )
        if warm_task is None:
            self.market_ws_task = asyncio.create_task(
                self._market_ws_loop(session, market, self.books), name="market-ws"
            )
            self.market_ws_task.add_done_callback(self._task_done)
        if old_task and old_task is not self.market_ws_task:
            old_task.cancel()
            await asyncio.gather(old_task, return_exceptions=True)
        self._evaluate_hot()

    async def _clear_prefetch(self) -> None:
        task = self.prefetched_ws_task
        self.prefetched_market = None
        self.prefetched_books = None
        self.prefetched_ws_task = None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _market_is_tracked(self, condition_id: str) -> bool:
        return any(
            market is not None and market.condition_id == condition_id
            for market in (self.market, self.prefetched_market)
        )

    async def _market_ws_loop(
        self,
        session: aiohttp.ClientSession,
        market: FastMarket,
        books: BookPair,
    ) -> None:
        backoff = 1.0
        while self._market_is_tracked(market.condition_id):
            try:
                async with session.ws_connect(
                    self.cfg.endpoints["market_ws"], heartbeat=15, receive_timeout=30
                ) as ws:
                    await ws.send_json({
                        "assets_ids": [market.up_token, market.down_token],
                        "type": "market",
                        "custom_feature_enabled": True,
                    })
                    self.event("info", "CLOB websocket connected")
                    backoff = 1.0
                    ping_task = asyncio.create_task(self._text_ping(ws, 10))
                    watchdog_task = asyncio.create_task(
                        self._book_inactivity_watchdog(ws, books),
                        name=f"book-watchdog-{self.cfg.asset}",
                    )
                    try:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                payload = msg.data
                                first = payload[:1]
                                if first not in {"{", "["}:
                                    continue
                                # custom_feature_enabled gives us a dedicated
                                # best_bid_ask event.  Deep price_change arrays
                                # are intentionally skipped on the 512 MB VPS;
                                # a fresh REST depth snapshot is fetched only
                                # for the rare intent at simulated arrival.
                                if (
                                    first == "{"
                                    and "price_change" in payload[:160]
                                ):
                                    books.touch(time.monotonic())
                                    continue
                                if (
                                    first == "{"
                                    and books.initialized
                                    and '"event_type"' in payload[:160]
                                    and '"book"' in payload[:160]
                                ):
                                    books.touch(time.monotonic())
                                    continue
                                data = json.loads(payload)
                                items = data if isinstance(data, list) else [data]
                                recv = time.monotonic()
                                for item in items:
                                    if isinstance(item, dict):
                                        books.apply(item, recv)
                                if self.market and self.market.condition_id == market.condition_id:
                                    self._evaluate_hot()
                            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                                break
                    finally:
                        ping_task.cancel()
                        watchdog_task.cancel()
                        await asyncio.gather(
                            ping_task, watchdog_task, return_exceptions=True
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.reconnects["market"] += 1
                self.event("warning", f"CLOB websocket reconnect: {type(exc).__name__}")
            await asyncio.sleep(backoff)
            backoff = min(float(self.cfg.feeds.get("reconnect_max_seconds", 20)), backoff * 2)

    async def _rtds_loop(self, session: aiohttp.ClientSession) -> None:
        backoff = 1.0
        binance_symbol = str(self.cfg.feeds.get("binance_symbol", "btcusdt"))
        chainlink_symbol = str(self.cfg.feeds.get("chainlink_symbol", "btc/usd"))
        subscriptions = [
            {
                "topic": "crypto_prices",
                "type": "update",
                "filters": json.dumps(
                    {"symbol": binance_symbol}, separators=(",", ":")
                ),
            },
            {
                "topic": "crypto_prices_chainlink",
                "type": "update",
                "filters": json.dumps(
                    {"symbol": chainlink_symbol}, separators=(",", ":")
                ),
            },
        ]
        while True:
            try:
                async with session.ws_connect(
                    self.cfg.endpoints["rtds_ws"], heartbeat=10, receive_timeout=30
                ) as ws:
                    # The official RTDS client sends a lowercase application ping
                    # before subscribing (the prose docs currently show uppercase).
                    await ws.send_str("ping")
                    await ws.send_json({"action": "subscribe", "subscriptions": subscriptions})
                    self.event("info", "RTDS websocket connected")
                    backoff = 1.0
                    ping_task = asyncio.create_task(self._text_ping(ws, 5, "ping"))
                    watchdog_task = asyncio.create_task(
                        self._rtds_inactivity_watchdog(ws),
                        name=f"rtds-watchdog-{self.cfg.asset}",
                    )
                    try:
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                if msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                                    break
                                continue
                            if not msg.data.lstrip().startswith(("{", "[")):
                                continue
                            data = json.loads(msg.data)
                            changed = False
                            for item in data if isinstance(data, list) else [data]:
                                changed = self._handle_price_message(item) or changed
                            if changed:
                                self._evaluate_hot()
                    finally:
                        ping_task.cancel()
                        watchdog_task.cancel()
                        await asyncio.gather(
                            ping_task, watchdog_task, return_exceptions=True
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.reconnects["rtds"] += 1
                self.event("warning", f"RTDS websocket reconnect: {type(exc).__name__}")
            await asyncio.sleep(backoff)
            backoff = min(float(self.cfg.feeds.get("reconnect_max_seconds", 20)), backoff * 2)

    @staticmethod
    async def _text_ping(
        ws: aiohttp.ClientWebSocketResponse, seconds: float, payload: str = "PING"
    ) -> None:
        while True:
            await asyncio.sleep(seconds)
            await ws.send_str(payload)

    async def _book_inactivity_watchdog(
        self, ws: aiohttp.ClientWebSocketResponse, books: BookPair
    ) -> None:
        """A PONG is not proof that the subscribed orderbook is still moving."""
        while True:
            await asyncio.sleep(5)
            if not books.initialized:
                continue
            age_ms = max(books.up.age_ms(), books.down.age_ms())
            if age_ms > 15_000:
                self.event("warning", "CLOB book silent for 15s; forcing reconnect")
                await ws.close(code=1011, message=b"book inactivity watchdog")
                return

    async def _rtds_inactivity_watchdog(
        self, ws: aiohttp.ClientWebSocketResponse
    ) -> None:
        """Force a resubscribe when keepalives continue but Binance ticks stop."""
        while True:
            await asyncio.sleep(5)
            if self.signal.binance and self.signal.binance.age_ms() > 10_000:
                self.event("warning", "RTDS price stream silent for 10s; forcing reconnect")
                await ws.close(code=1011, message=b"price inactivity watchdog")
                return

    def _handle_price_message(self, item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        changed = False
        topic = str(item.get("topic") or "")
        payload = item.get("payload") or item.get("data") or item
        payloads = payload if isinstance(payload, list) else [payload]
        for row in payloads:
            if not isinstance(row, dict):
                continue
            # A filtered RTDS subscription begins with a recent-data snapshot:
            # {"symbol":"btc/usd","data":[{"timestamp":...,"value":...}]}.
            # Replay it in source order so the exact five-minute opening print
            # remains available even when discovery finishes a few seconds late.
            history = row.get("data")
            if isinstance(history, list):
                wrapper_symbol = row.get("symbol")
                for point in history:
                    if not isinstance(point, dict):
                        continue
                    nested = dict(point)
                    nested.setdefault("symbol", wrapper_symbol)
                    changed = self._handle_price_message({
                        "topic": topic,
                        "payload": nested,
                        "timestamp": item.get("timestamp"),
                        "historical": True,
                    }) or changed
                continue
            symbol = str(row.get("symbol") or "").lower()
            price = _price(row)
            if price is None:
                continue
            source_ts = _source_seconds(row.get("timestamp") or item.get("timestamp"))
            if source_ts is None:
                self.feed_rejects["missing_source_timestamp"] += 1
                continue
            wall_delta = source_ts - time.time()
            max_past_seconds = 600 if item.get("historical") else 30
            if wall_delta > 5 or wall_delta < -max_past_seconds:
                self.feed_rejects["implausible_source_timestamp"] += 1
                continue
            if "chainlink" in topic and symbol == str(
                self.cfg.feeds.get("chainlink_symbol", "btc/usd")
            ).lower():
                changed = self.signal.update_chainlink(price, source_ts) or changed
                if self.market:
                    self.signal.capture_open(self.market)
            elif "crypto_prices" in topic and symbol == str(
                self.cfg.feeds.get("binance_symbol", "btcusdt")
            ).lower():
                changed = self.signal.update_binance(price, source_ts) or changed
        return changed

    async def _network_probe_loop(self) -> None:
        try:
            await asyncio.to_thread(self.http_probe.measure, time.perf_counter())
        except Exception as exc:  # noqa: BLE001
            self.event("warning", f"CLOB probe warmup failed: {type(exc).__name__}")
        while True:
            try:
                submitted = time.perf_counter()
                sample = await asyncio.to_thread(self.http_probe.measure, submitted)
                coroutine_completed = time.perf_counter()
                self.clob_http.add(sample["total_ms"])
                self.clob_http_queue_wait.add(sample["queue_wait_ms"])
                self.clob_http_socket_ttfb.add(sample["socket_ttfb_ms"])
                self.clob_http_body_read.add(sample["body_read_ms"])
                self.clob_http_timing.add(
                    submitted=submitted,
                    worker_started=sample["worker_started_monotonic"],
                    worker_completed=sample["worker_completed_monotonic"],
                    coroutine_completed=coroutine_completed,
                    ttfb_ms=sample["socket_ttfb_ms"],
                )
            except Exception as exc:  # noqa: BLE001
                self.event("warning", f"CLOB probe failed: {type(exc).__name__}")
            await asyncio.sleep(float(self.cfg.feeds.get("http_probe_seconds", 5)))

    async def _loop_lag_loop(self) -> None:
        interval = 0.1
        loop = asyncio.get_running_loop()
        target = loop.time() + interval
        while True:
            await asyncio.sleep(max(0.0, target - loop.time()))
            now = loop.time()
            self.event_loop_lag.add(max(0.0, now - target) * 1000)
            target = max(target + interval, now)

    def _window_exposure_usd(self, condition_id: str) -> float:
        return (
            self.shadow_committed_by_condition.get(condition_id, 0.0)
            + self.shadow_pending_by_condition.get(condition_id, 0.0)
        )

    def _evaluate(self) -> Decision:
        if not self.market or not self.books:
            return Decision("WAIT", None, None, None, None, None, 0, ("no_active_market",), {})
        if self.external_halt_reason:
            return Decision(
                "WAIT", None, None, None, None, None, 0,
                (self.external_halt_reason,), {"portfolio_risk_gate": False},
            )
        return evaluate_entry(
            self.market,
            self.books,
            self.signal,
            time.time(),
            self.cfg.strategy,
            self.cfg.risk,
            self.shadow_orders_this_window,
            float(self.account_state.get("daily_pnl_usd", 0.0)),
            self._window_exposure_usd(self.market.condition_id),
        )

    def _evaluate_hot(self) -> None:
        """Evaluate immediately on a feed/book event, never on the state timer."""
        began = time.perf_counter()
        decision = self._evaluate()
        observer_decision = self._evaluate_observer()
        self.decision_compute.add((time.perf_counter() - began) * 1000)
        self.last_decision = decision
        self.observer_last_decision = observer_decision
        if self.market:
            condition_id = self.market.condition_id
            signature = tuple(decision.reasons or ("candidate",))
            now_mono = time.monotonic()
            last_ts, last_signature = self.last_gate_sample.get(
                condition_id,
                (0.0, ()),
            )
            if signature != last_signature or now_mono - last_ts >= 1.0:
                counter = self.gate_counts.setdefault(condition_id, Counter())
                for reason in signature:
                    counter[reason] += 1
                self.last_gate_sample[condition_id] = (now_mono, signature)
        self._record_candidate(decision)
        self._record_observer_candidate(observer_decision)

    def _evaluate_observer(self) -> Decision:
        """Evaluate the challenger independently of the frozen champion account."""
        if not self.observer or not self.market or not self.books:
            return Decision(
                "WAIT", None, None, None, None, None, 0,
                ("observer_disabled_or_no_market",), {},
            )
        strategy = {
            **self.cfg.strategy,
            "min_selected_probability": self.observer_cfg[
                "min_selected_probability"
            ],
            "max_spread": self.observer_cfg["max_spread"],
        }
        virtual_order = self.observer_cfg["virtual_order_usd"]
        risk = {
            **self.cfg.risk,
            "max_order_usd": virtual_order,
            "max_window_usd": virtual_order,
            "max_daily_loss_usd": 1_000_000,
            "max_orders_per_window": 1,
            "minimum_cash_reserve_usd": 0,
        }
        return evaluate_entry(
            self.market,
            self.books,
            self.signal,
            time.time(),
            strategy,
            risk,
            self.observer_orders_this_window,
            0.0,
            0.0,
        )

    def _record_observer_candidate(self, decision: Decision) -> None:
        if decision.action != "CANDIDATE" or not self.market or not self.observer:
            return
        market = self.market
        if market.condition_id in self.observer_seen_conditions:
            return
        self.observer_seen_conditions.add(market.condition_id)
        self.observer_orders_this_window = 1
        signal_monotonic = time.monotonic()
        task = asyncio.create_task(
            self._simulate_observer_candidate(market, decision, signal_monotonic),
            name=f"observer-fill-{self.cfg.asset}-{market.start_epoch}",
        )
        self.observer_execution_tasks.add(task)
        task.add_done_callback(self._observer_execution_done)
        self.event(
            "info",
            (
                f"observer candidate {decision.outcome}: "
                f"fair={float(decision.fair_probability):.3f} "
                f"edge={float(decision.net_edge):.4f}"
            ),
            outcome=decision.outcome,
        )

    def _observer_execution_done(self, task: asyncio.Task) -> None:
        self.observer_execution_tasks.discard(task)
        self._task_done(task)

    def _record_candidate(self, decision: Decision) -> None:
        if decision.action != "CANDIDATE" or not self.market:
            return
        now_mono = time.monotonic()
        cooldown = float(self.cfg.strategy.get("entry_cooldown_seconds", 12))
        if now_mono - self.last_shadow_entry_monotonic < cooldown:
            return
        market = self.market
        event = {
            "ts": _utc_now(),
            "mode": "shadow-v4-multi",
            "market": market.to_dict(),
            "decision": decision.to_dict(),
        }
        self._queue_audit(event)
        self.last_shadow_entry_monotonic = now_mono
        self.shadow_orders_this_window += 1
        reserved_budget = float(decision.order_usd)
        self.shadow_pending_by_condition[market.condition_id] = (
            self.shadow_pending_by_condition.get(market.condition_id, 0.0)
            + reserved_budget
        )
        task = asyncio.create_task(
            self._simulate_candidate(market, decision, now_mono),
            name=f"shadow-fill-{market.start_epoch}-{self.shadow_orders_this_window}",
        )
        self.execution_tasks.add(task)
        self.execution_reservations[task] = (market.condition_id, reserved_budget)
        task.add_done_callback(self._execution_done)
        self.event(
            "info",
            f"shadow intent {decision.outcome}: edge={decision.net_edge:.4f}",
            outcome=decision.outcome,
        )

    def _execution_done(self, task: asyncio.Task) -> None:
        self.execution_tasks.discard(task)
        reservation = self.execution_reservations.pop(task, None)
        if reservation:
            condition_id, amount = reservation
            self.shadow_pending_by_condition[condition_id] = max(
                0.0,
                self.shadow_pending_by_condition.get(condition_id, 0.0) - amount,
            )
        self._task_done(task)

    async def _simulate_candidate(
        self,
        market: FastMarket,
        decision: Decision,
        signal_monotonic: float,
    ) -> None:
        if signal_value_missing(decision):
            return
        tick = max(0.0001, market.tick_size)
        extra_ticks = int(self.cfg.strategy.get("execution_slippage_ticks", 1))
        limit_price = min(
            0.99,
            math.ceil((float(decision.ask) + extra_ticks * tick) / tick - 1e-9) * tick,
        )
        limit_price = round(limit_price, 10)
        unit_cost_at_limit = limit_price + crypto_fee_per_share(limit_price, market.fee_rate)
        target_budget = min(
            float(decision.order_usd),
            float(self.cfg.risk.get("max_order_usd", decision.order_usd)),
            float(self.cfg.risk.get("max_window_usd", decision.order_usd)),
        )
        target_shares = math.floor(target_budget / unit_cost_at_limit * 10_000) / 10_000
        pre_reject = None
        if target_shares + 1e-9 < market.min_order_size:
            target_shares = market.min_order_size
            pre_reject = "minimum_order_exceeds_cash_cap_after_fee_and_slippage"
        locked = target_shares * unit_cost_at_limit
        http = self.clob_http.state()
        outbound_delay = max(1.0, float(http.get("p50_ms") or 0.0) / 2)
        platform_delay = float(market.taker_delay_ms)
        due_ts = time.time() + (platform_delay + outbound_delay) / 1000
        intent_id = f"{self.ledger.run_id}-{market.start_epoch}-{time.time_ns()}"
        intent = {
            "intent_id": intent_id,
            "condition_id": market.condition_id,
            "created_ts": time.time(),
            "due_ts": due_ts,
            "outcome": str(decision.outcome),
            "fair_probability": float(decision.fair_probability),
            "intended_ask": float(decision.ask),
            "limit_price": limit_price,
            "net_edge": float(decision.net_edge),
            "target_shares": target_shares,
            "locked_usd": locked,
            "platform_delay_ms": platform_delay,
            "outbound_delay_ms": outbound_delay,
            "decision_compute_ms": float(self.decision_compute.values[-1] or 0.0),
            "pre_reject_reason": pre_reject,
            "details": {
                "market": market.to_dict(),
                "decision": decision.to_dict(),
                "order_type": "FAK",
                "simulation": "baseline",
            },
        }
        row = await asyncio.to_thread(
            self.ledger.create_intent,
            intent,
            float(self.cfg.risk.get("minimum_cash_reserve_usd", 0.0)),
        )
        if row["status"] != "submitted":
            self.account_state = await asyncio.to_thread(self.ledger.state, self._current_marks())
            self.event("warning", f"shadow intent rejected: {row['reject_reason']}")
            return

        await asyncio.sleep(max(0.0, due_ts - time.time()))
        if (
            not self.market
            or not self.books
            or self.market.condition_id != market.condition_id
        ):
            await asyncio.to_thread(
                self.ledger.finalize_fill,
                intent_id,
                None,
                "market_changed_before_simulated_arrival",
            )
            self.account_state = await asyncio.to_thread(self.ledger.state, self._current_marks())
            return

        now_mono = time.monotonic()
        if not self.books.healthy(
            float(self.cfg.strategy.get("max_clob_age_ms", 500)),
            float(self.cfg.strategy.get("max_spread", 0.08)),
            now_mono,
        ):
            await asyncio.to_thread(
                self.ledger.finalize_fill,
                intent_id,
                None,
                "book_unhealthy_at_simulated_arrival",
            )
            self.account_state = await asyncio.to_thread(self.ledger.state, self._current_marks())
            self.event("warning", "shadow FAK rejected: unhealthy arrival book")
            return

        token_id = market.token_for(str(decision.outcome))
        book = await self._fetch_arrival_book(token_id)
        if book is None:
            await asyncio.to_thread(
                self.ledger.finalize_fill,
                intent_id,
                None,
                "arrival_depth_snapshot_unavailable",
            )
            self.account_state = await asyncio.to_thread(self.ledger.state, self._current_marks())
            self.event("warning", "shadow FAK rejected: arrival snapshot unavailable")
            return
        remaining = target_shares
        depth: list[dict[str, float]] = []
        notional = 0.0
        fee = 0.0
        slippage = 0.0
        for price, available in sorted(book.asks.items()):
            if price > limit_price + 1e-9 or remaining <= 1e-9:
                break
            quantity = min(remaining, available)
            if quantity <= 0:
                continue
            depth.append({"price": price, "available": available, "filled": quantity})
            notional += quantity * price
            fee += quantity * crypto_fee_per_share(price, market.fee_rate)
            slippage += quantity * max(0.0, price - float(decision.ask))
            remaining -= quantity
        shares = target_shares - remaining
        if shares + 1e-9 < market.min_order_size:
            await asyncio.to_thread(
                self.ledger.finalize_fill,
                intent_id,
                None,
                "fak_depth_below_minimum_order",
            )
            self.account_state = await asyncio.to_thread(self.ledger.state, self._current_marks())
            self.event("warning", "shadow FAK rejected: insufficient depth")
            return

        average_price = notional / shares
        fill = {
            "executed_ts": time.time(),
            "shares": shares,
            "average_price": average_price,
            "notional_usd": notional,
            "fee_usd": fee,
            "total_cost_usd": notional + fee,
            "slippage_usd": slippage,
            "latency_ms": (time.monotonic() - signal_monotonic) * 1000,
            "depth": depth,
        }
        saved = await asyncio.to_thread(self.ledger.finalize_fill, intent_id, fill)
        self.shadow_committed_by_condition[market.condition_id] = (
            self.shadow_committed_by_condition.get(market.condition_id, 0.0)
            + float(saved["total_cost_usd"])
        )
        self.account_state = await asyncio.to_thread(self.ledger.state, self._current_marks())
        self.event(
            "info",
            (
                f"shadow {saved['status']} {shares:.4f} {decision.outcome} "
                f"@ {average_price:.3f}, latency={fill['latency_ms']:.1f}ms"
            ),
        )

    async def _simulate_observer_candidate(
        self,
        market: FastMarket,
        decision: Decision,
        signal_monotonic: float,
    ) -> None:
        """Record a realistic FAK arrival without reserving portfolio cash."""
        if not self.observer or signal_value_missing(decision):
            return
        tick = max(0.0001, market.tick_size)
        extra_ticks = int(self.cfg.strategy.get("execution_slippage_ticks", 1))
        limit_price = min(
            0.99,
            math.ceil((float(decision.ask) + extra_ticks * tick) / tick - 1e-9) * tick,
        )
        limit_price = round(limit_price, 10)
        unit_cost_at_limit = limit_price + crypto_fee_per_share(
            limit_price, market.fee_rate
        )
        target_budget = float(self.observer_cfg["virtual_order_usd"])
        target_shares = math.floor(target_budget / unit_cost_at_limit * 10_000) / 10_000
        due_ts = time.time() + (
            float(market.taker_delay_ms)
            + max(1.0, float(self.clob_http.state().get("p50_ms") or 0.0) / 2)
        ) / 1000
        saved = await asyncio.to_thread(
            self.observer.create_candidate,
            {
                "condition_id": market.condition_id,
                "slug": market.slug,
                "start_epoch": market.start_epoch,
                "end_epoch": market.end_epoch,
                "outcome": decision.outcome,
                "fair_probability": decision.fair_probability,
                "intended_ask": decision.ask,
                "limit_price": limit_price,
                "net_edge": decision.net_edge,
                "target_shares": target_shares,
                "created_ts": time.time(),
                "due_ts": due_ts,
                "details": {
                    "market": market.to_dict(),
                    "decision": decision.to_dict(),
                    "order_type": "FAK",
                    "simulation": "independent-zero-capital-observer",
                },
            },
        )
        observation_id = str(saved["observation_id"])
        if saved["status"] != "submitted":
            self.observer_state = await asyncio.to_thread(self.observer.state)
            return
        if target_shares + 1e-9 < market.min_order_size:
            await asyncio.to_thread(
                self.observer.reject,
                observation_id,
                "minimum_order_exceeds_virtual_order_cap",
            )
            self.observer_state = await asyncio.to_thread(self.observer.state)
            return

        await asyncio.sleep(max(0.0, due_ts - time.time()))
        if (
            not self.market
            or not self.books
            or self.market.condition_id != market.condition_id
        ):
            await asyncio.to_thread(
                self.observer.reject,
                observation_id,
                "market_changed_before_simulated_arrival",
            )
            self.observer_state = await asyncio.to_thread(self.observer.state)
            return
        if not self.books.healthy(
            float(self.cfg.strategy.get("max_clob_age_ms", 500)),
            float(self.observer_cfg["max_spread"]),
            time.monotonic(),
        ):
            await asyncio.to_thread(
                self.observer.reject,
                observation_id,
                "book_unhealthy_at_simulated_arrival",
            )
            self.observer_state = await asyncio.to_thread(self.observer.state)
            return

        token_id = market.token_for(str(decision.outcome))
        book = await self._fetch_arrival_book(token_id)
        if book is None:
            await asyncio.to_thread(
                self.observer.reject,
                observation_id,
                "arrival_depth_snapshot_unavailable",
            )
            self.observer_state = await asyncio.to_thread(self.observer.state)
            return

        remaining = target_shares
        depth: list[dict[str, float]] = []
        notional = 0.0
        fee = 0.0
        slippage = 0.0
        for price, available in sorted(book.asks.items()):
            if price > limit_price + 1e-9 or remaining <= 1e-9:
                break
            quantity = min(remaining, available)
            if quantity <= 0:
                continue
            depth.append({"price": price, "available": available, "filled": quantity})
            notional += quantity * price
            fee += quantity * crypto_fee_per_share(price, market.fee_rate)
            slippage += quantity * max(0.0, price - float(decision.ask))
            remaining -= quantity
        shares = target_shares - remaining
        if shares + 1e-9 < market.min_order_size:
            await asyncio.to_thread(
                self.observer.reject,
                observation_id,
                "fak_depth_below_minimum_order",
            )
            self.observer_state = await asyncio.to_thread(self.observer.state)
            return

        average_price = notional / shares
        fill = {
            "executed_ts": time.time(),
            "shares": shares,
            "average_price": average_price,
            "notional_usd": notional,
            "fee_usd": fee,
            "total_cost_usd": notional + fee,
            "slippage_usd": slippage,
            "latency_ms": (time.monotonic() - signal_monotonic) * 1000,
            "details": {"depth": depth, "arrival_limit_price": limit_price},
        }
        await asyncio.to_thread(self.observer.fill, observation_id, fill)
        self.observer_state = await asyncio.to_thread(self.observer.state)
        self.event(
            "info",
            (
                f"observer filled {shares:.4f} {decision.outcome} "
                f"@ {average_price:.3f}, latency={fill['latency_ms']:.1f}ms"
            ),
        )

    async def _fetch_arrival_book(self, token_id: str) -> LocalBook | None:
        if self.session is None:
            return None
        try:
            async with self.session.get(
                f"{self.cfg.endpoints['clob'].rstrip('/')}/book",
                params={"token_id": token_id},
            ) as response:
                response.raise_for_status()
                raw = await response.json(content_type=None)
            book = LocalBook(token_id)
            if not book.apply_snapshot(raw, time.monotonic()) or not book.synchronized:
                return None
            return book
        except Exception as exc:  # noqa: BLE001
            self.event("warning", f"arrival book fetch failed: {type(exc).__name__}")
            return None

    async def _settlement_loop(self, session: aiohttp.ClientSession) -> None:
        while True:
            now = time.time()
            positions = await asyncio.to_thread(self.ledger.open_positions)
            changed = False
            for position in positions:
                if now < float(position["end_epoch"]):
                    continue
                if (
                    self.signal.chainlink
                    and self.signal.chainlink.source_ts >= float(position["end_epoch"])
                    and position.get("open_price") is not None
                ):
                    provisional = (
                        "UP"
                        if self.signal.chainlink.value >= float(position["open_price"])
                        else "DOWN"
                    )
                    await asyncio.to_thread(
                        self.ledger.set_provisional_outcome,
                        position["position_id"],
                        provisional,
                    )
                if now < self.settlement_retry_after.get(position["position_id"], 0):
                    continue
                official = await self._fetch_official_outcome(session, str(position["slug"]))
                if official:
                    settled = await asyncio.to_thread(
                        self.ledger.settle_position,
                        position["position_id"],
                        official,
                    )
                    changed = True
                    self.event(
                        "info",
                        f"official settlement {official}: {float(settled['pnl_usd']):+.2f} USD",
                    )
                    self.settlement_retry_after.pop(position["position_id"], None)
                else:
                    self.settlement_retry_after[position["position_id"]] = now + 10
            if changed:
                self.account_state = await asyncio.to_thread(
                    self.ledger.state,
                    self._current_marks(),
                )
            observer_changed = False
            if self.observer:
                observations = await asyncio.to_thread(
                    self.observer.open_observations
                )
                for observation in observations:
                    if now < float(observation["end_epoch"]):
                        continue
                    observation_id = str(observation["observation_id"])
                    if now < self.observer_settlement_retry_after.get(
                        observation_id, 0
                    ):
                        continue
                    official = await self._fetch_official_outcome(
                        session, str(observation["slug"])
                    )
                    if official:
                        settled = await asyncio.to_thread(
                            self.observer.settle,
                            observation_id,
                            official,
                        )
                        observer_changed = True
                        self.observer_settlement_retry_after.pop(
                            observation_id, None
                        )
                        self.event(
                            "info",
                            (
                                f"observer official settlement {official}: "
                                f"{float(settled['pnl_usd']):+.2f} normalized USD"
                            ),
                        )
                    else:
                        self.observer_settlement_retry_after[observation_id] = now + 10
                if observer_changed:
                    self.observer_state = await asyncio.to_thread(
                        self.observer.state
                    )
            await asyncio.sleep(2)

    async def _fetch_official_outcome(
        self,
        session: aiohttp.ClientSession,
        slug: str,
    ) -> str | None:
        for endpoint in ("markets", "events"):
            try:
                async with session.get(
                    f"{self.cfg.endpoints['gamma'].rstrip('/')}/{endpoint}",
                    params={"slug": slug},
                ) as response:
                    response.raise_for_status()
                    raw = select_any_market_payload(await response.json(content_type=None))
                    if raw:
                        outcome = resolved_fast_outcome(raw)
                        if outcome:
                            return outcome
            except Exception as exc:  # noqa: BLE001
                self.event("warning", f"official settlement lookup failed: {type(exc).__name__}")
        return None

    def _current_marks(self) -> dict[str, float]:
        marks: dict[str, float] = {}
        if not self.market or not self.books:
            return marks
        for position in self.account_state.get("open_positions", []):
            if position.get("condition_id") != self.market.condition_id:
                continue
            book = self.books.up if position.get("outcome") == "UP" else self.books.down
            if book.best_bid is not None:
                marks[self.market.condition_id] = book.best_bid
        return marks

    async def _flush_gate_counts(self, condition_id: str | None = None) -> None:
        targets = (
            [condition_id]
            if condition_id is not None
            else list(self.gate_counts)
        )
        for target in targets:
            counts = self.gate_counts.pop(target, None)
            if counts:
                await asyncio.to_thread(
                    self.ledger.flush_gate_counts,
                    target,
                    dict(counts),
                )

    async def _review_loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            http = self.clob_http.state()
            compute = self.decision_compute.state()
            loop_lag = self.event_loop_lag.state()
            runtime = {
                "network": {
                    "clob_http": http,
                    "clob_http_breakdown": {
                        "queue_wait": self.clob_http_queue_wait.state(),
                        "socket_ttfb": self.clob_http_socket_ttfb.state(),
                        "body_read": self.clob_http_body_read.state(),
                    },
                    "clob_http_timing": self.clob_http_timing.state(),
                    "decision_compute": compute,
                    "event_loop_lag": loop_lag,
                    "platform_taker_delay_ms": (
                        self.market.taker_delay_ms if self.market else None
                    ),
                    "platform_taker_delay_enabled": (
                        self.market.taker_delay_enabled if self.market else None
                    ),
                    "platform_taker_delay_verified": (
                        self.market.taker_delay_verified if self.market else False
                    ),
                    "under_50ms": (
                        http["p95_ms"] is not None
                        and compute["p95_ms"] is not None
                        and loop_lag["p95_ms"] is not None
                        and http["p95_ms"] + compute["p95_ms"] + loop_lag["p95_ms"] <= 50
                    ),
                    "reconnects": dict(self.reconnects),
                },
                "feed_rejects": dict(self.feed_rejects),
                "state_age_seconds": 0,
                "observer": dict(self.observer_state),
            }
            created = await asyncio.to_thread(
                run_due_reviews,
                self.ledger,
                self.cfg.review_path,
                runtime,
            )
            self.review_state = await asyncio.to_thread(self.ledger.recent_reviews, 1)
            for report in created:
                self.event(
                    "info",
                    f"{report['review_type']} review: {report['verdict']}",
                )
                if report["verdict"] == "HALT_AND_INVESTIGATE":
                    self.event("error", "review detected a ledger invariant failure")
                    self.stop.set()
            await asyncio.sleep(60)

    def _queue_audit(self, event: dict[str, Any]) -> None:
        try:
            self.audit_queue.put_nowait(event)
        except asyncio.QueueFull:
            # An incomplete audit trail is a hard fault.  Stop so the service
            # supervisor restarts the process instead of hiding data loss.
            self.event("error", "audit queue full")
            self.stop.set()

    async def _audit_loop(self) -> None:
        while True:
            event = await self.audit_queue.get()
            try:
                await asyncio.to_thread(append_decision, self.cfg.decisions_path, event)
            finally:
                self.audit_queue.task_done()

    def _state(self) -> dict[str, Any]:
        decision = self.last_decision or Decision(
            "WAIT", None, None, None, None, None, 0, ("awaiting_feed_event",), {}
        )
        now = time.time()
        market = self.market.to_dict() if self.market else None
        if market:
            market["seconds_remaining"] = max(0, self.market.end_epoch - now)
        http = self.clob_http.state()
        compute = self.decision_compute.state()
        loop_lag = self.event_loop_lag.state()
        taker_delay = self.market.taker_delay_ms if self.market else None
        taker_delay_verified = bool(self.market and self.market.taker_delay_verified)
        ack_floor = None
        if (
            taker_delay is not None
            and taker_delay_verified
            and http["p95_ms"] is not None
            and compute["p95_ms"] is not None
            and loop_lag["p95_ms"] is not None
        ):
            ack_floor = round(
                taker_delay + http["p95_ms"] + compute["p95_ms"] + loop_lag["p95_ms"],
                2,
            )
        return {
            "ready": self.market is not None,
            "asset": self.cfg.asset,
            "external_halt_reason": self.external_halt_reason,
            "mode": "SHADOW",
            "live_ready": False,
            "live_blockers": [
                "72h shadow sample not yet accepted",
                "user-channel fill/position reconciliation not yet enabled",
                "wallet credentials and canary approval absent",
            ],
            "started_at": self.started_at,
            "updated_at": _utc_now(),
            "market": market,
            "feeds": {
                **self.signal.feed_state(),
                "rejected_events": dict(self.feed_rejects),
            },
            "books": None if not self.books else {
                "up": self.books.up.to_dict(),
                "down": self.books.down.to_dict(),
            },
            "decision": decision.to_dict(),
            "network": {
                "clob_http": http,
                "clob_http_breakdown": {
                    "queue_wait": self.clob_http_queue_wait.state(),
                    "socket_ttfb": self.clob_http_socket_ttfb.state(),
                    "body_read": self.clob_http_body_read.state(),
                },
                "clob_http_timing": self.clob_http_timing.state(),
                "decision_compute": compute,
                "event_loop_lag": loop_lag,
                "platform_taker_delay_ms": taker_delay,
                "platform_taker_delay_enabled": (
                    self.market.taker_delay_enabled if self.market else None
                ),
                "platform_taker_delay_verified": taker_delay_verified,
                "estimated_taker_ack_floor_ms": ack_floor,
                "under_50ms": (
                    http["p95_ms"] is not None
                    and compute["p95_ms"] is not None
                    and loop_lag["p95_ms"] is not None
                    and http["p95_ms"] + compute["p95_ms"] + loop_lag["p95_ms"] <= 50
                ),
                "market_discovery_ms": self.market_discovery_ms,
                "reconnects": self.reconnects,
                "geoblock": self.geoblock,
            },
            "prefetch": None if not self.prefetched_market else {
                "slug": self.prefetched_market.slug,
                "connected": bool(
                    self.prefetched_books
                    and self.prefetched_books.up.update_count
                    and self.prefetched_books.down.update_count
                ),
                "taker_delay_ms": self.prefetched_market.taker_delay_ms,
                "taker_delay_enabled": self.prefetched_market.taker_delay_enabled,
                "taker_delay_verified": self.prefetched_market.taker_delay_verified,
            },
            "account": self.account_state,
            "reviews": self.review_state,
            "observer": {
                **self.observer_state,
                "enabled": bool(self.observer),
                "decision": (
                    self.observer_last_decision.to_dict()
                    if self.observer_last_decision else None
                ),
                "orders_this_window": self.observer_orders_this_window,
                "parameters": dict(self.observer_cfg),
                "independent_since": self.observer_state.get("started_ts"),
                "selection_bias_warning": (
                    "The rule came from 3 selected historical fills; only new "
                    "officially settled observations count toward promotion."
                ),
            },
            "shadow": {
                "orders_this_window": self.shadow_orders_this_window,
                "pending_executions": len(self.execution_tasks),
                "committed_usd_this_window": (
                    self.shadow_committed_by_condition.get(self.market.condition_id, 0.0)
                    if self.market else 0.0
                ),
                "pending_budget_usd": (
                    self.shadow_pending_by_condition.get(self.market.condition_id, 0.0)
                    if self.market else 0.0
                ),
                "window_exposure_usd": (
                    self._window_exposure_usd(self.market.condition_id)
                    if self.market else 0.0
                ),
                "campaign": f"FAST-V5 {self.cfg.asset} ${float(self.cfg.risk.get('initial_capital_usd', 0)):g} SHADOW",
            },
            "controls": {
                "initial_capital_usd": self.cfg.risk.get("initial_capital_usd"),
                "max_order_usd": self.cfg.risk.get("max_order_usd"),
                "max_window_usd": self.cfg.risk.get("max_window_usd"),
                "max_daily_loss_usd": self.cfg.risk.get("max_daily_loss_usd"),
                "minimum_cash_reserve_usd": self.cfg.risk.get(
                    "minimum_cash_reserve_usd"
                ),
                "max_orders_per_window": self.cfg.risk.get("max_orders_per_window"),
                "entry_cooldown_seconds": self.cfg.strategy.get("entry_cooldown_seconds"),
                "min_net_edge": self.cfg.strategy.get("min_net_edge"),
                "max_spread": self.cfg.strategy.get("max_spread"),
                "max_clob_age_ms": self.cfg.strategy.get("max_clob_age_ms"),
                "minimum_order_shares": self.market.min_order_size
                if self.market else None,
                "champion_frozen": True,
            },
            "logs": list(self.logs),
        }

    async def _state_loop(self) -> None:
        interval = max(0.1, float(self.cfg.feeds.get("state_flush_ms", 250)) / 1000)
        while True:
            try:
                now_mono = time.monotonic()
                if now_mono - self.last_ledger_refresh >= 1.0:
                    if self.market:
                        await asyncio.to_thread(
                            self.ledger.ensure_window,
                            self.market.to_dict(),
                            self.signal.open_price,
                            self.signal.open_source_ts,
                        )
                    self.account_state = await asyncio.to_thread(
                        self.ledger.state,
                        self._current_marks(),
                    )
                    if self.observer:
                        self.observer_state = await asyncio.to_thread(
                            self.observer.state
                        )
                    self.last_ledger_refresh = now_mono
                if now_mono - self.last_gate_flush >= 10.0:
                    await self._flush_gate_counts()
                    self.last_gate_flush = now_mono
                await asyncio.to_thread(write_state, self.cfg.state_path, self._state())
            except Exception as exc:  # noqa: BLE001
                self.event("error", f"state flush failed: {type(exc).__name__}: {exc}")
            await asyncio.sleep(interval)


def signal_value_missing(decision: Decision) -> bool:
    return decision.outcome is None or decision.ask is None or decision.fee_per_share is None


async def async_main(config_path: str) -> None:
    bot = FastBot(load_fast_config(config_path))
    loop = asyncio.get_running_loop()
    for sig in (os_signal.SIGINT, os_signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, bot.stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    await bot.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket fast-market shadow engine")
    parser.add_argument("--config", default="fast_config.yaml")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        asyncio.run(async_main(args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
