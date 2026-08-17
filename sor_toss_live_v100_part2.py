@dataclass
class LiveConfig:
    accountSeq: int
    liveEnabled: bool = False
    riskPerTrade: float = DEFAULT_RISK_PER_TRADE
    maxPositions: int = DEFAULT_MAX_POSITIONS
    maxOpenRisk: float = DEFAULT_MAX_OPEN_RISK
    maxGrossExposure: float = DEFAULT_MAX_GROSS
    entryWindowMinutes: int = ENTRY_WINDOW_MINUTES
    pollSeconds: float = POLL_SECONDS
    blockExistingHoldings: bool = True


def atomic_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def validate_frozen_config(cfg: LiveConfig) -> None:
    expected = {
        "riskPerTrade": DEFAULT_RISK_PER_TRADE,
        "maxPositions": DEFAULT_MAX_POSITIONS,
        "maxOpenRisk": DEFAULT_MAX_OPEN_RISK,
        "maxGrossExposure": DEFAULT_MAX_GROSS,
        "entryWindowMinutes": ENTRY_WINDOW_MINUTES,
    }
    actual = {k: getattr(cfg, k) for k in expected}
    bad = {k: (actual[k], v) for k, v in expected.items() if actual[k] != v}
    if bad:
        raise RuntimeError(f"SOR_V1.0_FROZEN config drift blocked: {bad}")
    if not cfg.blockExistingHoldings:
        raise RuntimeError("SOR_V1.0_FROZEN requires blockExistingHoldings=true")


def load_config() -> LiveConfig:
    if not CONFIG_PATH.exists():
        raise RuntimeError("live config missing; run: python sor_toss_live_v100.py --mode setup")
    obj = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg = LiveConfig(**obj)
    validate_frozen_config(cfg)
    return cfg


def default_state() -> dict[str, Any]:
    return {
        "strategyVersion": STRATEGY_VERSION,
        "pending": {},
        "positions": {},
        "processedSignals": [],
        "lastScanAt": None,
        "lastCompletedSignalDate": None,
    }


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return default_state()
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if state.get("strategyVersion") != STRATEGY_VERSION:
            raise RuntimeError("live state strategy version mismatch")
        return state
    except Exception as exc:
        raise RuntimeError(f"cannot load live state: {exc}")


def save_state(state: dict[str, Any]) -> None:
    atomic_json(STATE_PATH, state)


def log_event(event: str, **data: Any) -> None:
    row = {"ts": pd.Timestamp.now(tz=KST_TZ).isoformat(), "event": event, **data}
    with EVENT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    print(json.dumps(row, ensure_ascii=False, default=str), flush=True)


def masked_account_no(s: str) -> str:
    s = str(s)
    return ("*" * max(0, len(s) - 4)) + s[-4:]


def setup(client: TossLiveClient, account_seq: int | None, arm_live: bool) -> None:
    accounts = client.accounts()
    if not accounts:
        raise RuntimeError("No Toss BROKERAGE account returned by API")
    print("AVAILABLE ACCOUNTS")
    for a in accounts:
        print({"accountSeq": a.get("accountSeq"), "accountNo": masked_account_no(a.get("accountNo", "")), "accountType": a.get("accountType")})
    if account_seq is None:
        if len(accounts) == 1:
            account_seq = int(accounts[0]["accountSeq"])
        else:
            account_seq = int(input("Select accountSeq: ").strip())
    valid = {int(a["accountSeq"]) for a in accounts}
    if int(account_seq) not in valid:
        raise RuntimeError(f"accountSeq {account_seq} not in API account list")
    cfg = LiveConfig(accountSeq=int(account_seq), liveEnabled=bool(arm_live))
    validate_frozen_config(cfg)
    atomic_json(CONFIG_PATH, cfg.__dict__)
    if not STATE_PATH.exists():
        save_state(default_state())
    print(json.dumps({"saved": str(CONFIG_PATH), "accountSeq": cfg.accountSeq, "liveEnabled": cfg.liveEnabled}, ensure_ascii=False, indent=2))


