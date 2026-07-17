#!/usr/bin/env python3
"""polyvote 观测台。

  python dashboard.py           # 0.0.0.0:8910
  python dashboard.py --port X

只读 polyvote.db,不碰交易逻辑 —— 观测台挂了不影响 bot 跑。
用 stdlib http.server,不引入 FastAPI/uvicorn 依赖。

绑 0.0.0.0。装了 tailscale 的话,从别的设备直接用主机名访问即可
(http://<你的主机名>:8920) —— 别把这个口暴露到公网,它没有任何鉴权。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

DB_PATH = "polyvote.db"
HTML_PATH = Path(__file__).parent / "dashboard.html"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _table_exists(c: sqlite3.Connection, name: str) -> bool:
    return (
        c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def api_state() -> dict:
    """观测台的全部数据。一个请求拿完,前端不用编排多个调用。"""
    c = _conn()
    try:
        if not _table_exists(c, "rounds"):
            return {"ready": False, "reason": "还没有数据 —— bot 还没跑过第一轮"}

        rounds = [dict(r) for r in c.execute(
            "SELECT * FROM rounds ORDER BY id DESC LIMIT 60"
        ).fetchall()]

        vcols = {r[1] for r in c.execute("PRAGMA table_info(votes)")}
        by_round: dict[int, list] = {}
        for v in c.execute(
            "SELECT * FROM votes WHERE round_id IN "
            "(SELECT id FROM rounds ORDER BY id DESC LIMIT 60)"
        ):
            d = dict(v)
            # reasons_no/yes 是后加的列。老库里没有 —— 别让观测台因为读老数据就崩。
            for k in ("reasons_no", "reasons_yes"):
                d[k] = json.loads(d[k]) if d.get(k) else []
            if "base_rate" not in vcols:
                d["base_rate"] = None
            by_round.setdefault(v["round_id"], []).append(d)

        research: dict[int, dict] = {}
        if _table_exists(c, "research"):
            for r in c.execute(
                "SELECT * FROM research WHERE round_id IN "
                "(SELECT id FROM rounds ORDER BY id DESC LIMIT 60)"
            ):
                d = dict(r)
                d["facts"] = json.loads(d.get("facts") or "[]")
                d["uncertainties"] = json.loads(d.get("uncertainties") or "[]")
                research[r["round_id"]] = d

        for r in rounds:
            r["tally"] = json.loads(r.get("tally") or "{}")
            r["risk_notes"] = json.loads(r.get("risk_notes") or "[]")
            r["votes"] = by_round.get(r["id"], [])
            r["research"] = research.get(r["id"])

        fills = [dict(f) for f in c.execute(
            "SELECT * FROM fills ORDER BY id DESC LIMIT 100"
        ).fetchall()] if _table_exists(c, "fills") else []

        settlements = [dict(s) for s in c.execute(
            "SELECT * FROM settlements ORDER BY resolved_at DESC LIMIT 100"
        ).fetchall()] if _table_exists(c, "settlements") else []

        # 累计盈亏曲线。没结算过的话是空的 —— 那才是诚实的:
        # 没有已结算的交易就没有 PnL 可言,不画一条假的平线。
        curve, run = [], 0.0
        for s in sorted(settlements, key=lambda x: x["resolved_at"]):
            run += s["pnl_usd"]
            curve.append({"ts": s["resolved_at"], "pnl": round(run, 4),
                          "won": bool(s["won"]), "q": s["question"]})

        n_settled = len(settlements)
        wins = sum(1 for s in settlements if s["won"])
        staked = sum(s["cost_usd"] for s in settlements)
        trade_pnl = sum(s["pnl_usd"] for s in settlements)
        llm_cost = c.execute(
            "SELECT COALESCE(SUM(cost_usd),0) s FROM rounds"
        ).fetchone()["s"]

        # 滚动 5 小时窗口的配额占用 —— 这才是订阅制下的真实约束。
        # 夏用同一份配额,cc_engine 的 rate_limit_type 是 'five_hour'。
        window_usd = c.execute(
            """SELECT COALESCE(SUM(cost_usd), 0) s FROM rounds
               WHERE ts >= datetime('now', '-5 hours')"""
        ).fetchone()["s"]

        acted = sum(1 for r in rounds if r["allowed"])
        return {
            "ready": True,
            "now": datetime.now(timezone.utc).isoformat(),
            "dry_run": bool(rounds[0]["dry_run"]) if rounds else True,
            "summary": {
                "rounds": c.execute("SELECT COUNT(*) n FROM rounds").fetchone()["n"],
                "acted": acted,
                "settled": n_settled,
                "wins": wins,
                "losses": n_settled - wins,
                "win_rate": (wins / n_settled) if n_settled else None,
                # 真美元
                "trade_pnl_usd": round(trade_pnl, 2),
                "staked_usd": round(staked, 2),
                "roi": (trade_pnl / staked) if staked else None,
                # 配额当量 —— CLI 报的 total_cost_usd,是"走 API 会花多少"的折算。
                # 走订阅时不是真实支出,所以绝不拿它去减 trade_pnl:
                # 那是虚拟单位减真实单位,算出来永远深度负,毫无意义。
                "llm_cost_usd": round(llm_cost, 2),
                "window_usd": round(window_usd, 2),
            },
            "rounds": rounds,
            "fills": fills,
            "settlements": settlements,
            "curve": curve,
        }
    finally:
        c.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 别把 access log 刷屏
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/state":
            try:
                body = json.dumps(api_state(), ensure_ascii=False, default=str)
            except Exception as e:  # noqa: BLE001
                body = json.dumps({"ready": False, "reason": f"{type(e).__name__}: {e}"})
            self._send(200, body.encode("utf-8"), "application/json; charset=utf-8")
        elif path in ("/", "/index.html"):
            try:
                self._send(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, b"dashboard.html not found", "text/plain")
        else:
            self._send(404, b"404", "text/plain")


def main() -> None:
    global DB_PATH
    ap = argparse.ArgumentParser()
    # 撞端口很容易发生 —— 先 `ss -tln` 扫一眼再定。
    ap.add_argument("--port", type=int, default=8920)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--db", default="polyvote.db")
    args = ap.parse_args()
    DB_PATH = args.db

    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        # 端口撞车在这台 Pi 上很容易发生 —— 上面已经跑着 20+ 个监听服务。
        # 直接说清楚是撞了,别让人对着 traceback 猜。
        raise SystemExit(
            f"起不来 {args.host}:{args.port} —— {e}\n"
            f"多半是端口被占了。查一下:  ss -tlnp | grep {args.port}\n"
            f"换个口:  python dashboard.py --port 8921"
        ) from e

    import socket

    print(f"观测台: http://{args.host}:{args.port}  (db={DB_PATH})")
    print(f"走 tailscale 从别的设备:  http://{socket.gethostname()}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n退出")


if __name__ == "__main__":
    main()
