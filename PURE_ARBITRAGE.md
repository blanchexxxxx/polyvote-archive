# Complete-set arbitrage observer

`fastarb.py` is a keyless, shadow-only scanner for the structural complete-set
constraint on Polymarket binary markets:

- `BUY_SET`: buy equal outcome shares when asks plus taker fees and buffers cost
  less than one collateral unit, then merge the complete set.
- `SELL_SET`: split one collateral unit into equal outcome shares and sell both
  when bids minus taker fees and buffers exceed one unit.

The detector concept is adapted from the MIT-licensed
[`cryptuon/polybot`](https://github.com/cryptuon/polybot) arbitrage strategy.
The local implementation replaces its fixed fee estimate and top-of-book sizing
with the current per-market fee curve, equal-share multi-level depth, minimum
order size, a fixed-cost buffer, and fail-closed book receipt checks.

## Safety boundary

This process has no wallet, signer, key loader, order client, or live mode. A
positive price inequality is not atomic execution: Polymarket processes batched
orders in parallel, so one leg can fill while the other fails. Do not describe a
shadow opportunity as realized or risk-free profit.

The three historical champion/favorite/calibrated incident locks are unrelated
and remain permanent. This observer never reads, resets, or writes those ledgers.

## Run

```powershell
.\.venv\Scripts\python.exe fastarb.py --once
.\.venv\Scripts\python.exe fastarb.py
```

The continuous process writes `fast_arb_state.json` and records qualifying
snapshots in `fast_arb_observations.jsonl`; both are ignored by Git. Parameters
live in `fast_arb.yaml` and default to a `$30` maximum shadow capital model.

For systemd, install `deploy/polyvote-fast-arb.service`. Keep it separate from
the retired directional engine while collecting a new natural-time sample.
