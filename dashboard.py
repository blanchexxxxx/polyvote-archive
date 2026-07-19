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
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# 能 import polyvote.config —— 观测台现在要 load 配置拿当前模型
sys.path.insert(0, str(Path(__file__).parent))

DB_PATH = "polyvote.db"
HTML_PATH = Path(__file__).parent / "dashboard.html"
CONFIG_PATH = Path(__file__).parent / "config.yaml"
OVERRIDES_PATH = Path(__file__).parent / "overrides.json"


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


def cache_stats(c: sqlite3.Connection, limit: int = 30) -> dict:
    """缓存命中率。公式和阈值抄自 mory 的 RailCacheWidget。

      ratio = Σcache_read / Σ(cache_read + cache_write + new)   —— out 不进分母
      单次:  cache_read==0 → miss;cache_read > cache_write*2 → hit;否则 warm

    四个数原样呈现,不加权。加权算术("实耗")在 mory 里只存在于注释,
    没有代码验证过 —— 不抄没验证过的东西。
    """
    rows = [
        dict(r)
        for r in c.execute(
            """SELECT ts, role, actor, model, new_tokens, cache_read,
                      cache_write, out_tokens
               FROM llm_calls ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    ]
    agg = c.execute(
        """SELECT COALESCE(SUM(new_tokens),0) n, COALESCE(SUM(cache_read),0) r,
                  COALESCE(SUM(cache_write),0) w, COALESCE(SUM(out_tokens),0) o,
                  COUNT(*) c FROM llm_calls"""
    ).fetchone()

    # gap 是相对**更旧的那一次**算的 —— 这是 TTL 探针:
    # gap 很大(比如 +45m)而 cache_read 还很高,就证明 1h TTL 真的在生效。
    for i, r in enumerate(rows):
        older = rows[i + 1]["ts"] if i + 1 < len(rows) else None
        r["gap_s"] = _gap_seconds(older, r["ts"]) if older else None
        # Codex 的 banner 只报一个 tokens used 合计,没有读/写分项。
        # 所以读写都是 0 不代表"没命中",而是"抠不出数据"—— 单列一类 n/a,
        # 别让它冒充 miss 拉低命中率的观感(它其实也不进分母,因为四个数都是 0)。
        if r["cache_read"] == 0 and r["cache_write"] == 0 and r["new_tokens"] == 0:
            r["cls"] = "na"
        elif r["cache_read"] == 0:
            r["cls"] = "miss"
        elif r["cache_read"] > r["cache_write"] * 2:
            r["cls"] = "hit"
        else:
            r["cls"] = "warm"

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


def _gap_seconds(older_ts: str, newer_ts: str) -> float | None:
    try:
        a = datetime.fromisoformat(older_ts)
        b = datetime.fromisoformat(newer_ts)
        return (b - a).total_seconds()
    except (ValueError, TypeError):
        return None


def model_config() -> dict:
    """当前生效的模型配置 + 可选项,喂前端的模型设置面板。

    "当前生效"= config.yaml 叠加 overrides.json 之后。所以前端显示的永远是
    bot 下一轮真会用的模型,不是 yaml 里的原始值。
    """
    try:
        from polyvote.config import load

        cfg = load(CONFIG_PATH)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}

    roles = [
        {"key": "researcher", "label": "研究员", "engine": cfg.researcher.engine,
         "model": cfg.researcher.model, "effort": cfg.researcher.effort},
        *[
            {"key": f"vote:{v.id}", "label": f"投票·{v.id}", "engine": v.engine,
             "model": v.model, "effort": v.effort}
            for v in cfg.voters
        ],
        {"key": "decider", "label": "决策者", "engine": cfg.decider.engine,
         "model": cfg.decider.model, "effort": cfg.decider.effort},
    ]
    return {
        "roles": roles,
        # 每个 engine 能选的模型(带 note),前端下拉用
        "options": cfg.known_models,
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        # 有没有活跃的覆盖,前端标个"已改"
        "has_overrides": OVERRIDES_PATH.exists(),
    }


def save_override(role_key: str, model: str, effort) -> dict:
    """把一条模型覆盖写进 overrides.json。bot 下一轮 load 时生效。

    engine 不让改 —— 换 engine 会连带改 effort 语义和工具能力(codex 没有
    WebSearch),不是一个下拉能安全表达的。只让换同 engine 下的 model。
    """
    # 校验:model 必须在这个 role 当前 engine 的 known 列表里
    cfg_info = model_config()
    if "error" in cfg_info:
        return {"ok": False, "reason": cfg_info["error"]}
    role = next((r for r in cfg_info["roles"] if r["key"] == role_key), None)
    if role is None:
        return {"ok": False, "reason": f"未知角色 {role_key}"}
    allowed = {m["model"] for m in cfg_info["options"].get(role["engine"], [])}
    if allowed and model not in allowed:
        return {"ok": False, "reason": f"{model} 不在 {role['engine']} 的可选列表里"}
    if effort not in (None, "low", "medium", "high", "xhigh", "max"):
        return {"ok": False, "reason": f"effort {effort!r} 非法"}

    ov = {}
    if OVERRIDES_PATH.exists():
        try:
            ov = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            ov = {}
    ov[role_key] = {"model": model, "effort": effort}
    OVERRIDES_PATH.write_text(json.dumps(ov, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}


def reset_override(role_key: str | None) -> dict:
    """清覆盖。role_key=None 清全部,回到 config.yaml 的原始值。"""
    if not OVERRIDES_PATH.exists():
        return {"ok": True}
    if role_key is None:
        OVERRIDES_PATH.unlink()
        return {"ok": True}
    try:
        ov = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"ok": True}
    ov.pop(role_key, None)
    if ov:
        OVERRIDES_PATH.write_text(json.dumps(ov, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        OVERRIDES_PATH.unlink()
    return {"ok": True}


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
        #
        # 边界必须在 Python 里算、跟 ts 同格式(isoformat 带 T)再传参。
        # 不能用 sqlite 的 datetime('now','-5h') —— 它产出空格分隔,而
        # 'T'(0x54) > ' '(0x20),字符串比较会把边界日全天都算进窗口:
        # 实测显示 $52.40,真实只有 $11.38,虚高 4.6 倍。
        boundary = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        window_usd = c.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) s FROM rounds WHERE ts >= ?",
            (boundary,),
        ).fetchone()["s"]

        cache = (
            cache_stats(c)
            if _table_exists(c, "llm_calls")
            else {"calls": 0, "ratio": 0.0, "recent": []}
        )

        acted = sum(1 for r in rounds if r["allowed"])
        return {
            "cache": cache,
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
            "models": model_config(),
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

    def _json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/state":
            try:
                self._json(200, api_state())
            except Exception as e:  # noqa: BLE001
                self._json(200, {"ready": False, "reason": f"{type(e).__name__}: {e}"})
        elif path in ("/", "/index.html"):
            try:
                self._send(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, b"dashboard.html not found", "text/plain")
        else:
            self._send(404, b"404", "text/plain")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"ok": False, "reason": "body 不是合法 JSON"})
            return

        # 设置模型:观测台从只读变成能写 overrides.json。
        # 只改模型/effort,engine 不让动(见 save_override 里的说明)。
        if path == "/api/model":
            r = save_override(body.get("role"), body.get("model"), body.get("effort"))
            self._json(200 if r["ok"] else 400, r)
        elif path == "/api/model/reset":
            self._json(200, reset_override(body.get("role")))
        else:
            self._json(404, {"ok": False, "reason": "no such endpoint"})


def main() -> None:
    global DB_PATH
    ap = argparse.ArgumentParser()
    # 撞端口很容易发生 —— 先 `ss -tln` 扫一眼再定。
    ap.add_argument("--port", type=int, default=8920)
    # 默认只听本机。这个观测台**没有任何鉴权** —— 谁能连上谁就能看到
    # 你的全部持仓、决策和推理链。绑 0.0.0.0 之前先确认这台机器在
    # tailscale/VPN 里,而不是裸奔在公网上。
    ap.add_argument(
        "--host",
        default="127.0.0.1",
        help="默认 127.0.0.1(只听本机)。要从别的设备看就传 0.0.0.0 —— "
        "但仅限这台机器在 tailscale/VPN 之类的私有网络里,因为本服务无鉴权。",
    )
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
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"⚠ 绑在 {args.host} 且无鉴权 —— 谁能连上谁就能看到你的全部持仓和推理链。\n"
            f"  确认这台机器在 tailscale/VPN 里,不是裸奔公网。"
        )
        print(f"  从别的设备:  http://{socket.gethostname()}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n退出")


if __name__ == "__main__":
    main()
