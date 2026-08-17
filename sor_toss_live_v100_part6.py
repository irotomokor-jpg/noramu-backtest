def mark_trend_exits(state: dict[str, Any]) -> None:
    for ticker, p in list(state.get("positions", {}).items()):
        if bool(p.get("exitNextOpen")):
            continue
        try:
            original = v4.ATR_RATIO_MAX; v4.ATR_RATIO_MAX = ATR_RATIO_MAX
            try:
                df = v4.add_sor_setup(completed_daily_df(ticker))
            finally:
                v4.ATR_RATIO_MAX = original
            if len(df) and not bool(df["trend"].iloc[-1]):
                p["exitNextOpen"] = True
                p["trendOffSignalDate"] = pd.Timestamp(df.index[-1]).strftime("%Y-%m-%d")
                log_event("TREND_OFF_MARKED", ticker=ticker, signalDate=p["trendOffSignalDate"])
        except Exception as exc:
            log_event("TREND_CHECK_ERROR", ticker=ticker, error=repr(exc))
    save_state(state)


def setup(client: TossLiveClient, account_seq: int | None, arm_live: bool) -> None:
    """One-time live configuration. Only ordinary BROKERAGE accounts are accepted."""
    accounts = client.accounts()
    if not accounts:
        raise RuntimeError("No Toss account returned by API")
    tradeable = [a for a in accounts if str(a.get("accountType") or "").upper() == "BROKERAGE"]
    print("AVAILABLE ACCOUNTS")
    for a in accounts:
        print({
            "accountSeq": a.get("accountSeq"),
            "accountNo": masked_account_no(a.get("accountNo", "")),
            "accountType": a.get("accountType"),
            "liveEligible": a in tradeable,
        })
    if not tradeable:
        raise RuntimeError("No BROKERAGE account is available for SOR live trading")
    if account_seq is None:
        if len(tradeable) == 1:
            account_seq = int(tradeable[0]["accountSeq"])
        else:
            account_seq = int(input("Select BROKERAGE accountSeq: ").strip())
    valid = {int(a["accountSeq"]) for a in tradeable}
    if int(account_seq) not in valid:
        raise RuntimeError(f"accountSeq {account_seq} is not an eligible BROKERAGE account")
    cfg = LiveConfig(accountSeq=int(account_seq), liveEnabled=bool(arm_live))
    validate_frozen_config(cfg)
    atomic_json(CONFIG_PATH, cfg.__dict__)
    if not STATE_PATH.exists():
        save_state(default_state())
    print(json.dumps({
        "saved": str(CONFIG_PATH),
        "accountSeq": cfg.accountSeq,
        "accountType": "BROKERAGE",
        "liveEnabled": cfg.liveEnabled,
    }, ensure_ascii=False, indent=2))


