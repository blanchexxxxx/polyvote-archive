#!/usr/bin/env python3
"""polyvote 主循环。

  python run.py --once          跑一轮就退出 (调试用)
  python run.py                 按 config.yaml 的 interval_minutes 常驻
  python run.py --status        看看 dry-run 攒了什么数据
  python run.py --once --market <id>   只分析指定市场
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone

from polyvote import brain, desk, execute, livedata, llm, market as mkt, risk, settle
from polyvote.config import ConfigError, load
from polyvote.store import Store

log = logging.getLogger("polyvote")

_stop = False


def _on_signal(signum, _frame):
    global _stop
    log.info("收到信号 %s,跑完这一轮就退出", signum)
    _stop = True


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def _position_ctx(store: Store, dry_run: bool) -> str:
    pos = store.positions(dry_run)
    if not pos:
        return "(no current positions)"
    lines = [
        f"  {p['question'][:60]} — {p['side']} ${p['net_usd']:.2f} "
        f"@ avg price {p['avg_price']:.3f}"
        for p in pos
    ]
    return "\n".join(lines)


def _fetch_book_numbers(cfg, m: mkt.Market) -> tuple[float | None, float | None]:
    """取一次 YES 盘口的 (bid, ask)。desk 的算术和 prompt 的文本共用。"""
    host = cfg.execution.get("clob_host", "https://clob.polymarket.com")
    try:
        yes_book = mkt.fetch_book(host, m.token_id_for("YES"))
    except ValueError:
        return None, None
    return mkt.best_bid(yes_book), mkt.best_ask(yes_book)


def _book_ctx(bid: float | None, ask: float | None) -> str:
    """把真实盘口塞进 prompt。只看中间价会低估点差。"""
    if ask is None and bid is None:
        return ""
    spread = f"{(ask - bid):.4f}" if (ask and bid) else "n/a"
    return (
        f"\nReal book (YES):\n"
        f"  bid = {bid if bid else 'n/a'}   ask = {ask if ask else 'n/a'}   "
        f"spread = {spread}\n"
        f"  (buying YES costs the ask price, not the midpoint — include spread in edge calculation)\n"
    )


def run_round(cfg, store: Store) -> None:
    dry = cfg.risk.dry_run
    log.info("=" * 72)
    log.info(
        "开跑 %s  模式=%s",
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "DRY-RUN (不下单)" if dry else "!!! 实盘 !!!",
    )

    # token 快过期时避让整轮 —— 别在刷新窗口跟同机的其他 claude 进程抢刷新,
    # 抢输的一方可能把 token 写空,一次搞哑两边。等下一轮,那时 token 已被
    # 别的调用续期。读不到凭据(比如别的机器)返回 None,照常跑。
    left = llm.claude_token_seconds_left()
    if left is not None and 0 < left < 900:
        log.warning(
            "claude token %.0f 秒后过期,跳过本轮避免刷新竞争,等下一轮", left
        )
        return

    # 先结算已解决的持仓 —— 这是唯一能证明这套投票行不行的数据
    try:
        n = settle.settle_open_markets(cfg, store)
        if n:
            log.info("结算了 %d 个已解决市场", n)
    except Exception:  # noqa: BLE001
        log.exception("结算巡检出错,不影响本轮交易")

    try:
        markets = mkt.fetch_markets(
            cfg.data["gamma_host"], pages=int(cfg.market_filter.get("pages", 3))
        )
    except Exception as e:  # noqa: BLE001
        log.error("拉市场数据失败,本轮跳过: %s", e)
        return

    # 五层漏斗: 静态筛 → 冷却 → 打分 → 盘口体检 → 取前 N。
    # 前四层全是代码,零 LLM 成本 —— 配额只烧在通过体检的健康盘上。
    base = mkt.filter_markets(markets, cfg.market_filter)
    if not base:
        log.info("没有市场通过静态筛选,本轮结束")
        return

    if cfg.market_filter.get("_only_id"):
        # --market 是操作员显式点名,冷却/打分不该拦它 —— 那是给自动漏斗
        # 省钱的,不是给手动指令上锁的。盘口体检保留(安全检查不豁免)。
        cands, cooled = base, 0
    else:
        open_ids = frozenset(p["market_id"] for p in store.positions(dry))
        cands, cooled = mkt.select_candidates(
            base, store.last_seen_prices(), cfg.market_filter, open_ids
        )
    need = int(cfg.market_filter.get("max_markets_per_round", 3))
    picked, book_drops = mkt.book_prefilter(
        cands, cfg.market_filter, need,
        cfg.execution.get("clob_host", "https://clob.polymarket.com"),
    )
    log.info(
        "漏斗: 静态筛后 %d → 冷却掉 %d → 候选 %d → 盘口筛掉 %s → 本轮分析 %d 个",
        len(base), cooled, len(cands),
        ", ".join(f"{k}×{v}" for k, v in book_drops.items()) or "0",
        len(picked),
    )
    if not picked:
        log.info("没有市场通过盘口体检,本轮结束(不硬凑,等下一轮)")
        return

    spent = store.spent_today_usd(dry)
    log.info("今日已用 $%.2f / 上限 $%.2f", spent, cfg.risk.max_notional_per_day_usd)

    for m in picked:
        if _stop:
            return
        log.info("-" * 72)
        log.info("市场: %s", m.question[:70])
        log.info(
            "      YES=%.3f  24h量=$%s  %s",
            m.yes_price,
            f"{m.volume_24h:,.0f}",
            f"{m.days_to_resolution:.0f}天后结算" if m.days_to_resolution else "",
        )

        # 0) 能代码直取的一手数据先取好(现货价等) —— 零滞后零 LLM 成本。
        #    搜索引擎给的是记者嚼过的二手货,数字类事实不该让 LLM 去搜。
        live_ctx = None
        try:
            live_ctx = livedata.live_context(m)
            if live_ctx:
                log.info("  一手数据: %s", live_ctx.splitlines()[-1].strip())
        except Exception:  # noqa: BLE001 — 直取失败不影响流程
            log.exception("  一手数据直取失败(不影响本轮)")

        # 1) 查一次,三个投票人共用
        rsch = brain.research(cfg, m, live_ctx)
        if not rsch.ok and cfg.research.get("skip_market_if_research_fails", True):
            log.info("  → 跳过 (研究失败,不在没有事实的情况下让模型硬判)")
            # 必须记一条 round,否则这个市场没有冷却记录,下一轮漏斗又会
            # 选中它、又失败 —— 实测 Bitcoin 盘 34 分钟内连失败两次,
            # 每次白烧 ~$0.1 研究费。记了这条,8h 冷却就盖住它了。
            rid = store.log_round(
                market_id=m.id,
                question=m.question,
                yes_price=m.yes_price,
                volume_24h=m.volume_24h,
                tally=brain.tally([]),
                decision=brain.Decision(
                    action="SKIP", side="YES", size_usd=0.0, limit_price=0.0,
                    confidence=0,
                    reason=f"research failed: {(rsch.error or '')[:200]}",
                    model=cfg.researcher.model,
                ),
                votes=[],
                research=rsch,
                allowed=False,
                risk_notes=["研究失败,进入冷却"],
                dry_run=dry,
            )
            store.attach_calls_to_round(rid)
            continue

        # 2) 投票。投票人对价格致盲,所以 book_ctx 只给审查层,绝不能进投票 prompt。
        #    live_ctx 是世界事实(现货价),不是市场赔率 —— 可以给投票人。
        bid, ask = _fetch_book_numbers(cfg, m)
        bctx = _book_ctx(bid, ask)
        votes = brain.collect_votes(cfg, m, rsch, live_ctx)
        t = brain.tally(votes)
        log.info(
            "  票型: YES=%d NO=%d 弃权=%d → 领先=%s  平均估值=%s 分歧=%.3f",
            t["yes"],
            t["no"],
            t["abstain"],
            t["lead"],
            f"{t['mean_fair_price']:.3f}" if t["mean_fair_price"] is not None else "n/a",
            t["fair_price_spread"],
        )

        # 3) 扣扳机的是算术,不是 LLM(操盘手研究的元发现:问 LLM"该不该交易"
        #    它永远说不 —— 102 轮全 SKIP 就是证据)。desk 触发 → LLM 只审否决;
        #    没触发 → 记下带数字的理由,连审查那 $0.2 都省了。
        #    例外:该市场有持仓时仍走老 decide()(它会判断 SELL)。
        pos_here = store.position_usd(m.id, dry)
        if pos_here > 0:
            decision = brain.decide(
                cfg, m, rsch, votes, t, _position_ctx(store, dry), bctx
            )
        else:
            proposal, skip_why = desk.propose(t, bid, ask, cfg.risk)
            if proposal is None:
                log.info("  → 算术跳过: %s", skip_why)
                decision = brain.Decision(
                    action="SKIP", side=t.get("lead") or "YES", size_usd=0.0,
                    limit_price=0.0, confidence=0,
                    reason=f"desk: {skip_why}", model="desk-arithmetic",
                )
            else:
                log.info("  ⚡ 触发: %s", proposal.audit)
                decision = brain.review_proposal(cfg, m, rsch, votes, t, proposal, bctx)

        verdict = risk.check(
            cfg,
            m,
            decision,
            t,
            spent_today_usd=store.spent_today_usd(dry),
            position_in_market_usd=pos_here,
            open_positions=store.open_position_count(dry),
        )

        rid = store.log_round(
            market_id=m.id,
            question=m.question,
            yes_price=m.yes_price,
            volume_24h=m.volume_24h,
            tally=t,
            decision=decision,
            votes=votes,
            research=rsch,
            allowed=bool(verdict),
            risk_notes=verdict.reasons,
            dry_run=dry,
        )
        # 调用发生在 log_round 之前(那时还没 round id),这里回填
        store.attach_calls_to_round(rid)

        if not verdict:
            log.info("  → 不动作 (%s)", "; ".join(verdict.reasons))
            continue

        fill = execute.execute(cfg, m, decision)
        if fill.ok:
            store.log_fill(
                round_id=rid,
                market_id=m.id,
                question=m.question,
                side=decision.side if decision.action == "BUY" else "SELL",
                token_id=m.token_ids[0 if decision.side == "YES" else 1],
                size_usd=fill.size_usd,
                price=fill.price,
                dry_run=dry,
                order_id=fill.order_id,
                status=fill.status,
                raw=fill.raw,
            )
            log.info("  → 已记账 round=%d", rid)
        else:
            log.info("  → 未成交: %s", fill.error)


def show_status(cfg, store: Store) -> None:
    dry = cfg.risk.dry_run
    print(f"\n模式: {'DRY-RUN' if dry else '实盘'}   库: {store.path}")
    print(f"今日已用: ${store.spent_today_usd(dry):.2f} / "
          f"${cfg.risk.max_notional_per_day_usd}")
    pos = store.positions(dry)
    print(f"\n持仓 ({len(pos)}):")
    for p in pos:
        print(f"  {p['question'][:55]:<55} {p['side']:<4} "
              f"${p['net_usd']:>7.2f} @ {p['avg_price']:.3f}")
    if not pos:
        print("  (无)")

    with store._conn() as c:
        rows = c.execute(
            """SELECT ts, question, action, side, size_usd, confidence,
                      allowed, cost_usd
               FROM rounds ORDER BY id DESC LIMIT 12"""
        ).fetchall()
        total = c.execute("SELECT COALESCE(SUM(cost_usd),0) s FROM rounds").fetchone()["s"]
        nrounds = c.execute("SELECT COUNT(*) n FROM rounds").fetchone()["n"]
    print(f"\n最近 {len(rows)} 轮:")
    for r in rows:
        flag = "✓" if r["allowed"] else "·"
        print(f"  {flag} {r['ts'][5:16]} {r['question'][:42]:<42} "
              f"{r['action']:<4} {r['side'] or '':<3} "
              f"${r['size_usd'] or 0:>5.2f} conf={r['confidence'] or 0}")
    print(f"\n累计 {nrounds} 轮,LLM 成本当量 ${total:.2f}")
    if nrounds:
        print(f"平均 ${total/nrounds:.3f}/轮")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="polyvote — 多模型投票的 Polymarket 交易 agent")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--once", action="store_true", help="跑一轮就退出")
    ap.add_argument("--status", action="store_true", help="看 dry-run 攒的数据")
    ap.add_argument("--market", help="只分析这个 market id")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)

    try:
        cfg = load(args.config)
    except ConfigError as e:
        log.error("配置有问题: %s", e)
        return 2

    store = Store(cfg.data.get("db_path", "./polyvote.db"))
    # 把每次 LLM 调用的 token 记进库,喂观测台的命中率仪表
    brain.on_call = store.log_call

    if args.status:
        show_status(cfg, store)
        return 0

    if not cfg.risk.dry_run:
        log.warning("!" * 72)
        log.warning("!! 实盘模式 —— 会用真钱下单")
        log.warning("!! 单笔上限 $%s  日上限 $%s",
                    cfg.risk.max_notional_per_trade_usd,
                    cfg.risk.max_notional_per_day_usd)
        log.warning("!" * 72)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    if args.market:
        cfg.market_filter = dict(cfg.market_filter)
        cfg.market_filter["_only_id"] = args.market

    if args.once:
        run_round(cfg, store)
        return 0

    interval = int(cfg.loop.get("interval_minutes", 45)) * 60
    log.info("常驻模式,每 %d 分钟一轮。Ctrl-C 退出。", interval // 60)
    while not _stop:
        # 每轮开头重新 load —— 这样观测台改的 overrides.json 下一轮就生效,
        # 不用重启 bot。这正是"模型别写死、前端给选择权"的落地点。
        # load 失败(比如 config 被改坏)就沿用上一份,别让 bot 崩。
        try:
            cfg = load(args.config)
            brain.on_call = store.log_call
        except ConfigError as e:
            log.warning("重新 load 配置失败,沿用上一份: %s", e)

        try:
            run_round(cfg, store)
        except Exception:  # noqa: BLE001 — 单轮出事不能把常驻进程带走
            log.exception("本轮异常,继续下一轮")
        if _stop:
            break
        interval = int(cfg.loop.get("interval_minutes", 45)) * 60
        log.info("休息 %d 分钟…", interval // 60)
        for _ in range(interval):
            if _stop:
                break
            time.sleep(1)
    log.info("已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
