# Toss Shadow Host on AWS Lightsail (Seoul)

Research/shadow only. No live order path is present.

## Recommended instance
- Region: Asia Pacific (Seoul) `ap-northeast-2`
- OS: Ubuntu 24.04 LTS
- Bundle: Linux/Unix with public IPv4, 1 GB RAM / 2 vCPU / 40 GB SSD (currently USD 7/month)
- Attach a Lightsail **Static IP** before registering the address with Toss.

## Why not standard GitHub-hosted Actions?
Toss OAuth rejected a standard GitHub-hosted runner with `403 access_denied: IP address not allowed`. Standard GitHub-hosted runner egress IPs are not stable enough for an allowlist. This host gives the market-data client one stable IPv4.

## Bootstrap
Connect to the instance with Lightsail browser SSH, then run:

```bash
sudo apt-get update && sudo apt-get install -y git
cd /tmp
git clone --branch agent/trading-engine-v004-fixed-ip-shadow-host --single-branch https://github.com/irotomokor-jpg/noramu-backtest.git
sudo bash noramu-backtest/deploy/lightsail/bootstrap.sh
```

## Static IP / Toss registration
1. Lightsail console > Networking > Create static IP.
2. Select the Seoul instance and attach it.
3. Confirm with:

```bash
curl -4 https://checkip.amazonaws.com
```

4. In Toss WTS > Settings > Open API, register/allow that exact IPv4 for the API client.

Do not register the transient GitHub Actions IP.

## Store credentials on the host
The bootstrap creates `/etc/noramu-shadow/toss.env` as root-only mode 0600. Edit it locally on the server:

```bash
sudo nano /etc/noramu-shadow/toss.env
```

Set only:
- `TOSS_CLIENT_ID`
- `TOSS_CLIENT_SECRET`

Do not paste those values into Git, logs, issues, or chat.

Then:

```bash
sudo systemctl restart noramu-shadow
sudo systemctl status noramu-shadow --no-pager
noramu-shadow-health
```

## Expected files
- `/var/lib/noramu-shadow/status.json` — heartbeat and error summary
- `/var/lib/noramu-shadow/state.json` — last-seen candle timestamp per symbol
- `/var/lib/noramu-shadow/feed/YYYY-MM-DD.jsonl` — normalized 1-minute BAR events

The daemon polls only during approximate regular-session windows unless `--force-markets` is used for a manual smoke check. It deduplicates candle timestamps across restarts.

## Manual read-only smoke
After the Toss IP allowlist is updated:

```bash
sudo -u noramu bash -lc 'set -a; source /etc/noramu-shadow/toss.env; set +a; /opt/noramu-shadow/.venv/bin/python /opt/noramu-shadow/toss_shadow_daemon_v001.py --root /var/lib/noramu-shadow --once --force-markets KR,US'
```

Then inspect:

```bash
noramu-shadow-health
```

## Safety contract
- `LIVE_APPROVAL=False`
- market-data endpoints only
- no `X-Tossinvest-Account` header
- no account/holdings/order/conditional-order endpoints
- no broker write calls

The next stage connects these normalized BAR events to the existing shadow runtime and frozen strategy adapters. It still does not enable live orders.