def status(client: TossLiveClient, cfg: LiveConfig, state: dict[str, Any]) -> None:
    accounts = client.accounts()
    selected = next((a for a in accounts if int(a.get("accountSeq", -1)) == cfg.accountSeq), None)
    hmap, overview = holdings_map(client, cfg)
    equity, cash, managed_value = managed_equity_usd(client, cfg, state, hmap)
    external = sorted([t for t in hmap if t not in state.get("positions", {})])
    summary = {
        "strategyVersion": STRATEGY_VERSION,
        "liveEnabledInConfig": cfg.liveEnabled,
        "killFilePresent": KILL_FILE.exists(),
        "accountSeq": cfg.accountSeq,
        "accountType": selected.get("accountType") if selected else None,
        "accountNoMasked": masked_account_no(selected.get("accountNo", "")) if selected else None,
        "usdCashBuyingPower": cash,
        "managedPositionMarketValueUsd": managed_value,
        "managedEquityUsd": equity,
        "managedPositions": len(state.get("positions", {})),
        "plannedOpenRiskUsd": planned_open_risk_usd(state),
        "plannedGrossUsd": planned_gross_usd(state),
        "pendingSignals": len(state.get("pending", {})),
        "externalHoldingsBlockedTickers": external,
        "positions": state.get("positions", {}),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def _has_pending_entry_recovery(state: dict[str, Any]) -> bool:
    return any(bool(x.get("entrySubmissionStarted")) for x in state.get("pending", {}).values())


def run_once(client: TossLiveClient, cfg: LiveConfig, state: dict[str, Any], cli_live: bool) -> None:
    if _has_pending_entry_recovery(state):
        recover_pending_entries(client, cfg, state, cli_live)
    # Refresh latest completed daily information immediately before an open-cycle.
    scan(client, state)
    mark_trend_exits(state)
    # Conservative same-open order: new entries still compete BEFORE marked exits release capital.
    process_entries(client, cfg, state, cli_live)
    execute_trend_exits(client, cfg, state, cli_live)
    manage_tp1(client, cfg, state, cli_live)


def daemon(client: TossLiveClient, cfg: LiveConfig, state: dict[str, Any], cli_live: bool) -> None:
    require_live(cfg, cli_live)
    log_event("DAEMON_START", strategyVersion=STRATEGY_VERSION, accountSeq=cfg.accountSeq)
    # Broker-side state is repaired before any signal work. This runs regardless
    # of market hours so a BUY that crossed a previous process crash is not left
    # unprotected until the next session.
    reconcile(client, cfg, state)
    if _has_pending_entry_recovery(state):
        recover_pending_entries(client, cfg, state, True)
    scan(client, state)
    # Intentionally blank: force one more scan/trend check in the pre-open/open window.
    # This catches daily data that became available after daemon startup/close scan.
    last_scan_key = ""
    last_trend_key = ""
    while True:
        try:
            if _has_pending_entry_recovery(state):
                recover_pending_entries(client, cfg, state, True)
            now_ny = pd.Timestamp.now(tz=NY_TZ)
            today = now_ny.strftime("%Y-%m-%d")
            cal = client.us_calendar(today)
            reg = (cal.get("today") or {}).get("regularMarket")
            if reg:
                start = pd.Timestamp(reg["startTime"]).tz_convert(NY_TZ)
                end = pd.Timestamp(reg["endTime"]).tz_convert(NY_TZ)
                if start - pd.Timedelta(minutes=30) <= now_ny <= start + pd.Timedelta(minutes=cfg.entryWindowMinutes):
                    key = f"entry-{today}"
                    if key != last_scan_key:
                        scan(client, state)
                        mark_trend_exits(state)
                        last_scan_key = key
                    # Marking a trend-off does not release capacity. Entries are still
                    # evaluated first, then next-open trend exits are submitted.
                    process_entries(client, cfg, state, True)
                    execute_trend_exits(client, cfg, state, True)
                if start <= now_ny <= end:
                    manage_tp1(client, cfg, state, True)
                if end + pd.Timedelta(minutes=5) <= now_ny <= end + pd.Timedelta(minutes=30):
                    key = f"close-{today}"
                    if key != last_trend_key:
                        mark_trend_exits(state); scan(client, state); last_trend_key = key
            time.sleep(max(1.0, cfg.pollSeconds))
        except KeyboardInterrupt:
            log_event("DAEMON_STOP_KEYBOARD")
            return
        except Exception as exc:
            log_event("DAEMON_LOOP_ERROR", error=repr(exc))
            time.sleep(5.0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["setup", "status", "scan", "run-once", "daemon", "arm", "disarm"], required=True)
    ap.add_argument("--account-seq", type=int)
    ap.add_argument("--arm-live", action="store_true", help="setup: save liveEnabled=true")
    ap.add_argument("--live", action="store_true", help="required for any real order submission")
    args = ap.parse_args()

    client = TossLiveClient()
    if args.mode == "setup":
        setup(client, args.account_seq, args.arm_live)
        return
    cfg = load_config()
    state = load_state()
    if args.mode == "arm":
        cfg.liveEnabled = True; atomic_json(CONFIG_PATH, cfg.__dict__); print("LIVE_ENABLED=true"); return
    if args.mode == "disarm":
        cfg.liveEnabled = False; atomic_json(CONFIG_PATH, cfg.__dict__); print("LIVE_ENABLED=false"); return
    if args.mode == "status":
        status(client, cfg, state); return
    if args.mode == "scan":
        scan(client, state); return
    if args.mode == "run-once":
        run_once(client, cfg, state, args.live); return
    daemon(client, cfg, state, args.live)


if __name__ == "__main__":
    main()
