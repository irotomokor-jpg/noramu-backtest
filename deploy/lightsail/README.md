# Toss Shadow Host on AWS Lightsail (Seoul)

Research/shadow only. No live order path is present.

## Recommended instance
- Region: Asia Pacific (Seoul) `ap-northeast-2`
- OS: Ubuntu 24.04 LTS
- Bundle: Linux/Unix with public IPv4, 1 GB RAM / 2 vCPU / 40 GB SSD (`micro_3_0`, currently USD 7/month)
- Attach a Lightsail **Static IP** before registering the address with Toss.

## Why not standard GitHub-hosted Actions?
Toss OAuth rejected a standard GitHub-hosted runner with `403 access_denied: IP address not allowed`. Standard GitHub-hosted runner egress IPs are not stable enough for an allowlist. This host gives the market-data client one stable IPv4.

## Fast provisioning with AWS CloudShell
AWS CloudShell already includes AWS CLI. After signing into AWS and opening CloudShell:

```bash
git clone --branch agent/trading-engine-v004-fixed-ip-shadow-host --single-branch https://github.com/irotomokor-jpg/noramu-backtest.git
bash noramu-backtest/deploy/lightsail/create_instance_cloudshell.sh
```

The script prints the selected monthly bundle price and requires typing `CREATE` before it creates a billable instance. It then waits for the instance, allocates a Lightsail Static IPv4, attaches it, and prints the exact address to register with Toss.

## Bootstrap the new host
Open the new Lightsail instance's browser SSH terminal, then run:

```bash
sudo apt-get update && sudo apt-get install -y git
cd /tmp
git clone --branch agent/trading-engine-v004-fixed-ip-shadow-host --single-branch https://github.com/irotomokor-jpg/noramu-backtest.git
sudo bash noramu-backtest/deploy/lightsail/bootstrap.sh
```

## Static IP / Toss registration
The CloudShell provisioner already attaches the Static IPv4. Confirm it from the Lightsail host with:

```bash
curl -4 https://checkip.amazonaws.com
```

In Toss WTS > Settings > Open API, register/allow that exact IPv4 for the API client. Do not register the transient GitHub Actions IP.

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

At KR regular-session time the service will immediately begin read-only 1-minute candle collection. Off-hours, it remains healthy but does not waste candle calls.

## Expected files
- `/var/lib/noramu-shadow/status.json` — heartbeat and error summary
- `/var/lib/noramu-shadow/state.json` — last-seen candle timestamp per symbol
- `/var/lib/noramu-shadow/feed/YYYY-MM-DD.jsonl` — runtime-compatible normalized 1-minute `BAR` events

The daemon deduplicates candle timestamps across restarts. Each `BAR` record uses the exact envelope consumed by `shadow_runtime_driver_v001.py` and stores Toss-only metadata separately under `meta`.

## Safety contract
- `LIVE_APPROVAL=False`
- market-data endpoints only
- no `X-Tossinvest-Account` header
- no account/holdings/order/conditional-order endpoints
- no broker write calls

The next stage adds the frozen Noramu/Doro/ETF signal producers to these BAR events. It still does not enable live orders.
