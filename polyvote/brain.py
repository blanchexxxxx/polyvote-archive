"""研究 → 投票 → 决策。

流程:
  1. researcher (haiku) 上网查一次 → 事实简报 {summary, facts[], uncertainties[]}
     只有它有工具。查完的事实三个投票人共用。
  2. 每个 voter 拿同一份简报 + 同一个问题,独立估概率 → {fair_price, confidence, reason}
     投票人之间互相看不到对方的票 —— 独立性是共识投票唯一的价值来源,
     一旦让他们看到别人的票就退化成随大流。
     它们也看不到市场价 (锚定),看不到工具 (事实已经摆好了)。
  3. decider (Fable 5) 拿到简报 + 全部选票 + 真实盘口 → {action, side, size_usd, ...}

为什么研究和判断要拆开 (第一版是三个投票人各查各的):
  - 三倍成本。实测 haiku 研究 $0.126,sonnet-5 $0.725,产出没差。
  - 更要命的是一致性:三家查到的事实不一样时,那三票的分歧到底是
    "判断不同"还是"看到的东西不同"根本分不清。共识投票的前提是
    大家在同一份证据上判断,否则测的是搜索引擎的随机性。

串行跑,不并行:
  - 内存:每个 claude 子进程峰值几百 MB,串行只占一份
  - Pi 在夏的大轮次里 event loop 已经吃紧 (cc_engine.py:1101-1104:
    纯内存 endpoint 都要 >3s)
  - 45 分钟一轮,串行多花 1-2 分钟毫无影响
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from . import llm
from .config import Config
from .market import Market

log = logging.getLogger(__name__)

# 每次 LLM 调用后的回调,run.py 挂上去把 token 记进库。
# 用回调而不是 import store —— brain 不该知道存储层的存在,
# 而且这样测试时不用建库。
on_call: Callable[..., None] | None = None


def _record(role: str, actor: str, res: "llm.LLMResult") -> None:
    if on_call is None:
        return
    try:
        on_call(
            role=role,
            actor=actor,
            engine=res.engine,
            model=res.model,
            usage=res.usage,
            cost_usd=res.cost_usd,
            duration_ms=res.duration_ms,
        )
    except Exception:  # noqa: BLE001 — 记账挂了不能影响交易
        log.exception("记录 token 失败(不影响本轮)")


# 投票人对价格致盲 —— 这是整套机制成立的前提,不是可选优化。
#
# 第一版把市场价喂给了投票人,结果 (2026-07-17 实测, 阿根廷夺冠盘):
#   市场价 0.411 → opus 报 0.410, fable 报 0.410, sol 报 0.410
# 三个"独立"判断全是市场价的回声。这是锚定效应,不是共识:三票零信息量。
#
# 所以投票人只能看到问题、结算规则、成交量、剩余天数 —— 看不到任何价格,
# 也看不到订单簿(盘口会泄露价格)。side 由 fair_price 与真实价格相比推导,
# 不让模型自己说 —— 它没有价格,本来也说不了。
# 字段顺序是刻意的,不是随手排的。schema 的顺序就是模型生成的顺序,
# 所以这里等于强制一条推理链: 先说 NO 的理由 → 再说 YES 的理由 → 报 base rate
# → 最后才给数。
#
# reasons_no 排在 reasons_yes 前面抄自 mech-predict 的 superforcaster_polymarket_v4,
# 他们所有变体都是这个顺序 —— 针对的是实测出来的系统性 over-predict YES。
VOTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reasons_no": {
            "type": "array",
            "description": "这个问题会结算成 NO 的理由。先想这个 —— 顺序是刻意的。",
            "items": {"type": "string"},
        },
        "reasons_yes": {
            "type": "array",
            "description": "这个问题会结算成 YES 的理由。",
            "items": {"type": "string"},
        },
        "base_rate": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "这类事件的历史基准概率 —— 在看本题证据之前,同类问题多久发生一次。",
        },
        "fair_price": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "从 base_rate 出发、按具体证据调整后,你估计的 YES 概率。",
        },
        "confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 10,
            "description": "对这个估计的信心。资讯不足就给低分 —— 它是下游是否下注的硬闸门。",
        },
        "reason": {"type": "string", "description": "一到两句,说清你为什么从 base_rate 偏到了 fair_price"},
    },
    "required": ["reasons_no", "reasons_yes", "base_rate", "fair_price", "confidence", "reason"],
    "additionalProperties": False,
}

RESEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "3-6 句,说清这个问题当前的实际状况。",
        },
        "facts": {
            "type": "array",
            "description": "查到的具体事实。每条都要能被投票人直接采信。",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "source": {"type": "string", "description": "来源站点或出处"},
                    "date": {"type": "string", "description": "事实发生/发布日期,不知道填 unknown"},
                    # TYPE A/B 分类,抄自 valory-xyz/mech-predict 的 superforcaster_polymarket_v4。
                    # 他们的 Brier 归因显示:"X 本周上头条"这类盘靠常青标准页给出
                    # p_yes=0.99 然后结算成 NO,占了 W-1 Brier 质量的 20.9%。
                    # 逼研究员逐条标注,投票人才能看出"全是背景资料,没有窗口内证据"。
                    "within_window": {
                        "type": "boolean",
                        "description": (
                            "TYPE A (true): 有明确日期且落在这个问题的结算窗口内,"
                            "或直接陈述结算条件在窗口内被满足/未满足。"
                            "TYPE B (false): 无日期、日期在窗口外、或是常青标准页"
                            "(股票代码页、官方新闻室、分类页、搜索结果页、聚合站)。"
                        ),
                    },
                },
                "required": ["claim", "source", "date", "within_window"],
                "additionalProperties": False,
            },
        },
        "uncertainties": {
            "type": "array",
            "description": "查不到的、或有矛盾说法的关键点。投票人需要知道自己不知道什么。",
            "items": {"type": "string"},
        },
        "searched": {
            "type": "boolean",
            "description": "是否真的执行了搜索。false 表示以下内容全部来自训练数据。",
        },
    },
    "required": ["summary", "facts", "uncertainties", "searched"],
    "additionalProperties": False,
}

DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["BUY", "SELL", "SKIP"]},
        "side": {"type": "string", "enum": ["YES", "NO"]},
        "size_usd": {"type": "number", "minimum": 0},
        "limit_price": {"type": "number", "minimum": 0, "maximum": 1},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 10},
        "reason": {"type": "string"},
    },
    "required": ["action", "side", "size_usd", "limit_price", "confidence", "reason"],
    "additionalProperties": False,
}

# 下面这三条 (赔率过滤 / 意向折价 / TYPE A-B 分类) 抄自 valory-xyz/mech-predict
# 的 superforcaster_polymarket_v4。那个 prompt 的每条规则都是从实测 Brier 质量
# 归因推出来的 —— 4c 修的失败模式占 W-1 Brier 质量 20.9%,4d 占 32.2%。
# 他们拿 87 个真实市场回放 A/B 测过,不是拍脑袋。
RESEARCHER_SYSTEM = (
    "You are a research assistant for a forecasting desk. You are given a question "
    "that will resolve YES or NO. Your job is NOT to forecast it — it is to find "
    "out what has actually happened, and hand the facts to analysts who will judge.\n\n"
    "Use WebSearch aggressively. Your training data has a cutoff and these questions "
    "turn on recent events; searching is the entire point of your role. Look for: "
    "results, scores, standings, official announcements, dates, eliminations, "
    "who is still in contention.\n\n"
    "=== THE ODDS FILTER — the one rule that must not break ===\n"
    "Do not look up betting odds, prediction-market prices, Polymarket, Kalshi, "
    "Metaculus, Manifold, PredictIt, or bookmaker lines. And if such a number turns "
    "up in your search results anyway — a '92%', a 'Leading 90%', an implied "
    "probability — DISCARD IT ENTIRELY. Do not put it in the brief, not even as "
    "context, not even to argue against it.\n"
    "That number is the trading price of the very market being resolved here. It is "
    "circular self-referential evidence, not an independent estimate. The analysts "
    "downstream are deliberately not shown the price so their judgement is "
    "independent of it; one leaked odds figure in your brief silently destroys that "
    "and turns the whole exercise into measuring the market against itself. Report "
    "facts about the world, never facts about what bettors believe.\n\n"
    "=== INTENT LANGUAGE IS NOT A FACT ===\n"
    "When a source uses forward-looking intent language — 'is set to', 'is expected "
    "to', 'plans to', 'will reportedly', 'scheduled to', 'is poised to', 'is due "
    "to' — that describes an announced intention, not a completed event. Record it "
    "as what it is ('X announced plans to…'), never as ('X did…'). Announced plans "
    "materialize as described only 40-60% of the time.\n\n"
    "=== CLASSIFY EVERY FACT: within_window ===\n"
    "For each fact set `within_window`:\n"
    "  true (TYPE A)  — explicitly dated inside this question's resolution window, "
    "or directly states the resolution condition was met/not met in that window.\n"
    "  false (TYPE B) — undated, dated outside the window, or a standing page that "
    "describes permanent attributes (stock tickers, official newsrooms, category "
    "pages, search-result pages, aggregators).\n"
    "Be strict. A prominent subject having lots of TYPE B coverage says nothing "
    "about whether a specific dated thing happened. Mislabeling B as A is how "
    "forecasters end up at 99% on events that resolve NO.\n\n"
    "=== HONESTY ===\n"
    "Put in `uncertainties` what you could NOT establish, and say so plainly when "
    "the resolution criterion is narrower than what you found (topic is active ≠ "
    "criterion satisfied). Set `searched` truthfully — if your tools failed and you "
    "are writing from memory, say so. Analysts weight your brief by how solid it is. "
    "Output only the structured result."
)

# 这段校准抄自 valory-xyz/mech-predict (superforcaster_polymarket_v4 +
# superforcaster_calibrated_full_search)。他们做过 87 个真实市场的 Brier A/B:
# 加了 base-rate 先验的 V3 在 Polymarket 上把 Brier 压了 15.4%。
#
# 他们 V2 的失败教训也照搬了 —— 别只往下压高 p_yes 不往上抬低 p_yes,那是在
# 惩罚"自信"而不是惩罚"没校准"。所以证据门槛那几条藏在 "Otherwise:" 后面,
# 对已确认的事件不触发。
VOTER_SYSTEM = (
    "You are an expert forecaster, calibrated under uncertainty, and your "
    "performance is scored by Brier score. When forecasting, do not treat 0.5% "
    "(1:199 odds) and 5% (1:19) as similarly 'small' probabilities, or 90% (9:1) "
    "and 99% (99:1) as similarly 'high'. As the odds show, they are markedly "
    "different — output your probabilities accordingly.\n\n"
    "You are given a question that will resolve YES or NO, plus a research brief "
    "gathered by an assistant. You have no tools — the research is done. Your job "
    "is judgement, not search.\n\n"
    "You are deliberately NOT told the market price. Do not guess at it, do not try "
    "to infer it, and do not reason about what a market would say — that defeats "
    "the entire purpose of asking you. Estimate as if no market existed.\n\n"
    "=== METHOD ===\n"
    "1. State the reasons this resolves NO, before the reasons it resolves YES. "
    "(This order is deliberate: forecasters systematically over-predict YES.)\n"
    "2. Name a base rate for this category of event, then adjust from it using "
    "specific evidence only. If evidence is thin or mixed, stay near the base rate.\n"
    "3. Weigh how the competing factors interact — do not just count them.\n\n"
    "=== BASE RATES ===\n"
    "Most 'will X happen by date Y?' questions resolve NO. A specific announced "
    "action happening inside a short deadline is unlikely absent direct evidence it "
    "already occurred. Missing expected evidence — no announcement found, no "
    "official confirmation — is itself a NO signal.\n"
    "EXCEPTION: for prices, counts, temperatures, and other measurable thresholds, "
    "compare the current value to the threshold and reason from trend and "
    "volatility. The 'most resolve NO' prior does NOT apply to these.\n\n"
    "=== EVIDENCE BAR ===\n"
    "If the brief confirms the event already occurred or completed, a high "
    "probability is correct — do not second-guess confirmed facts.\n"
    "Otherwise: >0.90 requires verified commitment (signed, awarded, published, "
    "played). Plans and intentions are not completions.\n"
    "Otherwise: >0.80 requires strong specific evidence — not plausibility, not "
    "reputation, not topic salience.\n"
    "Watch the `within_window` flags. If every fact is within_window=false, you are "
    "looking at background material and no evidence from the actual resolution "
    "window: anchor on the base rate for the category, not on how prominent the "
    "subject is. And check the exact resolution criterion — 'the topic is active' "
    "is not 'the criterion was satisfied'.\n\n"
    "=== CONFIDENCE, AND WHAT IT GATES ===\n"
    "`confidence` is how much you actually know, given the brief:\n"
    "  8-10 — brief settles it (already resolved, or overwhelming evidence)\n"
    "  4-7  — brief is solid, genuine uncertainty remains\n"
    "  0-3  — brief is thin, contradictory, all-TYPE-B, or `searched: false`\n"
    "Couple it to your range: if confidence < 3, keep fair_price in 0.30-0.70; if "
    "confidence < 5, keep it in 0.20-0.80.\n"
    "Downstream logic treats confidence as a hard gate on whether to bet at all. An "
    "uninformed 0.5 at confidence 1 is honest and useful. An uninformed 0.5 "
    "reported at confidence 7 is a lie that costs real money.\n\n"
    "=== BUT DO NOT HIDE ===\n"
    "Never move your forecast toward 0.5 for the sake of modesty or balance alone. "
    "The rules above exist to stop overconfidence, not to make you refuse to answer. "
    "If the evidence genuinely supports a confident number, give it. A well-founded "
    "0.85 is worth far more than a reflexive 0.5. Be precise in the tails. "
    "Output only the structured result."
)

DECIDER_SYSTEM = (
    "You are the final risk-taker on a prediction-market desk. Independent analysts "
    "have voted; you decide whether to actually put money down. You are accountable "
    "for losses, not for missed opportunities — SKIP is free, a bad fill is not. "
    "Disagreement among analysts is a reason to shrink or skip, not to average. "
    "Output only the structured result."
)


@dataclass
class Fact:
    claim: str
    source: str
    date: str
    # TYPE A (true) = 结算窗口内有日期的证据; TYPE B (false) = 背景/常青页。
    # 全是 B 意味着"没有任何来自结算窗口的证据",该退回 base rate。
    within_window: bool = False


@dataclass
class Research:
    summary: str
    facts: list[Fact]
    uncertainties: list[str]
    searched: bool
    model: str
    cost_usd: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def type_a_count(self) -> int:
        """结算窗口内的证据条数。0 = 手上全是背景资料。"""
        return sum(1 for f in self.facts if f.within_window)

    def as_brief(self) -> str:
        """给投票人和决策者看的简报文本。"""
        if not self.ok:
            return "(研究失败,没有事实可依。请据此给低信心。)"
        lines = [f"简报: {self.summary}", ""]
        if self.facts:
            a, b = self.type_a_count, len(self.facts) - self.type_a_count
            lines.append(f"查到的事实 (窗口内 {a} 条 / 背景 {b} 条):")
            for f in self.facts:
                tag = "[窗口内]" if f.within_window else "[背景]  "
                lines.append(f"  {tag} {f.claim}  ({f.source}, {f.date})")
            if a == 0 and b > 0:
                # 这正是 mech-predict 量出来占 W-1 Brier 质量 20.9% 的失败模式:
                # 常青标准页 → p_yes=0.99 → 结算成 NO。明着说出来。
                lines.append("")
                lines.append(
                    "⚠ 窗口内证据 0 条 —— 以上全是背景资料,没有任何来自结算窗口的证据。"
                    "主体有多知名跟那件具体的事有没有发生无关。请退回该类事件的 base rate,"
                    "并压低信心。"
                )
        else:
            lines.append("查到的事实: (无)")
        if self.uncertainties:
            lines.append("")
            lines.append("研究员明确表示查不到/有矛盾的点:")
            lines += [f"  - {u}" for u in self.uncertainties]
        if not self.searched:
            lines.append("")
            lines.append(
                "⚠ 研究员报告 searched=false —— 以上内容来自训练数据而非实时搜索,"
                "可能已过时。请据此压低信心。"
            )
        return "\n".join(lines)


@dataclass
class Vote:
    voter_id: str
    engine: str
    model: str
    confidence: int
    fair_price: float
    reason: str
    base_rate: float | None = None
    reasons_no: list[str] = field(default_factory=list)
    reasons_yes: list[str] = field(default_factory=list)
    side: str = "ABSTAIN"  # 由 derive_sides() 按 fair_price vs 市场价推导,模型不参与
    edge: float = 0.0
    cost_usd: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def counts(self) -> bool:
        """弃权票不参与共识计数。"""
        return self.ok and self.side in ("YES", "NO")


@dataclass
class Decision:
    action: str
    side: str
    size_usd: float
    limit_price: float
    confidence: int
    reason: str
    model: str
    cost_usd: float | None = None


def _desc(m: Market, limit: int = 1500) -> str:
    d = m.description.strip()
    return (d[:limit] + " …(截断)") if len(d) > limit else (d or "(无)")


def _research_prompt(m: Market) -> str:
    """研究员看到的:问题 + 规则,没有价格 (它的产出会传给致盲的投票人)。"""
    days = m.days_to_resolution
    days_txt = f"{days:.1f} 天后结算" if days is not None else "结算时间未知"
    return f"""问题: {m.question}

