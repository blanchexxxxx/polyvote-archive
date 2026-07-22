"""Configuration for the isolated fast-market service."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class FastConfigError(ValueError):
    pass


@dataclass(frozen=True)
class FastConfig:
    mode: str
    asset: str
    window_seconds: int
    endpoints: dict[str, str]
    feeds: dict[str, Any]
    strategy: dict[str, Any]
    risk: dict[str, Any]
    live: dict[str, Any]
    storage: dict[str, str]
    path: Path = field(repr=False)

    @property
    def state_path(self) -> Path:
        p = Path(self.storage["state"])
        return p if p.is_absolute() else self.path.parent / p

    @property
    def decisions_path(self) -> Path:
        p = Path(self.storage["decisions"])
        return p if p.is_absolute() else self.path.parent / p

    @property
    def ledger_path(self) -> Path:
        p = Path(self.storage["ledger"])
        return p if p.is_absolute() else self.path.parent / p

    @property
    def review_path(self) -> Path:
        p = Path(self.storage["reviews"])
        return p if p.is_absolute() else self.path.parent / p


def _positive(raw: dict[str, Any], key: str, section: str) -> None:
    try:
        ok = float(raw[key]) > 0
    except (KeyError, TypeError, ValueError):
        ok = False
    if not ok:
        raise FastConfigError(f"{section}.{key} must be positive")


def load_fast_config(path: str | Path = "fast_config.yaml") -> FastConfig:
    path = Path(path).resolve()
    if not path.exists():
        raise FastConfigError(f"configuration not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    mode = str(raw.get("mode", "shadow")).lower()
    if mode not in {"shadow", "live"}:
        raise FastConfigError("mode must be shadow or live")

    endpoints = raw.get("endpoints") or {}
    for key in ("gamma", "clob", "market_ws", "rtds_ws", "geoblock"):
        if not endpoints.get(key):
            raise FastConfigError(f"endpoints.{key} is required")

    strategy = raw.get("strategy") or {}
    risk = raw.get("risk") or {}
    for key in ("max_clob_age_ms", "max_chainlink_age_ms", "min_net_edge"):
        _positive(strategy, key, "strategy")
    for key in (
        "initial_capital_usd",
        "max_order_usd",
        "max_window_usd",
        "max_daily_loss_usd",
    ):
        _positive(risk, key, "risk")

    live = raw.get("live") or {}
    if mode == "live" and not live.get("enabled"):
        raise FastConfigError("mode=live requires live.enabled=true")

    storage = raw.get("storage") or {}
    storage.setdefault("state", "fast_state.json")
    storage.setdefault("decisions", "fast_decisions_v4.jsonl")
    storage.setdefault("ledger", "fast_shadow_v4.db")
    storage.setdefault("reviews", "fast_reviews_v4.json")
    return FastConfig(
        mode=mode,
        asset=str(raw.get("asset", "BTC")).upper(),
        window_seconds=int(raw.get("window_seconds", 300)),
        endpoints={str(k): str(v) for k, v in endpoints.items()},
        feeds=raw.get("feeds") or {},
        strategy=strategy,
        risk=risk,
        live=live,
        storage=storage,
        path=path,
    )
