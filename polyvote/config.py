"""配置加载 + 软校验。

设计要点:未知模型只 WARN 不 raise。硬 allowlist 会在模型升级时
把合法模型挡在门外 —— mory 的 Fable 5 400 bug 就是这么来的。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Voter:
    id: str
    engine: str
    model: str
    effort: str | None = None


@dataclass(frozen=True)
class Risk:
    dry_run: bool
    max_notional_per_trade_usd: float
    max_notional_per_day_usd: float
    max_position_per_market_usd: float
    max_open_positions: int
    min_decider_confidence: int
    min_vote_agreement: int
    max_price_for_buy: float
    min_price_for_buy: float
    # 一票的 fair_price 要偏离市价多少才算数。低于这个值 = 噪声,记弃权。
    min_edge: float
    # 单票信心下限。低于此值强制弃权 —— 无知不是 edge。
    min_voter_confidence: int
    # 投票人之间估值分歧超过这个值 → 拒单。分歧大说明没人真懂,
    # 此时"平均值"是假的精确。
    max_fair_price_spread: float


@dataclass
class Config:
    voters: list[Voter]
    decider: Voter
    researcher: Voter
    engines: dict[str, dict[str, Any]]
    risk: Risk
    market_filter: dict[str, Any]
    loop: dict[str, Any]
    execution: dict[str, Any]
    data: dict[str, Any]
    research: dict[str, Any] = field(default_factory=dict)
    known_models: dict[str, list[str]] = field(default_factory=dict)
    path: Path | None = None

    @property
    def private_key(self) -> str | None:
        return os.environ.get("POLYVOTE_PRIVATE_KEY") or None

    def engine_cfg(self, engine: str) -> dict[str, Any]:
        try:
            return self.engines[engine]
        except KeyError:
            raise ConfigError(
                f"引擎 {engine!r} 未在 config.yaml 的 engines: 下定义"
            ) from None


class ConfigError(Exception):
    pass


_VALID_ENGINES = {"claude", "codex"}
_VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max", None}


def _mk_voter(raw: dict[str, Any], where: str) -> Voter:
    for key in ("engine", "model"):
        if not raw.get(key):
            raise ConfigError(f"{where}: 缺少必填字段 {key!r}")
    engine = raw["engine"]
    if engine not in _VALID_ENGINES:
        raise ConfigError(
            f"{where}: engine={engine!r} 无效,只支持 {sorted(_VALID_ENGINES)}"
        )
    effort = raw.get("effort")
    if effort not in _VALID_EFFORTS:
        raise ConfigError(
            f"{where}: effort={effort!r} 无效,只支持 {sorted(x for x in _VALID_EFFORTS if x)}"
        )
    return Voter(
        id=raw.get("id") or raw["model"],
        engine=engine,
        model=raw["model"],
        effort=effort,
    )


def load(path: str | Path = "config.yaml") -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"找不到配置文件: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    voters = [
        _mk_voter(v, f"voters[{i}]") for i, v in enumerate(raw.get("voters") or [])
    ]
    if not voters:
        raise ConfigError("voters: 至少要有一个投票人")

    decider_raw = raw.get("decider")
    if not decider_raw:
        raise ConfigError("decider: 未配置")
    decider = _mk_voter(decider_raw, "decider")

    researcher_raw = raw.get("researcher")
    if not researcher_raw:
        raise ConfigError("researcher: 未配置")
    researcher = _mk_voter(researcher_raw, "researcher")
    if researcher.engine != "claude":
        # 研究员必须能上网。codex exec 跑的是 -s read-only 沙箱,不开网。
        raise ConfigError(
            f"researcher.engine 目前只支持 claude (拿到的是 {researcher.engine!r}) —— "
            "研究员必须能用 WebSearch,而 codex 这边走的是 read-only 沙箱"
        )

    known = raw.get("known_models") or {}
    for v in [*voters, decider, researcher]:
        allowed = known.get(v.engine) or []
        if allowed and v.model not in allowed:
            # 刻意只 WARN。新模型上线时不该被这张表挡住。
            log.warning(
                "模型 %r 不在 known_models.%s 里 —— 如果是新上线的模型这很正常,"
                "照常执行。若是拼写错误,调用时会以引擎报错的形式暴露。",
                v.model,
                v.engine,
            )

    ids = [v.id for v in voters]
    if len(set(ids)) != len(ids):
        raise ConfigError(f"voters 的 id 有重复: {ids}")

    try:
        risk = Risk(**raw["risk"])
    except (KeyError, TypeError) as e:
        raise ConfigError(f"risk: 配置不完整或有多余字段 — {e}") from e

    cfg = Config(
        voters=voters,
        decider=decider,
        researcher=researcher,
        engines=raw.get("engines") or {},
        risk=risk,
        market_filter=raw.get("market_filter") or {},
        loop=raw.get("loop") or {},
        execution=raw.get("execution") or {},
        data=raw.get("data") or {},
        research=raw.get("research") or {},
        known_models=known,
        path=path,
    )

    for v in [*voters, decider, researcher]:
        cfg.engine_cfg(v.engine)  # 提前炸,别等到跑一半

    if not risk.dry_run and not cfg.private_key:
        raise ConfigError(
            "dry_run=false 但环境变量 POLYVOTE_PRIVATE_KEY 没设 —— 拒绝启动"
        )

    return cfg