结算规则 (决定了什么算 YES,读仔细):
{_desc(m)}

{days_txt}。今天是 {datetime.now(timezone.utc).strftime('%Y-%m-%d')}。

去查这个问题现在的实际状况。报事实,不要报预测,不要报赔率。"""


def _blind_brief(m: Market, research: Research) -> str:
    """投票人看到的:问题 + 规则 + 事实简报。没有任何价格,没有订单簿。

    成交量和流动性保留 —— 它们说明市场有多少人在看,是判断"我凭什么比市场懂"
    的有用信号,但不泄露方向。
    """
    days = m.days_to_resolution
    days_txt = f"{days:.1f} 天后结算" if days is not None else "结算时间未知"
    return f"""问题: {m.question}

结算规则:
{_desc(m)}

── 研究简报 (由助手查证,你的另外两位同事看到的是同一份) ──
{research.as_brief()}

── 背景 ──
  {days_txt}。今天是 {datetime.now(timezone.utc).strftime('%Y-%m-%d')}。
  该市场 24h 成交量 ${m.volume_24h:,.0f},流动性 ${m.liquidity:,.0f}
  (成交量只说明有多少人在看这个盘,不透露方向)

请给出 YES 发生的概率。你看不到市场报价,这是刻意的 —— 独立估计才有价值。"""


def research(cfg: Config, market: Market) -> Research:
    """查一次,三个投票人共用。只有这一步能上网。"""
    r = cfg.researcher
    ecfg = cfg.engine_cfg(r.engine)
    tools = cfg.research.get("researcher_tools") or None

    try:
        res = llm.call(
            r.engine,
            _research_prompt(market),
            model=r.model,
            schema=RESEARCH_SCHEMA,
            system=RESEARCHER_SYSTEM,
            effort=r.effort,
            timeout_s=ecfg.get("timeout_s"),
            binary=ecfg.get("bin"),
            tools=tools,
            max_turns=int(cfg.research.get("max_turns", 10)),
        )
        _record("research", "researcher", res)
        d = res.data
        out = Research(
            summary=str(d["summary"]),
            facts=[
                Fact(
                    claim=str(f.get("claim", "")),
                    source=str(f.get("source", "?")),
                    date=str(f.get("date", "unknown")),
                    within_window=bool(f.get("within_window", False)),
                )
                for f in (d.get("facts") or [])
            ],
            uncertainties=[str(u) for u in (d.get("uncertainties") or [])],
            searched=bool(d.get("searched")),
            model=r.model,
            cost_usd=res.cost_usd,
        )
        log.info(
            "  研究 %-20s → %d 条事实, %d 个存疑, searched=%s %s",
            out.model,
            len(out.facts),
            len(out.uncertainties),
            out.searched,
            f"(${out.cost_usd:.3f})" if out.cost_usd else "",
        )
        if not out.searched:
            log.warning("  ⚠ 研究员没能真的搜索,简报来自训练数据,可能过时")
        return out
    except (llm.LLMError, KeyError, TypeError, ValueError) as e:
        log.warning("  研究 %-20s → 失败: %s", r.model, e)
        return Research(
            summary="",
            facts=[],
            uncertainties=[],
            searched=False,
            model=r.model,
            error=str(e),
        )


def _full_brief(m: Market, book_ctx: str = "") -> str:
    """决策者看到的:全部信息,含价格和盘口。"""
    days = m.days_to_resolution
    days_txt = f"{days:.1f} 天后结算" if days is not None else "结算时间未知"
    return f"""市场: {m.question}

