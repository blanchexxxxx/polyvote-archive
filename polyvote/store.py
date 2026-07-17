"""SQLite 记账。

dry-run 阶段这个库就是全部产出:每一轮谁投了什么、决策者拍了什么、
风控放没放行、模拟成交价多少。跑几天之后回头看这张表,才知道
这套投票机制到底有没有 edge —— 别靠感觉决定要不要开实盘。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .brain import Decision, Research, Vote

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    market_id   TEXT NOT NULL,
    question    TEXT NOT NULL,
    yes_price   REAL NOT NULL,
    volume_24h  REAL,
    tally       TEXT NOT NULL,
    action      TEXT NOT NULL,
    side        TEXT,
    size_usd    REAL,
    limit_price REAL,
    confidence  INTEGER,
    reason      TEXT,
    allowed     INTEGER NOT NULL,
    risk_notes  TEXT,
    dry_run     INTEGER NOT NULL,
    cost_usd    REAL
);
CREATE TABLE IF NOT EXISTS votes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id    INTEGER NOT NULL REFERENCES rounds(id),
    voter_id    TEXT NOT NULL,
    engine      TEXT NOT NULL,
    model       TEXT NOT NULL,
    side        TEXT NOT NULL,
    confidence  INTEGER,
    fair_price  REAL,
    base_rate   REAL,      -- 模型看本题证据前给的同类基准
    reasons_no  TEXT,      -- JSON: [str] —— 强制的推理链,先 NO 后 YES
    reasons_yes TEXT,      -- JSON: [str]
    reason      TEXT,
    cost_usd    REAL,
    error       TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id     INTEGER NOT NULL REFERENCES rounds(id),
    ts           TEXT NOT NULL,
    market_id    TEXT NOT NULL,
    question     TEXT,
    side         TEXT NOT NULL,
    token_id     TEXT,
    size_usd     REAL NOT NULL,
    price        REAL NOT NULL,
    shares       REAL,
    dry_run      INTEGER NOT NULL,
    order_id     TEXT,
    status       TEXT,
    raw          TEXT
);
-- 研究简报。每轮一条,给前端展示"这轮 agent 看到了什么事实"。
CREATE TABLE IF NOT EXISTS research (
    round_id      INTEGER PRIMARY KEY REFERENCES rounds(id),
    model         TEXT NOT NULL,
    summary       TEXT,
    facts         TEXT,      -- JSON: [{claim, source, date}]
    uncertainties TEXT,      -- JSON: [str]
    searched      INTEGER,
    cost_usd      REAL,
    error         TEXT
);
-- 结算。市场解决之后回写,这才能算出这套投票到底有没有 edge。
CREATE TABLE IF NOT EXISTS settlements (
    market_id     TEXT PRIMARY KEY,
    question      TEXT,
    resolved_at   TEXT NOT NULL,
    winning_side  TEXT NOT NULL,   -- YES | NO
    dry_run       INTEGER NOT NULL,
    cost_usd      REAL NOT NULL,   -- 我们买入花的钱
    payout_usd    REAL NOT NULL,   -- 结算拿回的钱
    pnl_usd       REAL NOT NULL,
    won           INTEGER NOT NULL
);
-- 每次 LLM 调用的真实 token。订阅制下 cost_usd 只是折算,规划配额靠这个。
--
-- 四个数是原始值,不加权 —— 加权算术在别处只存在于注释里,没人验证过。
-- 命中率 = Σcache_read / Σ(cache_read + cache_write + new),output 不进分母。
CREATE TABLE IF NOT EXISTS llm_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id    INTEGER REFERENCES rounds(id),
    ts          TEXT NOT NULL,
    role        TEXT NOT NULL,   -- research | vote | decide
    actor       TEXT NOT NULL,   -- voter_id / 'researcher' / 'decider'
    engine      TEXT NOT NULL,
    model       TEXT NOT NULL,
    new_tokens  INTEGER NOT NULL DEFAULT 0,  -- input_tokens: 没走缓存的增量
    cache_read  INTEGER NOT NULL DEFAULT 0,  -- 命中旧缓存 · 约 0.1× · 高 = 热
    cache_write INTEGER NOT NULL DEFAULT 0,  -- 写/重建缓存 · 约 1.25-2× · 高 = 冷启
    out_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON llm_calls(ts);
CREATE INDEX IF NOT EXISTS idx_calls_round ON llm_calls(round_id);
CREATE INDEX IF NOT EXISTS idx_rounds_ts ON rounds(ts);
CREATE INDEX IF NOT EXISTS idx_rounds_market ON rounds(market_id);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
CREATE INDEX IF NOT EXISTS idx_fills_market ON fills(market_id);
CREATE INDEX IF NOT EXISTS idx_votes_round ON votes(round_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def log_round(
        self,
        *,
        market_id: str,
        question: str,
        yes_price: float,
        volume_24h: float,
        tally: dict[str, Any],
        decision: Decision,
        votes: list[Vote],
        research: Research | None,
        allowed: bool,
        risk_notes: list[str],
        dry_run: bool,
    ) -> int:
        cost = sum(
            x
            for x in [
                decision.cost_usd,
                *(v.cost_usd for v in votes),
                research.cost_usd if research else None,
            ]
            if x
        )
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO rounds
                   (ts, market_id, question, yes_price, volume_24h, tally, action,
                    side, size_usd, limit_price, confidence, reason, allowed,
                    risk_notes, dry_run, cost_usd)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    _now(),
                    market_id,
                    question,
                    yes_price,
                    volume_24h,
                    json.dumps(tally, ensure_ascii=False),
                    decision.action,
                    decision.side,
                    decision.size_usd,
                    decision.limit_price,
                    decision.confidence,
                    decision.reason,
                    int(allowed),
                    json.dumps(risk_notes, ensure_ascii=False),
                    int(dry_run),
                    cost or None,
                ),
            )
            rid = cur.lastrowid
            c.executemany(
                """INSERT INTO votes
                   (round_id, voter_id, engine, model, side, confidence,
                    fair_price, base_rate, reasons_no, reasons_yes, reason,
                    cost_usd, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        rid,
                        v.voter_id,
                        v.engine,
                        v.model,
                        v.side,
                        v.confidence,
                        # NaN 进 SQLite 会变成 NULL,但显式点更清楚
                        None if v.fair_price != v.fair_price else v.fair_price,
                        v.base_rate,
                        json.dumps(v.reasons_no, ensure_ascii=False),
                        json.dumps(v.reasons_yes, ensure_ascii=False),
                        v.reason,
                        v.cost_usd,
                        v.error,
                    )
                    for v in votes
                ],
            )
            if research is not None:
                c.execute(
                    """INSERT INTO research
                       (round_id, model, summary, facts, uncertainties,
                        searched, cost_usd, error)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        rid,
                        research.model,
                        research.summary,
                        json.dumps(
                            [asdict(f) for f in research.facts], ensure_ascii=False
                        ),
                        json.dumps(research.uncertainties, ensure_ascii=False),
                        int(research.searched),
                        research.cost_usd,
                        research.error,
                    ),
                )
        return rid

    def log_call(
        self,
        *,
        role: str,
        actor: str,
        engine: str,
        model: str,
        usage: Any,
        cost_usd: float | None = None,
        duration_ms: int | None = None,
        round_id: int | None = None,
    ) -> None:
        """记一次 LLM 调用的 token。

        round_id 先写 None —— 调用发生在 log_round 之前(那时还没有 round id)。
        整轮结束后由 attach_calls_to_round() 回填。
        """
        with self._conn() as c:
            c.execute(
                """INSERT INTO llm_calls
                   (round_id, ts, role, actor, engine, model,
                    new_tokens, cache_read, cache_write, out_tokens,
                    cost_usd, duration_ms)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    round_id,
                    _now(),
                    role,
                    actor,
                    engine,
                    model,
                    getattr(usage, "input_tokens", 0),
                    getattr(usage, "cache_read", 0),
                    getattr(usage, "cache_write", 0),
                    getattr(usage, "output_tokens", 0),
                    cost_usd,
                    duration_ms,
                ),
            )

    def attach_calls_to_round(self, round_id: int) -> None:
        """把本轮里还没归属的调用挂到这一轮上。"""
        with self._conn() as c:
            c.execute(
                "UPDATE llm_calls SET round_id = ? WHERE round_id IS NULL",
                (round_id,),
            )

    def cache_stats(self, limit: int = 30) -> dict[str, Any]:
        """缓存命中率 + 最近若干次调用。

        命中率公式和分类阈值抄自 mory 的 RailCacheWidget:
          ratio = Σcache_read / Σ(cache_read + cache_write + new)   (out 不进分母)
          单次: cache_read==0 → miss; cache_read > cache_write*2 → hit; 否则 warm
        """
        with self._conn() as c:
            rows = [
                dict(r)
                for r in c.execute(
                    """SELECT ts, role, actor, model, new_tokens, cache_read,
                              cache_write, out_tokens, cost_usd
                       FROM llm_calls ORDER BY id DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            ]
            agg = c.execute(
                """SELECT COALESCE(SUM(new_tokens),0) n,
                          COALESCE(SUM(cache_read),0) r,
                          COALESCE(SUM(cache_write),0) w,
                          COALESCE(SUM(out_tokens),0) o,
                          COUNT(*) c
                   FROM llm_calls"""
            ).fetchone()

        denom = agg["r"] + agg["w"] + agg["n"]
        return {
            "calls": agg["c"],
            "new": agg["n"],
            "cache_read": agg["r"],
            "cache_write": agg["w"],
            "out": agg["o"],
            "ratio": (agg["r"] / denom) if denom else 0.0,
            "recent": rows,
        }

    def log_fill(
        self,
        *,
        round_id: int,
        market_id: str,
        question: str,
        side: str,
        token_id: str | None,
        size_usd: float,
        price: float,
        dry_run: bool,
        order_id: str | None = None,
        status: str | None = None,
        raw: Any = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO fills
                   (round_id, ts, market_id, question, side, token_id, size_usd,
                    price, shares, dry_run, order_id, status, raw)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    round_id,
                    _now(),
                    market_id,
                    question,
                    side,
                    token_id,
                    size_usd,
                    price,
                    size_usd / price if price > 0 else None,
                    int(dry_run),
                    order_id,
                    status,
                    json.dumps(raw, ensure_ascii=False, default=str) if raw else None,
                ),
            )

    def spent_today_usd(self, dry_run: bool) -> float:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._conn() as c:
            row = c.execute(
                """SELECT COALESCE(SUM(size_usd), 0) AS s FROM fills
                   WHERE dry_run = ? AND ts >= ? AND side != 'SELL'""",
                (int(dry_run), today),
            ).fetchone()
        return float(row["s"])

    def position_usd(self, market_id: str, dry_run: bool) -> float:
        """净敞口 = 买入额 - 卖出额。粗口径,够风控用。"""
        with self._conn() as c:
            row = c.execute(
                """SELECT COALESCE(SUM(CASE WHEN side='SELL' THEN -size_usd
                                            ELSE size_usd END), 0) AS s
                   FROM fills WHERE market_id = ? AND dry_run = ?""",
                (market_id, int(dry_run)),
            ).fetchone()
        return max(0.0, float(row["s"]))

    def open_position_count(self, dry_run: bool) -> int:
        with self._conn() as c:
            row = c.execute(
                """SELECT COUNT(*) AS n FROM (
                     SELECT market_id,
                            SUM(CASE WHEN side='SELL' THEN -size_usd
                                     ELSE size_usd END) AS net
                     FROM fills WHERE dry_run = ?
                     GROUP BY market_id HAVING net > 0.01
                   )""",
                (int(dry_run),),
            ).fetchone()
        return int(row["n"])

    def positions(self, dry_run: bool) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT market_id, question, side,
                          SUM(CASE WHEN side='SELL' THEN -size_usd
                                   ELSE size_usd END) AS net_usd,
                          AVG(price) AS avg_price,
                          SUM(shares) AS shares
                   FROM fills WHERE dry_run = ?
                   GROUP BY market_id HAVING net_usd > 0.01""",
                (int(dry_run),),
            ).fetchall()
        return [dict(r) for r in rows]

    def unsettled_markets(self, dry_run: bool) -> list[dict[str, Any]]:
        """有仓位、但还没结算过的市场。"""
        with self._conn() as c:
            rows = c.execute(
                """SELECT f.market_id, f.question, f.side,
                          SUM(f.size_usd) AS cost_usd,
                          SUM(f.shares) AS shares
                   FROM fills f
                   LEFT JOIN settlements s ON s.market_id = f.market_id
                                          AND s.dry_run = f.dry_run
                   WHERE f.dry_run = ? AND s.market_id IS NULL AND f.side != 'SELL'
                   GROUP BY f.market_id, f.side""",
                (int(dry_run),),
            ).fetchall()
        return [dict(r) for r in rows]

    def log_settlement(
        self,
        *,
        market_id: str,
        question: str,
        winning_side: str,
        our_side: str,
        cost_usd: float,
        shares: float,
        dry_run: bool,
    ) -> dict[str, Any]:
        """结算一个市场。

        Polymarket 的份额结算是全有或全无:赢的那边每份 $1,输的那边归零。
        所以 payout = 赢了就是 shares×$1,输了就是 0。
        """
        won = our_side == winning_side
        payout = shares * 1.0 if won else 0.0
        pnl = payout - cost_usd
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO settlements
                   (market_id, question, resolved_at, winning_side, dry_run,
                    cost_usd, payout_usd, pnl_usd, won)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    market_id,
                    question,
                    _now(),
                    winning_side,
                    int(dry_run),
                    cost_usd,
                    payout,
                    pnl,
                    int(won),
                ),
            )
        return {"won": won, "payout_usd": payout, "pnl_usd": pnl}

    def pnl_summary(self, dry_run: bool) -> dict[str, Any]:
        with self._conn() as c:
            row = c.execute(
                """SELECT COUNT(*) AS n,
                          COALESCE(SUM(won), 0) AS wins,
                          COALESCE(SUM(pnl_usd), 0) AS pnl,
                          COALESCE(SUM(cost_usd), 0) AS staked
                   FROM settlements WHERE dry_run = ?""",
                (int(dry_run),),
            ).fetchone()
            llm_cost = c.execute(
                "SELECT COALESCE(SUM(cost_usd),0) AS s FROM rounds WHERE dry_run = ?",
                (int(dry_run),),
            ).fetchone()["s"]
        n = int(row["n"])
        return {
            "settled": n,
            "wins": int(row["wins"]),
            "losses": n - int(row["wins"]),
            "win_rate": (int(row["wins"]) / n) if n else None,
            "pnl_usd": float(row["pnl"]),
            "staked_usd": float(row["staked"]),
            "roi": (float(row["pnl"]) / float(row["staked"])) if row["staked"] else None,
            "llm_cost_usd": float(llm_cost),
            # 这个数字才是真相:交易赚的钱减掉喂给模型的钱。
            "net_of_llm_usd": float(row["pnl"]) - float(llm_cost),
        }
