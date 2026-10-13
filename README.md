<p align="center">
  <a href="README.md"><img alt="English" src="https://img.shields.io/badge/English-1f6feb?style=for-the-badge"></a>
  <a href="README.zh-CN.md"><img alt="简体中文" src="https://img.shields.io/badge/%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-555555?style=for-the-badge"></a>
  <a href="README.ja.md"><img alt="日本語" src="https://img.shields.io/badge/%E6%97%A5%E6%9C%AC%E8%AA%9E-555555?style=for-the-badge"></a>
</p>

# polyvote

> I originally set out to give Polymarket a try, but the VPS latency problem never seemed to get solved. I'll restart the project when I get the chance.

**Status: paused (2026-10-13).** All services are stopped and the VPS has been shut down. The system only ever ran in **shadow mode**: it never read a wallet key and never placed a real order.

---

## What it is

polyvote is an experiment in automated trading on [Polymarket](https://polymarket.com) prediction markets. It went through two phases.

### Phase 1: slow markets, multi-model voting (retired)

This phase targeted ordinary event markets (elections, sports, news) and had several LLMs "deliberate" on the probability:

```
Haiku researches once  →  three models estimate independently  →  a decider model rules  →  hard risk gates  →  order
  (only one with web)      Claude Opus     ┐
                           Claude Fable    ├─ blind to market price, no tools
                           GPT (codex)     ┘
```

- Voters are **price-blind**, so they can't just echo the market price back.
- Calibration rules are adapted from the superforecaster prompts in [valory-xyz/mech-predict](https://github.com/valory-xyz/mech-predict).
- Instead of the LLM APIs, each vote is a one-shot `claude` / `codex` CLI subprocess running on a subscription.
- `risk.py` is the hard gate. Nothing a model says overrides it, and if any check fails the whole order is rejected.

This phase was retired on 2026-07-22 in favour of fast markets. Design notes and lessons learned are in [docs/slow-market-design.zh-CN.md](docs/slow-market-design.zh-CN.md) (Chinese).

### Phase 2: Fast V2, fast markets (the main part)

This phase targets Polymarket's **5-minute crypto Up/Down markets** (BTC, ETH, SOL, XRP). Each asset runs its own event-driven engine:

```
RTDS Chainlink / Binance prices ─┐
                                 ├─> timestamp checks ─> signal model ─> hard gates
CLOB WebSocket ─> local order book ┘                                     │
                                                                         ├─> SQLite intent + locked cash
                                                                         ├─> book state at simulated arrival (measured latency)
                                                                         ├─> level-by-level FAK fill / partial / reject
                                                                         └─> reconcile against official settlement
```

- **Local order book:** full snapshot plus incremental updates. Stale events, crossed books and desynced state are rejected.
- **Cash ledger:** SQLite in WAL mode. Cash is locked when an intent is placed, and a position exists only after a fill. Settlement is an idempotent transaction, and restarts never reset the ledger.
- **Latency breakdown:** decision compute, HTTP round trip, event-loop queueing and the platform's `seconds_delay` are measured separately. All of them are reported as P95, never as the best single sample.
- **Experiment design:** a champion strategy, an observer that keeps sampling with normalised $5 orders, and frozen-parameter holdout challengers. Automated reviews may only propose changes. They cannot edit the running strategy and can never switch on live trading.
- **Dashboard:** `fast_dashboard.html` shows account state, the order lifecycle, the latency breakdown and review results. It goes to a full-screen alert if data is more than 2 seconds stale.

It ran on AWS Lightsail in Dublin ($5/month, 2 vCPU, 512 MB), close to Polymarket's CLOB.

## Results and findings

These figures are as of 2026-08-01, the last complete measurement:

| Metric | Result |
|---|---|
| Observer simulated trades (officially settled) | 8,713 |
| Net P&L after fees, slippage and latency | **−$2,097** (ROI −5.1%) |
| 95% CI of per-trade P&L | [−$0.32, −$0.16], entirely negative |
| Average stated probability vs actual win rate | 70.9% vs 59.4% (overconfident by about 11.5 pp) |
| Controllable-path latency P95 (target < 100 ms) | 383–385 ms at the last full measurement; the same process swung between 83 ms and 848 ms |
| Champion ledger (finite cash) | $30.00 → $7.79, then permanently locked by a risk gate |
| Two holdout challengers | Both halted by their own loss gates |

**Latency.** Almost every code-level suspect was ruled out, including executor queueing, IPv4 vs IPv6, write amplification and connection ageing. What remained was mostly scheduling on a $5 shared host, where CPU steal often reached 50–84%. Quiet windows reached about 83 ms, so the target was achievable, but never consistently.

**The other half.** The retrospective also found that the signal was systematically overconfident about its win rate. The 4 pp edge it acted on was smaller than its roughly 11.5 pp calibration error. A restart would need both a host with stable latency and a reworked probability calibration.

Services were stopped on 2026-08-15, and the VPS was shut down on 2026-10-13.

## Repository layout

| Path | Contents |
|---|---|
| `fastbot.py` | Single-asset fast engine: event-driven decisions, warming up the next market, simulated latency fills, official settlement |
| `fastmulti.py` | Runs the four asset engines together as one supervised portfolio |
| `fastchallenger.py` | Frozen-parameter holdout challengers |
| `fastdash_server.py`, `fast_dashboard.html` | Fast-market dashboard |
| `polyvote/fast*.py` | Order book, signal strategy, cash ledger, observer, reviews, state writer |
| `fast_*.yaml` | Portfolio and challenger configuration |
| `run.py`, `dashboard.py`, `polyvote/{brain,desk,risk,…}.py` | Phase 1 slow-market system (retired) |
| `deploy/` | systemd user units, deploy script, SSH tunnel script |
| `tests/` | Unit tests |

## Running it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-fast.txt -r requirements.txt pytest
.venv/bin/python -m pytest tests -q

# fast markets (shadow mode)
.venv/bin/python fastmulti.py --portfolio fast_portfolio.yaml
.venv/bin/python fastdash_server.py --host 127.0.0.1 --port 8920 --state-dir .
```

The fast-market code has no live-order path (the retired slow-market `execute.py` does, behind `dry_run: false` plus a private key in an environment variable). The dashboard has no authentication, so keep it on `127.0.0.1` and use an SSH tunnel to view it from another machine. For server setup, see the unit files in `deploy/`.

## Documentation

Most of the docs are in Chinese. They are working notes written during development:

- [FAST_MARKET.md](FAST_MARKET.md): fast-market design, cash rules and evaluation gates
- [FAST_MARKET_MEMORY.md](FAST_MARKET_MEMORY.md): final state and full conclusions
- [FAST_MARKET_RESEARCH_LOG.md](FAST_MARKET_RESEARCH_LOG.md): round-by-round research log covering latency attribution, the calibration experiments and the shutdown review
- [docs/slow-market-design.zh-CN.md](docs/slow-market-design.zh-CN.md): slow-market design and lessons learned

`AGENTS.md`, `CLAUDE.md` and `HANDOFF.md` are working instructions for the AI coding assistants used during development, kept as-is.

## Disclaimer

This is a personal research project and not investment advice. Polymarket's terms of service restrict US persons from trading via the API or agents. Check the rules where you live before using any of this.

## License

[MIT](LICENSE). Use it however you like.
