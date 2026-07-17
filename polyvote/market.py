"""Polymarket 市场数据 (Gamma 公开 API,不需要认证也不需要钱包)。

实测 (2026-07-17, Pi, 日本 IP): gamma-api 和 clob 都是 200,无地理封锁。

一个坑:按 volume24hr 排序的榜首全是 0.0085/0.9915 这种已经定盘的市场
(埃塞俄比亚总理之类),成交量大但没有任何 edge。所以价格区间才是最重要的
筛子,不是成交量。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

log = logging.getLogger(__name__)

_TIMEOUT = 20


@dataclass
class Market:
    id: str
    question: str
    description: str
    slug: str
    outcomes: list[str]
    prices: list[float]
    token_ids: list[str]
    volume_24h: float
    liquidity: float
    end_date: datetime | None

    @property
    def yes_price(self) -> float:
        return self.prices[0]

    @property
    def no_price(self) -> float:
        return self.prices[1] if len(self.prices) > 1 else 1.0 - self.prices[0]

    @property
    def days_to_resolution(self) -> float | None:
        if self.end_date is None:
            return None
        delta = self.end_date - datetime.now(timezone.utc)
        return delta.total_seconds() / 86400

    def token_id_for(self, side: str) -> str:
        """side 是 YES/NO —— 映射到 clobTokenIds 的下标。"""
        idx = 0 if side.upper() == "YES" else 1
        try:
            return self.token_ids[idx]
        except IndexError:
            raise ValueError(
                f"市场 {self.id} 没有 {side} 对应的 token id: {self.token_ids}"
            ) from None

    def url(self) -> str:
        return f"https://polymarket.com/event/{self.slug}"


def _maybe_json_list(value: Any) -> list:
    """Gamma 的 outcomes/outcomePrices/clobTokenIds 是 JSON 编码的字符串,
    不是真数组。但不同 endpoint 行为不完全一致,所以两种都接。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_market(raw: dict[str, Any]) -> Market | None:
    prices = [_to_float(p, -1.0) for p in _maybe_json_list(raw.get("outcomePrices"))]
    outcomes = [str(o) for o in _maybe_json_list(raw.get("outcomes"))]
    token_ids = [str(t) for t in _maybe_json_list(raw.get("clobTokenIds"))]

    # 二元市场以外的先不碰:仓位和 token 映射逻辑不一样。
    if len(prices) != 2 or len(token_ids) != 2 or any(p < 0 for p in prices):
        return None
    if not raw.get("question"):
        return None

    return Market(
        id=str(raw.get("conditionId") or raw.get("id") or ""),
        question=str(raw["question"]),
        description=str(raw.get("description") or "")[:4000],
        slug=str(raw.get("slug") or ""),
        outcomes=outcomes or ["Yes", "No"],
        prices=prices,
        token_ids=token_ids,
        volume_24h=_to_float(raw.get("volume24hr")),
        liquidity=_to_float(raw.get("liquidityNum") or raw.get("liquidity")),
        end_date=_parse_dt(raw.get("endDate")),
    )


def fetch_markets(gamma_host: str, limit: int = 200) -> list[Market]:
    """拉活跃市场。limit 给大点,筛完之后剩不了几个。"""
    url = f"{gamma_host.rstrip('/')}/markets"
    params = {
        "closed": "false",
        "active": "true",
        "archived": "false",
        "limit": str(limit),
        "order": "volume24hr",
        "ascending": "false",
    }
    resp = requests.get(url, params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    rows = payload if isinstance(payload, list) else payload.get("data", [])

    out = []
    for raw in rows:
        m = parse_market(raw)
        if m is not None:
            out.append(m)
    log.info("gamma 返回 %d 条,可解析的二元市场 %d 条", len(rows), len(out))
    return out


def filter_markets(markets: list[Market], f: dict[str, Any]) -> list[Market]:
    """按 config.yaml 的 market_filter 筛。

    价格区间是这里最要紧的一条 —— 挡掉已定盘的市场。
    """
    only_id = f.get("_only_id")
    if only_id:
        hit = [m for m in markets if only_id in (m.id, m.slug)]
        log.info("--market %s → 命中 %d 个,其余筛选全部跳过", only_id, len(hit))
        return hit

    price_min = f.get("price_min", 0.10)
    price_max = f.get("price_max", 0.90)
    min_vol = f.get("min_volume_24h_usd", 0)
    min_liq = f.get("min_liquidity_usd", 0)
    min_days = f.get("min_days_to_resolution")
    max_days = f.get("max_days_to_resolution")
    excludes = [k.lower() for k in (f.get("exclude_keywords") or [])]

    kept, reasons = [], {}

    def drop(why: str) -> None:
        reasons[why] = reasons.get(why, 0) + 1

    for m in markets:
        if not (price_min <= m.yes_price <= price_max):
            drop("价格在区间外(多半已定盘)")
            continue
        if m.volume_24h < min_vol:
            drop("24h成交量不足")
            continue
        if m.liquidity < min_liq:
            drop("流动性不足")
            continue
        d = m.days_to_resolution
        if d is not None:
            if min_days is not None and d < min_days:
                drop("离结算太近")
                continue
            if max_days is not None and d > max_days:
                drop("离结算太远")
                continue
        q = m.question.lower()
        if any(k in q for k in excludes):
            drop("命中排除关键词")
            continue
        kept.append(m)

    if reasons:
        log.info("筛掉: %s", ", ".join(f"{k}×{v}" for k, v in reasons.items()))
    log.info("筛后剩 %d 个市场", len(kept))

    kept.sort(key=lambda m: m.volume_24h, reverse=True)
    max_n = f.get("max_markets_per_round")
    return kept[:max_n] if max_n else kept


def fetch_book(clob_host: str, token_id: str) -> dict[str, Any] | None:
    """拉订单簿。用来给决策者看真实的买卖盘口,而不是只看中间价。"""
    try:
        resp = requests.get(
            f"{clob_host.rstrip('/')}/book",
            params={"token_id": token_id},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("拉 token %s 的订单簿失败: %s", token_id[:12], e)
        return None


def best_ask(book: dict[str, Any] | None) -> float | None:
    """最低卖价 = 你买入要付的价。"""
    if not book:
        return None
    asks = book.get("asks") or []
    prices = [_to_float(a.get("price"), -1) for a in asks if isinstance(a, dict)]
    prices = [p for p in prices if p > 0]
    return min(prices) if prices else None


def best_bid(book: dict[str, Any] | None) -> float | None:
    """最高买价 = 你卖出能拿到的价。"""
    if not book:
        return None
    bids = book.get("bids") or []
    prices = [_to_float(b.get("price"), -1) for b in bids if isinstance(b, dict)]
    prices = [p for p in prices if p > 0]
    return max(prices) if prices else None
