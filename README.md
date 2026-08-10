# Noramu LEVEL_RR v0.24 — Prospective Shadow

This repository runs the frozen Noramu LEVEL_RR v0.24 prospective shadow scanner on GitHub Actions.

## What it does

- Frozen v0.22 LEVEL_RR signal grammar; no result-driven parameter tuning.
- Frozen 147-stock US research universe.
- First successful run establishes the prospective baseline only.
- Later runs append only newly observed setup IDs after initialization.
- No live orders and no brokerage integration.
- Uses yfinance 60-minute data for research/shadow observation only; it is not an execution-grade feed.

## Automatic execution

`.github/workflows/noramu-shadow.yml` runs:

- immediately when the engine/workflow files are first pushed to `main`;
- manually from GitHub Actions via **Run workflow**;
- on US trading weekdays at several UTC checkpoints spanning EDT/EST sessions.

## Persistent files

Do not delete `state/` after the first successful run.

- `state/state.json`: initialization time, run count, and already-seen setup IDs.
- `state/shadow_signals.csv`: cumulative prospective signal ledger (created after signals exist).
- `latest_output/RUN_VALIDATION.txt`: latest run validation.
- `latest_output/shadow_run_summary.json`: latest scan summary.
- `latest_output/new_signals_this_run.csv`: only signals newly observed in that run.
- `latest_output/snapshot_by_ticker.csv`: per-ticker scan snapshot.
- `latest_output/failures.csv`: download/processing failures, if any.

The workflow commits `state/` and `latest_output/` back to the repository after each successful run, so GitHub runner resets do not erase the prospective experiment state.

## Expected first run

A successful first run should show `RUN_VALIDATION=PASS` and `first_run_baseline_only=1`. `new_signals_this_run=0` is expected on that first baseline run.