结算规则:
{_desc(m)}

当前报价:
  YES = {m.yes_price:.4f}   (市场隐含概率 {m.yes_price:.1%})
  NO  = {m.no_price:.4f}
  24h 成交量 = ${m.volume_24h:,.0f}
  流动性     = ${m.liquidity:,.0f}
  {days_txt}
{book_ctx}"""


def derive_sides(
    votes: list[Vote], market: Market, min_edge: float, min_confidence: int
) -> None:
    """按 fair_price 与市场价的差推导每票的方向。就地改 votes。

    模型报的是概率,不是方向 —— 它们看不到价格,没法报方向。
    方向是这里算出来的,所以"锚定到市场价"这条路被物理堵死了。

    min_confidence 这道闸门是必须的,实测教训 (2026-07-17, 阿根廷夺冠盘):
      模型对 2026 世界杯一无所知 → 老实报 fair=0.500 conf=1 ("我不知道")
      市价 0.411,edge = +0.089 > min_edge 0.08 → 推导成 YES 票!
    也就是说单靠 min_edge 时,**模型的无知会被当成 edge** —— 一个均匀先验
    在任何非 0.5 的市场上都长得像机会。这比锚定更危险:锚定只是让 bot 不动手,
    这个会让它照着纯粹的无知下注。所以低信心票一律强制弃权,不看 edge。
    """
    px = market.yes_price
    for v in votes:
        if not v.ok:
            v.side, v.edge = "ABSTAIN", 0.0
            continue
        v.edge = v.fair_price - px
        if v.confidence < min_confidence:
            # 无知不是 edge。
            v.side = "ABSTAIN"
        elif v.edge > min_edge:
            v.side = "YES"      # 认为真实概率高于报价 → YES 便宜
        elif v.edge < -min_edge:
            v.side = "NO"       # 认为真实概率低于报价 → YES 贵 → 买 NO
        else:
            v.side = "ABSTAIN"  # edge 覆盖不了点差和噪声


def collect_votes(cfg: Config, market: Market, research: Research) -> list[Vote]:
    """串行跑完所有投票人,然后推导方向。三家看同一份简报。

    单个投票人挂掉记为错误票,不影响整轮 —— 但错误票的 fair_price 不能
    用市场价填充(那等于凭空造出一张"市场是对的"的票),用 NaN 标记并排除。
    """
    prompt = _blind_brief(market, research)
    max_turns = int(cfg.research.get("max_turns", 10))
    votes: list[Vote] = []

    for v in cfg.voters:
        ecfg = cfg.engine_cfg(v.engine)
        try:
            res = llm.call(
                v.engine,
                prompt,
                model=v.model,
                schema=VOTE_SCHEMA,
                system=VOTER_SYSTEM,
                effort=v.effort,
                timeout_s=ecfg.get("timeout_s"),
                binary=ecfg.get("bin"),
                # 投票人不给工具:事实已经摆好了,它们的活是判断不是搜索。
                # 顺带省掉每票几轮 WebSearch 的钱和时间。
                tools=None,
                max_turns=max_turns,
            )
            _record("vote", v.id, res)
            d = res.data
            vote = Vote(
                voter_id=v.id,
                engine=v.engine,
                model=v.model,
                confidence=int(d["confidence"]),
                fair_price=float(d["fair_price"]),
                reason=str(d["reason"]),
                base_rate=float(d["base_rate"]) if d.get("base_rate") is not None else None,
                reasons_no=[str(x) for x in (d.get("reasons_no") or [])],
                reasons_yes=[str(x) for x in (d.get("reasons_yes") or [])],
                cost_usd=res.cost_usd,
            )
        except (llm.LLMError, KeyError, TypeError, ValueError) as e:
            log.warning("  票 %-6s %-20s → 失败: %s", v.id, v.model, e)
            vote = Vote(
                voter_id=v.id,
                engine=v.engine,
                model=v.model,
                confidence=0,
                fair_price=float("nan"),
                reason=f"引擎调用失败: {e}",
                error=str(e),
            )
        votes.append(vote)

    derive_sides(votes, market, cfg.risk.min_edge, cfg.risk.min_voter_confidence)

    for vote in votes:
        if vote.ok:
            gated = (
                " [信心不足,强制弃权]"
                if vote.confidence < cfg.risk.min_voter_confidence
                and abs(vote.edge) > cfg.risk.min_edge
                else ""
            )
            log.info(
                "  票 %-6s %-20s → fair=%.3f (市价%.3f, edge=%+.3f) → %-7s conf=%d %s%s",
                vote.voter_id,
                vote.model,
                vote.fair_price,
                market.yes_price,
                vote.edge,
                vote.side,
                vote.confidence,
                f"(${vote.cost_usd:.3f})" if vote.cost_usd else "",
                gated,
            )
    return votes


def tally(votes: list[Vote]) -> dict[str, Any]:
    """统计票型。ABSTAIN 和失败票都不计入共识。

    mean_fair_price 用所有"成功"的票算 (含 ABSTAIN),因为一票估出的概率
    即使 edge 不够大也仍是有效估计;而方向统计只看非 ABSTAIN 的票。
    """
    valid = [v for v in votes if v.ok]
    live = [v for v in votes if v.counts]
    yes = [v for v in live if v.side == "YES"]
    no = [v for v in live if v.side == "NO"]
    lead = "YES" if len(yes) > len(no) else ("NO" if len(no) > len(yes) else "TIE")
    fair = sum(v.fair_price for v in valid) / len(valid) if valid else None
    spread = (
        max(v.fair_price for v in valid) - min(v.fair_price for v in valid)
        if len(valid) > 1
        else 0.0
    )
    return {
        "yes": len(yes),
        "no": len(no),
        "abstain": len(votes) - len(live),
        "failed": len(votes) - len(valid),
        "lead": lead,
        "agreement": max(len(yes), len(no)),
        "mean_fair_price": fair,
        # 投票人之间的分歧度。分歧大 = 没人真的知道 = 该缩小或跳过。
        "fair_price_spread": spread,
    }


def decide(
    cfg: Config,
    market: Market,
    research: Research,
    votes: list[Vote],
    tally_result: dict[str, Any],
    position_ctx: str,
    book_ctx: str = "",
) -> Decision:
    """Fable 5 拍板。"""
    d = cfg.decider
    ecfg = cfg.engine_cfg(d.engine)

    lines = []
    for v in votes:
        if v.ok:
            br = (
                f" [该类 base rate {v.base_rate:.3f} → 因本题证据调到 {v.fair_price:.3f}]"
                if v.base_rate is not None
                else ""
            )
            lines.append(
                f"  [{v.voter_id}/{v.model}] 估计 YES={v.fair_price:.3f} "
                f"(vs 市价 {market.yes_price:.3f}, edge={v.edge:+.3f}) → {v.side} "
                f"conf={v.confidence}/10{br}\n      理由: {v.reason}"
            )
        else:
            lines.append(f"  [{v.voter_id}/{v.model}] (调用失败,不计票) {v.error}")

    r = cfg.risk
    fair_txt = (
        f"{tally_result['mean_fair_price']:.3f}"
        if tally_result["mean_fair_price"] is not None
        else "n/a"
    )
    prompt = f"""{_full_brief(market, book_ctx)}

