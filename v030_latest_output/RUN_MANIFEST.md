# v0.30 source-separated result manifest

- GitHub Actions run: `31370318953` (run number 2)
- Source commit: `b0c7a141ee3118471be9fe7eff8e82b5e682207e`
- Window policy: development, validation, and locked stress each start from fresh equity
- Development selection cutoff: `2025-12-31 23:59:59 UTC`
- Validation window: `2026-01-01` through `2026-06-30`
- Locked stress window: `2026-07-01` onward; not used for selection
- Result: pipeline passed, every selected family/market research gate failed, live approval remains false

## Verified artifacts

| Scope | Artifact ID | SHA-256 |
| --- | ---: | --- |
| KR | `9056056979` | `bc21022a889e11f6aa471519b41e34c6d1c258b40fe74cc012427b33530e1bfa` |
| US | `9055981563` | `890feac35d7a7c42319c47304e8c1c8ff01620db7e143b1489462f49b2ba222b` |

The downloaded ZIP digests matched GitHub's artifact metadata exactly.

## Reproducibility note

Run number 1 is superseded. It carried the development account state into later windows. Run number 2 resets account state at every window boundary and is the only v0.30 result represented here.

The committed result set intentionally excludes large setup/equity and development-trade exports. It retains run configuration, separation audits, coverage/failures, development grids, selected-family validation tables, diagnostics, and validation trade ledgers.
