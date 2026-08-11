# Toss Market Data Adapter Setup v0.01

## Scope

Read-only market data for the shadow trading engine. No account header, account read, holdings read, or trading-write API is used in this stage.

## GitHub Actions secrets

Repository: `irotomokor-jpg/noramu-backtest`

Create these **Repository secrets**:

- `TOSS_CLIENT_ID` = the client ID issued in Toss Securities WTS Open API settings
- `TOSS_CLIENT_SECRET` = the client secret issued in Toss Securities WTS Open API settings

Do not commit either value to the repository, workflow YAML, issue, PR, or chat.

## GitHub UI path

`Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`

Create both names exactly as shown above.

## What the live smoke is allowed to call

- `POST /oauth2/token`
- `GET /api/v1/prices`
- `GET /api/v1/candles`
- optionally `GET /api/v1/trades`
- optionally `GET /api/v1/market-calendar/KR|US`

It must not use account-level headers or trading-write endpoints.

## Smoke symbols

- KR: `005930`
- US: `AAPL`, `TQQQ`, `SOXL`

The smoke output prints only symbol names, bar counts, and timestamps. It never prints the access token, client ID, or client secret.

## After secrets are registered

Run workflow `Trading Engine v0.03 Toss Market Data` with `workflow_dispatch`. The optional live smoke will authenticate, read prices and five recent 1-minute bars per symbol, and print `TOSS_LIVE_SMOKE=PASS` if successful.

## Still disabled

- live trading
- broker trading-write adapter
- account/holdings integration
- conditional trading-write features

`LIVE_APPROVAL=False`