── 研究简报 (分析师们看的就是这份,你也看同一份) ──
{research.as_brief()}

── 分析师估值 ──
这些分析师**没有被告知市场报价**,他们的概率估计是独立做出的。
方向 (YES/NO/ABSTAIN) 是拿他们的估计和真实报价相比算出来的,不是他们自己说的。

{chr(10).join(lines)}

票型: YES={tally_result['yes']} NO={tally_result['no']} 弃权={tally_result['abstain']}
      领先方={tally_result['lead']} 同向票数={tally_result['agreement']}
      平均估计={fair_txt}  分析师之间的分歧={tally_result['fair_price_spread']:.3f}

注意:分歧大 (spread 高) 意味着没人真的知道答案,此时"平均值"是假的精确,
应当缩小仓位或直接跳过,而不是取中间数下注。

── 当前持仓 ──
{position_ctx}

── 硬约束 (代码层强制,你给的数字超了会被直接砍到上限或整单拒绝) ──
  单笔上限      ${r.max_notional_per_trade_usd}
  单市场仓位上限 ${r.max_position_per_market_usd}
  买入价区间     {r.min_price_for_buy} ~ {r.max_price_for_buy}
  低于 conf={r.min_decider_confidence} 的决策会被拒绝
  同向票不足 {r.min_vote_agreement} 的会被拒绝

