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


def fetch_markets(gamma_host: str, pages: int = 3, page_size: int = 100) -> list[Market]:
    """拉活跃市场,翻页。

    gamma 单页最多 100 条。只看第一页 = 只看成交量前 100 = 永远在大盘打转 ——
    24 小时实测里 71/98 轮都在反复分析同两个百万级世界杯盘,全部弃权。
    edge 在长尾,得把视野翻到 300 个。
    """
    url = f"{gamma_host.rstrip('/')}/markets"
    seen_ids: set[str] = set()
    out: list[Market] = []
    total_rows = 0
    for i in range(pages):
        params = {
            "closed": "false",
            "active": "true",
            "archived": "false",
            "limit": str(page_size),
            "offset": str(i * page_size),
            "order": "volume24hr",
            "ascending": "false",
        }
        try:
            resp = requests.get(url, params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as e:
            # 翻到第 N 页失败就用前面拿到的,别让一页超时废掉整轮
            log.warning("gamma 第 %d 页拉取失败,用已有的 %d 条: %s", i + 1, len(out), e)
            break
        rows = payload if isinstance(payload, list) else payload.get("data", [])
        if not rows:
            break
        total_rows += len(rows)
        for raw in rows:
            m = parse_market(raw)
            # 翻页期间排序可能漂移,按 id 去重
            if m is not None and m.id not in seen_ids:
                seen_ids.add(m.id)
                out.append(m)
    log.info("gamma 翻 %d 页拿到 %d 条,可解析二元市场 %d 个", pages, total_rows, len(out))
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
    max_vol = f.get("max_volume_24h_usd")
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
        if max_vol is not None and m.volume_24h > max_vol:
            # 巨量盘 = 盯的人最多 = 定价最尖。在这里找 edge 是结构性徒劳,
            # 24h 实测:71/98 轮烧在百万级世界杯盘上,全部正确地弃权。
            drop("成交量过大(定价太尖,没edge)")
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
        log.info("静态筛掉: %s", ", ".join(f"{k}×{v}" for k, v in reasons.items()))
    log.info("静态筛后剩 %d 个市场", len(kept))
    # 注意:这里不再截断到 max_markets_per_round —— 那样等于"永远取成交量
    # 最大的 N 个",正是 24h 空转的病根。最终挑选交给 select_candidates
    # (冷却+打分) 和 book_prefilter (盘口体检)。
    return kept


def select_candidates(
    markets: list[Market],
    seen: dict[str, tuple[datetime, float]],
    f: dict[str, Any],
    open_ids: frozenset[str] = frozenset(),
) -> tuple[list[Market], int]:
    """冷却 + 打分排序。返回 (排好序的候选, 被冷却掉的数量)。

    冷却:分析过且价格没动的盘,cooldown_hours 内不再烧配额重复推导同一个
    结论 —— 24h 实测里 71/98 轮是对同两个盘的重复分析,这是最大的浪费。
    价格挪动超过 reprice_threshold 提前解锁:动了说明有新闻,恰是机会窗口。
    持仓盘永不冷却(要持续盯着好决定 SELL)。

    打分全是代码启发式,零 LLM 成本,而且每一分都对着 24h 数据里的一个教训:
      新盘 +2        —— 没人分析过,定价可能还没坐实
      价格异动 +至多3 —— 出事了,市场可能反应过度/不足
      2-21天结算 +1  —— 反馈快,资金效率高
      中量带 +1      —— $5k-100k:够成交又不至于被盯死
      "vs." 单场 -2  —— 体育单场被博彩公司定死价,昨天此类全部弃权
    """
    now = datetime.now(timezone.utc)
    cooldown_h = float(f.get("cooldown_hours", 8))
    reprice = float(f.get("reprice_threshold", 0.04))
    depri = [k.lower() for k in (f.get("deprioritize_keywords") or [])]

    scored: list[tuple[float, Market]] = []
    cooled = 0
    for m in markets:
        s = seen.get(m.id)
        if m.id not in open_ids and s is not None:
            last_ts, last_px = s
            age_h = (now - last_ts).total_seconds() / 3600
            if age_h < cooldown_h and abs(m.yes_price - last_px) < reprice:
                cooled += 1
                continue

        score = 0.0
        if s is None:
            score += 2.0
        else:
            score += min(3.0, abs(m.yes_price - s[1]) / 0.02 * 0.5)
        d = m.days_to_resolution
        if d is not None and 2 <= d <= 21:
            score += 1.0
        if 5_000 <= m.volume_24h <= 100_000:
            score += 1.0
        q = m.question.lower()
        if any(k in q for k in depri):
            score -= 2.0
        scored.append((score, m))

    scored.sort(key=lambda t: (-t[0], -t[1].volume_24h))
    return [m for _, m in scored], cooled


def book_prefilter(
    cands: list[Market],
    f: dict[str, Any],
    need: int,
    clob_host: str,
    fetch: Any = None,
) -> tuple[list[Market], dict[str, int]]:
    """分析前的盘口体检。从高分往下试,凑够 need 个健康盘就停。

    这层堵的是 24h 实测里最贵的假信号:gamma 的中间价会陈旧 —— St. Louis
    棒球盘中间价挂 0.815,真实盘口 bid 0.999 无 ask,比赛早打完了。这种盘
    喂给分析管道,三个模型会"发现"edge -0.79 高信心投票,全靠决策者兜底,
    每次白烧 $0.71。在这里花一个免费 HTTP 调用直接筛掉。
    """
    fetch = fetch or fetch_book
    max_spread = float(f.get("book_max_spread", 0.06))
    max_ask = float(f.get("book_max_ask", 0.95))

    kept: list[Market] = []
    drops: dict[str, int] = {}

    def drop(why: str) -> None:
        drops[why] = drops.get(why, 0) + 1

    for m in cands:
        if len(kept) >= need:
            break
        try:
            book = fetch(clob_host, m.token_id_for("YES"))
        except ValueError:
            drop("拿不到 token id")
            continue
        bid, ask = best_bid(book), best_ask(book)
        if bid is None or ask is None:
            drop("盘口单边/空(死盘或已收敛,中间价是陈旧的)")
            continue
        if bid >= 0.97 or ask <= 0.03:
            drop("盘口已收敛(结果基本确定,gamma中间价没跟上)")
            continue
        if ask - bid > max_spread:
            drop(f"点差>{max_spread}(成交不了)")
            continue
        if ask > max_ask:
            drop(f"卖一>{max_ask}(追不了)")
            continue
        kept.append(m)
    return kept, drops


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
