"""一手实时数据直取 —— 不经过 LLM,不经过搜索引擎。

动机(实测):"Will Bitcoin reach $67,500 in July?" 这个盘,haiku 研究员烧了
10 轮 WebSearch 搜新闻,两次撞 max_turns 失败,每次 ~$0.1 —— 而它要的核心
事实(比特币现在多少钱)一个免费 API 调用就有,还是零滞后的一手数据。

搜索引擎索引的是记者消化过的二手货,滞后几分钟到几小时;市场靠一手信息定价。
数字类市场的关键事实必须代码直取,注入简报,LLM 只负责判断。

注意:注入的是**世界事实**(现货价格),不是市场赔率 —— 不破坏投票人的
价格致盲。致盲挡的是 Polymarket/博彩盘口,不是现实世界的数据。

目前只做加密现货价(CoinGecko 免费口,无需 key)。结构留了口子,以后可加:
天气(NWS/JMA)、体育比分(ESPN 公开 JSON)、股价等。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests

from .market import Market

log = logging.getLogger(__name__)

_TIMEOUT = 10

# 问题文本里的币种关键词 → CoinGecko id
_COINS = {
    "bitcoin": "bitcoin", "btc": "bitcoin",
    "ethereum": "ethereum", "eth": "ethereum",
    "solana": "solana", "sol ": "solana",
    "xrp": "ripple",
    "dogecoin": "dogecoin", "doge": "dogecoin",
    "cardano": "cardano", "ada ": "cardano",
}

# 问题里带价格阈值的样子: $67,500 / $0.50 / 100k 等
_PRICE_HINT = re.compile(r"\$[\d,]+(?:\.\d+)?|\b\d{2,3}k\b", re.IGNORECASE)


def _detect_coin(question: str) -> str | None:
    q = question.lower()
    for kw, coin_id in _COINS.items():
        if kw in q:
            return coin_id
    return None


def _spot_price(coin_id: str) -> float | None:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": coin_id, "vs_currencies": "usd"},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return float(r.json()[coin_id]["usd"])
    except Exception as e:  # noqa: BLE001 — 拿不到就算了,研究员照常跑
        log.warning("拉 %s 现货价失败: %s", coin_id, e)
        return None


def live_context(market: Market) -> str | None:
    """能代码直取的一手数据。拿不到/不适用返回 None,调用方照常走。

    返回的文本会同时注入研究员 prompt 和投票人简报,
    标明"代码直取的一手数据",跟搜索来的二手信息区分开。
    """
    coin = _detect_coin(market.question)
    if coin is None:
        return None
    # 没有价格阈值的币圈盘(比如"某链会不会上线")现货价帮助不大,也注入无妨,
    # 但优先服务阈值盘 —— 那是研究员反复烧钱的地方。
    px = _spot_price(coin)
    if px is None:
        return None
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    hint = " (question contains a price threshold — compare directly)" if _PRICE_HINT.search(market.question) else ""
    return (
        f"LIVE DATA (fetched directly by code from CoinGecko at {ts}; "
        f"primary source, zero lag — trust this over any news article){hint}:\n"
        f"  {coin} spot price = ${px:,.2f} USD"
    )