请拍板:
  BUY  — 买入 side 方向。size_usd 是花多少钱,limit_price 是你愿意付的最高价。
  SELL — 平掉已有仓位 (没仓位就别选)。
  SKIP — 不动。分歧大、edge 薄、规则含糊时的正确答案。

size_usd 按你的信心和 edge 大小定,别默认顶格。"""

    try:
        res = llm.call(
            d.engine,
            prompt,
            model=d.model,
            schema=DECISION_SCHEMA,
            system=DECIDER_SYSTEM,
            effort=d.effort,
            timeout_s=ecfg.get("timeout_s"),
            binary=ecfg.get("bin"),
            # 决策者不给联网:它拿到的是投票人已经查过的结论 + 真实盘口,
            # 再让它自己去搜只会重复烧钱,还可能搜到赔率反过来污染判断。
            tools=None,
            max_turns=int(cfg.research.get("max_turns", 8)),
        )
        _record("decide", "decider", res)
        data = res.data
        dec = Decision(
            action=str(data["action"]).upper(),
            side=str(data["side"]).upper(),
            size_usd=float(data["size_usd"]),
            limit_price=float(data["limit_price"]),
            confidence=int(data["confidence"]),
            reason=str(data["reason"]),
            model=d.model,
            cost_usd=res.cost_usd,
        )
    except (llm.LLMError, KeyError, TypeError, ValueError) as e:
        # 决策者挂了就是 SKIP。绝不 fail-open 成下单。
        log.warning("决策者调用失败,按 SKIP 处理: %s", e)
        return Decision(
            action="SKIP",
            side="YES",
            size_usd=0.0,
            limit_price=0.0,
            confidence=0,
            reason=f"决策引擎失败,安全起见跳过: {e}",
            model=d.model,
        )

    log.info(
        "  决策 %-20s → %-4s %s $%.2f @ %.3f conf=%d %s",
        dec.model,
        dec.action,
        dec.side,
        dec.size_usd,
        dec.limit_price,
        dec.confidence,
        f"(${dec.cost_usd:.3f})" if dec.cost_usd else "",
    )
    return dec