def require_live(cfg: LiveConfig, cli_live: bool) -> None:
    if not cli_live:
        raise RuntimeError("REAL ORDER BLOCKED: add --live")
    if not cfg.liveEnabled:
        raise RuntimeError("REAL ORDER BLOCKED: config liveEnabled=false; rerun setup with --arm-live")


def completed_daily_df(ticker: str) -> pd.DataFrame:
    df = load_data(None, ticker, v10.DOWNLOAD_START, None)
    if df.empty:
        return df
    now_ny = pd.Timestamp.now(tz=NY_TZ)
    last = pd.Timestamp(df.index[-1])
    # If a provider exposes today's still-forming daily candle, exclude it until 16:05 ET.
    if last.date() == now_ny.date() and (now_ny.hour < 16 or (now_ny.hour == 16 and now_ny.minute < 5)):
        df = df.iloc[:-1].copy()
    return df


def setup_signal(ticker: str) -> dict[str, Any] | None:
    original = v4.ATR_RATIO_MAX
    v4.ATR_RATIO_MAX = ATR_RATIO_MAX
    try:
        df = v4.add_sor_setup(completed_daily_df(ticker))
    finally:
        v4.ATR_RATIO_MAX = original
    if len(df) < 250:
        return None
    i = len(df) - 1
    row = df.iloc[i]
    if not bool(row.get("entry_signal", False)):
        return None
    stop, pivot_i = v4.recent_confirmed_pivot_low(df, i)
    source = "pivot"
    if stop is None or not np.isfinite(stop):
        left = max(0, i - v4.FALLBACK_STOP_LOOKBACK + 1)
        stop = float(df["Low"].iloc[left:i+1].min())
        pivot_i = None
        source = "fallback20"
    signal_date = pd.Timestamp(df.index[i]).strftime("%Y-%m-%d")
    signal_id = f"{ticker}-{signal_date}"
    return {
        "signalId": signal_id,
        "ticker": ticker,
        "signalDate": signal_date,
        "signalClose": float(row["Close"]),
        "initialStop": float(stop),
        "stopSource": source,
        "pivotDate": pd.Timestamp(df.index[pivot_i]).strftime("%Y-%m-%d") if pivot_i is not None else None,
        "atr20": float(row["ATR20"]),
        "priorityBreakoutVol": float(row["Volume"] / row["VOL50"]),
        "priorityAtrRatio": float(row["atr_ratio_setup"]),
        "priorityVolRatio": float(row["vol_ratio_setup"]),
    }


def next_business_day(client: TossLiveClient, signal_date: str) -> str:
    cal = client.us_calendar(signal_date)
    nxt = cal.get("nextBusinessDay") or {}
    if not nxt.get("date"):
        raise RuntimeError(f"US calendar has no nextBusinessDay for {signal_date}")
    return str(nxt["date"])


