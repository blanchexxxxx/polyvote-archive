"""Standalone keyless observer for Polymarket complete-set arbitrage."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import yaml

from polyvote.fastarb import analyze_complete_sets, top_margins


@dataclass(frozen=True)
class MarketSpec:
    condition_id: str
    question: str
    slug: str
    tokens: tuple[str, str]
    outcomes: tuple[str, str]
    fee_rate: float
    min_order_size: float
    tick_size: float
    neg_risk: bool
    volume_24h: float


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _epoch_ms(value: Any) -> int | None:
    try:
        stamp = int(float(value))
    except (TypeError, ValueError):
        return None
    return stamp * 1000 if stamp < 10_000_000_000 else stamp


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


class PureArbScanner:
    def __init__(self, config_path: Path):
        self.config_path = config_path.resolve()
        self.root = self.config_path.parent
        self.cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if str(self.cfg.get("mode") or "").lower() != "shadow":
            raise ValueError("pure arbitrage scanner is shadow-only")
        self.scan_cfg = self.cfg.get("scan") or {}
        self.economics = self.cfg.get("economics") or {}
        storage = self.cfg.get("storage") or {}
        self.state_path = self.root / str(storage.get("state", "fast_arb_state.json"))
        self.observations_path = self.root / str(
            storage.get("observations", "fast_arb_observations.jsonl")
        )
        endpoints = self.cfg.get("endpoints") or {}
        self.gamma = str(endpoints.get("gamma", "https://gamma-api.polymarket.com")).rstrip("/")
        self.clob = str(endpoints.get("clob", "https://clob.polymarket.com")).rstrip("/")
        self.markets: list[MarketSpec] = []
        self.last_refresh = 0.0
        self.scan_count = 0
        self._last_recorded: dict[tuple[str, str], tuple[float, float]] = {}

    async def discover(self, session: aiohttp.ClientSession) -> None:
        limit = int(self.scan_cfg.get("max_markets", 200))
        async with session.get(
            f"{self.gamma}/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": str(limit),
                "order": "volume24hr",
                "ascending": "false",
            },
        ) as response:
            response.raise_for_status()
            rows = await response.json()
        candidates: list[tuple[dict[str, Any], tuple[str, str], tuple[str, str]]] = []
        for raw in rows if isinstance(rows, list) else []:
            tokens = tuple(str(item) for item in _json_list(raw.get("clobTokenIds")))
            outcomes = tuple(str(item) for item in _json_list(raw.get("outcomes")))
            condition_id = str(raw.get("conditionId") or "")
            if (
                len(tokens) != 2
                or len(outcomes) != 2
                or not condition_id
                or not raw.get("acceptingOrders", False)
            ):
                continue
            candidates.append((raw, tokens, outcomes))  # type: ignore[arg-type]

        semaphore = asyncio.Semaphore(int(self.scan_cfg.get("metadata_concurrency", 20)))

        async def enrich(item: tuple[dict[str, Any], tuple[str, str], tuple[str, str]]) -> MarketSpec | None:
            raw, tokens, outcomes = item
            condition_id = str(raw.get("conditionId"))
            try:
                async with semaphore:
                    async with session.get(f"{self.clob}/clob-markets/{condition_id}") as response:
                        response.raise_for_status()
                        info = await response.json()
                fee_details = info.get("fd")
                if not isinstance(fee_details, dict) or fee_details.get("r") is None:
                    return None
                return MarketSpec(
                    condition_id=condition_id,
                    question=str(raw.get("question") or ""),
                    slug=str(raw.get("slug") or ""),
                    tokens=tokens,
                    outcomes=outcomes,
                    fee_rate=float(fee_details["r"]),
                    min_order_size=float(info.get("mos") or raw.get("orderMinSize") or 0),
                    tick_size=float(info.get("mts") or raw.get("orderPriceMinTickSize") or 0.01),
                    neg_risk=bool(raw.get("negRisk", False)),
                    volume_24h=float(raw.get("volume24hr") or 0),
                )
            except (aiohttp.ClientError, ValueError, TypeError):
                return None

        enriched = await asyncio.gather(*(enrich(item) for item in candidates))
        self.markets = [market for market in enriched if market is not None]
        self.last_refresh = time.monotonic()

    async def _fetch_books(self, session: aiohttp.ClientSession) -> dict[str, dict[str, Any]]:
        tokens = [token for market in self.markets for token in market.tokens]
        batch_size = max(2, int(self.scan_cfg.get("book_batch_size", 100)))
        books: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(tokens), batch_size):
            payload = [{"token_id": token} for token in tokens[offset:offset + batch_size]]
            async with session.post(f"{self.clob}/books", json=payload) as response:
                response.raise_for_status()
                rows = await response.json()
            received_ms = int(time.time() * 1000)
            for row in rows if isinstance(rows, list) else []:
                token = str(row.get("asset_id") or "")
                if token:
                    books[token] = dict(row) | {"_received_ms": received_ms}
        return books

    def _record(self, payload: dict[str, Any]) -> None:
        self.observations_path.parent.mkdir(parents=True, exist_ok=True)
        with self.observations_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")

    async def scan_once(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        if not self.markets:
            await self.discover(session)
        began = time.perf_counter()
        books = await self._fetch_books(session)
        now_ms = int(time.time() * 1000)
        max_age = int(self.scan_cfg.get("max_book_age_ms", 2500))
        max_skew = int(self.scan_cfg.get("max_pair_skew_ms", 750))
        best: list[dict[str, Any]] = []
        found: list[dict[str, Any]] = []
        skipped_stale = 0
        skipped_missing = 0
        for market in self.markets:
            first, second = (books.get(token) for token in market.tokens)
            if first is None or second is None:
                skipped_missing += 1
                continue
            stamps = (_epoch_ms(first.get("timestamp")), _epoch_ms(second.get("timestamp")))
            received = (_epoch_ms(first.get("_received_ms")), _epoch_ms(second.get("_received_ms")))
            # CLOB book timestamps describe the last book mutation, so a quiet
            # but freshly requested book can legitimately carry an old source
            # timestamp. Freshness is therefore based on when this process
            # received the POST /books response; source stamps remain audit data.
            if (
                any(stamp is None for stamp in received)
                or any(now_ms - int(stamp) > max_age or int(stamp) > now_ms + 1000 for stamp in received)
                or abs(int(received[0]) - int(received[1])) > max_skew
            ):
                skipped_stale += 1
                continue
            margins = top_margins(
                first,
                second,
                fee_rate=market.fee_rate,
                execution_buffer_per_set=float(
                    self.economics.get("execution_buffer_per_set", 0.002)
                ),
            )
            best.append({
                "condition_id": market.condition_id,
                "question": market.question,
                "slug": market.slug,
                "fee_rate": market.fee_rate,
                "neg_risk": market.neg_risk,
                "buy_margin_per_set": margins["buy_set"],
                "sell_margin_per_set": margins["sell_set"],
                "volume_24h": market.volume_24h,
            })
            opportunities = analyze_complete_sets(
                market.condition_id,
                first,
                second,
                fee_rate=market.fee_rate,
                min_order_size=market.min_order_size,
                max_capital_usd=float(self.economics.get("max_capital_usd", 30)),
                min_net_profit_usd=float(self.economics.get("min_net_profit_usd", 0.05)),
                min_net_return=float(self.economics.get("min_net_return", 0.0025)),
                execution_buffer_per_set=float(
                    self.economics.get("execution_buffer_per_set", 0.002)
                ),
                fixed_cost_buffer_usd=float(
                    self.economics.get("fixed_cost_buffer_usd", 0.05)
                ),
            )
            for opportunity in opportunities:
                item = opportunity.to_dict() | {
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "question": market.question,
                    "slug": market.slug,
                    "outcomes": list(market.outcomes),
                    "fee_rate": market.fee_rate,
                    "neg_risk": market.neg_risk,
                    "book_source_timestamps_ms": [
                        int(stamp) if stamp is not None else None for stamp in stamps
                    ],
                    "book_received_timestamps_ms": [int(received[0]), int(received[1])],
                    "execution_model": "same-batch-snapshot; two-leg execution is not atomic",
                    "mode": "SHADOW",
                }
                found.append(item)
                key = (market.condition_id, opportunity.direction)
                signature = (round(float(opportunity.outcome_prices[0]), 4), round(float(opportunity.outcome_prices[1]), 4))
                last_seen, last_signature = self._last_recorded.get(key, (0.0, (-1.0, -1.0)))
                if signature != last_signature or time.monotonic() - last_seen >= 60:
                    self._record(item)
                    self._last_recorded[key] = (time.monotonic(), signature)

        best.sort(
            key=lambda item: max(
                item["buy_margin_per_set"] if item["buy_margin_per_set"] is not None else -9,
                item["sell_margin_per_set"] if item["sell_margin_per_set"] is not None else -9,
            ),
            reverse=True,
        )
        self.scan_count += 1
        state = {
            "schema": "fast-pure-arb-shadow-state-v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "SHADOW",
            "ready": True,
            "live_execution_available": False,
            "markets_discovered": len(self.markets),
            "markets_with_books": len(books) // 2,
            "markets_skipped_missing": skipped_missing,
            "markets_skipped_stale": skipped_stale,
            "opportunities": found,
            "opportunity_count": len(found),
            "best_fee_adjusted_margins": best[:20],
            "scan_count": self.scan_count,
            "scan_ms": round((time.perf_counter() - began) * 1000, 3),
            "parameters": dict(self.economics),
            "warning": "A complete-set price inequality is not atomic two-leg execution.",
        }
        _atomic_json(self.state_path, state)
        return state

    async def run(self, *, once: bool) -> None:
        timeout = aiohttp.ClientTimeout(total=20, connect=8)
        connector = aiohttp.TCPConnector(limit=40, ttl_dns_cache=300)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            while True:
                if (
                    not self.markets
                    or time.monotonic() - self.last_refresh
                    >= float(self.scan_cfg.get("market_refresh_seconds", 300))
                ):
                    await self.discover(session)
                state = await self.scan_once(session)
                print(json.dumps({
                    "generated_at": state["generated_at"],
                    "markets": state["markets_discovered"],
                    "opportunities": state["opportunity_count"],
                    "scan_ms": state["scan_ms"],
                    "best": state["best_fee_adjusted_margins"][:3],
                }, ensure_ascii=False))
                if once:
                    return
                await asyncio.sleep(float(self.scan_cfg.get("interval_seconds", 2)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("fast_arb.yaml"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(PureArbScanner(args.config).run(once=args.once))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