def scan(client: TossLiveClient, state: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    processed = set(state.get("processedSignals") or [])
    for i, ticker in enumerate(UNIVERSE, 1):
        try:
            sig = setup_signal(str(ticker))
            if sig and sig["signalId"] not in processed and sig["signalId"] not in state["pending"]:
                sig["entryDate"] = next_business_day(client, sig["signalDate"])
                state["pending"][sig["signalId"]] = sig
                found.append(sig)
        except Exception as exc:
            log_event("SCAN_ERROR", ticker=str(ticker), error=repr(exc))
        if i % 20 == 0:
            print(f"LIVE_SCAN {i}/{len(UNIVERSE)}", flush=True)
    state["lastScanAt"] = pd.Timestamp.now(tz=KST_TZ).isoformat()
    save_state(state)
    found.sort(key=lambda x: (-x["priorityBreakoutVol"], x["priorityAtrRatio"], x["priorityVolRatio"], x["ticker"]))
    print(json.dumps({"newSignals": len(found), "signals": found}, ensure_ascii=False, indent=2, default=str))
    return found


def holdings_map(client: TossLiveClient, cfg: LiveConfig) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    overview = client.holdings(cfg.accountSeq)
    items = overview.get("items") or []
    return {str(x.get("symbol", "")).upper(): x for x in items if x.get("symbol")}, overview


def managed_equity_usd(client: TossLiveClient, cfg: LiveConfig, state: dict[str, Any], hmap: dict[str, dict[str, Any]]) -> tuple[float, float, float]:
    cash = client.buying_power_usd(cfg.accountSeq)
    managed_value = 0.0
    for ticker in state.get("positions", {}):
        h = hmap.get(ticker.upper())
        if h:
            try:
                managed_value += float((h.get("marketValue") or {}).get("amount") or 0.0)
            except Exception:
                pass
    return cash + managed_value, cash, managed_value


def planned_open_risk_usd(state: dict[str, Any]) -> float:
    # Match V010/V014 conservative portfolio accounting: the ORIGINAL planned
    # risk remains occupied until the final exit, even after TP1/BE.
    total = 0.0
    for p in state.get("positions", {}).values():
        try:
            if p.get("plannedRiskUsd") is not None:
                total += float(p["plannedRiskUsd"])
            else:
                qty = float(p.get("initialQuantity") or p.get("quantity") or 0.0)
                total += qty * max(0.0, float(p["entryPrice"]) - float(p["initialStop"]))
        except Exception:
            pass
    return total


def planned_gross_usd(state: dict[str, Any]) -> float:
    # Same V010/V014 convention: original entry notional occupies gross capacity
    # until the final exit; TP1 does not free a slot/notional early.
    total = 0.0
    for p in state.get("positions", {}).values():
        try:
            if p.get("entryNotionalUsd") is not None:
                total += float(p["entryNotionalUsd"])
            else:
                qty = float(p.get("initialQuantity") or p.get("quantity") or 0.0)
                total += qty * float(p.get("entryPrice") or 0.0)
        except Exception:
            pass
    return total


def broker_qty(hmap: dict[str, dict[str, Any]], ticker: str) -> float:
    h = hmap.get(ticker.upper())
    return float(h.get("quantity") or 0.0) if h else 0.0


def wait_order_fill(client: TossLiveClient, cfg: LiveConfig, order_id: str, max_wait: float = 30.0) -> dict[str, Any]:
    end = time.monotonic() + max_wait
    last: dict[str, Any] = {}
    while time.monotonic() < end:
        last = client.order_detail(cfg.accountSeq, order_id)
        status = str(last.get("status") or "")
        if status in {"FILLED", "REJECTED", "CANCELED"}:
            return last
        time.sleep(0.7)
    return last


def client_order_id(kind: str, ticker: str, stamp: str) -> str:
    raw = f"sor100-{kind}-{ticker}-{stamp}"
    return raw[:36].replace(".", "-")


def create_market_order(client: TossLiveClient, cfg: LiveConfig, ticker: str, side: str, quantity: float, cid: str) -> dict[str, Any]:
    if quantity <= 0:
        raise ValueError("quantity <= 0")
    q = str(int(quantity)) if float(quantity).is_integer() else f"{quantity:.6f}".rstrip("0").rstrip(".")
    payload = {
        "clientOrderId": cid,
        "symbol": ticker,
        "side": side,
        "orderType": "MARKET",
        "timeInForce": "DAY",
        "quantity": q,
        # Intentionally never auto-confirm >= KRW100m equivalent orders.
        "confirmHighValueOrder": False,
    }
    return client.create_order(cfg.accountSeq, payload)


def valid_us_trigger(price: float) -> str:
    if price <= 0 or not np.isfinite(price):
        raise ValueError("invalid trigger price")
    tick = 0.0001 if price < 1 else 0.01
    # Long protective stop rounds DOWN, never above the intended stop.
    px = math.floor((price + 1e-12) / tick) * tick
    decimals = 4 if px < 1 else 2
    return f"{px:.{decimals}f"
