def _complete_pending_signal(state: dict[str, Any], sid: str) -> None:
    if sid not in state["processedSignals"]:
        state["processedSignals"].append(sid)
    state["pending"].pop(sid, None)
    save_state(state)


def _position_from_fill(sig: dict[str, Any], ticker: str, sid: str, entry_date: str,
                        order_id: str, quantity: int, avg: float) -> dict[str, Any]:
    stop = float(sig["initialStop"])
    target = avg + RR_TARGET * (avg - stop)
    return {
        "signalId": sid, "signalDate": sig["signalDate"], "entryDate": entry_date,
        "entryOrderId": order_id, "entryPrice": avg, "quantity": int(quantity),
        "initialQuantity": int(quantity), "initialStop": stop,
        "entryNotionalUsd": int(quantity) * avg,
        "plannedRiskUsd": int(quantity) * max(0.0, avg - stop),
        "activeStop": stop, "target": target, "tp1Hit": False,
        "protectiveConditionalId": "", "protectiveQuantity": 0,
        "protectiveExpireDate": None, "exitNextOpen": False,
    }


def _recover_or_create_protection(client: TossLiveClient, cfg: LiveConfig, state: dict[str, Any],
                                  ticker: str, quantity: int, stop: float, stamp: str) -> str:
    rows = client.open_conditional_orders(cfg.accountSeq, ticker)
    exact = _matching_protective_rows(rows, ticker, quantity, stop, None)
    if len(exact) == 1:
        cid = str(exact[0].get("conditionalOrderId") or "")
        if cid:
            return cid
    if len(exact) > 1:
        raise RuntimeError(f"multiple exact protective stops while recovering {ticker}")
    other = [r for r in rows if str(r.get("symbol") or "").upper() == ticker.upper()]
    if other:
        raise RuntimeError(f"unknown conditional order conflicts while recovering {ticker}")
    return place_protective_stop(client, cfg, ticker, quantity, stop, stamp)


def _emergency_flatten_recovery(client: TossLiveClient, cfg: LiveConfig, state: dict[str, Any],
                                sid: str, sig: dict[str, Any], ticker: str,
                                quantity: int, reason: str) -> None:
    """Fail closed when a recovered fill cannot be safely reconstructed."""
    KILL_FILE.touch(exist_ok=True)
    log_event("ENTRY_RECOVERY_EMERGENCY_FLATTEN", signalId=sid, ticker=ticker,
              quantity=quantity, reason=reason, killFile=str(KILL_FILE))
    try:
        order = create_market_order(
            client, cfg, ticker, "SELL", quantity,
            client_order_id("ER", ticker, pd.Timestamp.now(tz=NY_TZ).strftime("%m%d%H%M%S")),
        )
        oid = str(order.get("orderId") or "")
        if oid:
            wait_order_fill(client, cfg, oid)
        time.sleep(0.5)
    except Exception as exc:
        log_event("ENTRY_RECOVERY_EMERGENCY_EXIT_ERROR", ticker=ticker, error=repr(exc))
    h2, _ = holdings_map(client, cfg)
    residual = int(math.floor(broker_qty(h2, ticker) + 1e-9))
    if residual <= 0:
        state["positions"].pop(ticker, None)
        _complete_pending_signal(state, sid)
        return
    stop = float(sig["initialStop"])
    ref = float(sig.get("entryReferencePrice") or 0.0)
    safe_ref = ref if ref > stop else stop * 1.0001
    state["positions"][ticker] = _position_from_fill(
        sig, ticker, sid, str(sig.get("entryDate") or pd.Timestamp.now(tz=NY_TZ).date()),
        str(sig.get("entryOrderId") or "RECOVERED"), residual, safe_ref,
    )
    try:
        sid2 = _recover_or_create_protection(
            client, cfg, state, ticker, residual, stop,
            "rr" + pd.Timestamp.now(tz=NY_TZ).strftime("%m%d%H%M%S"),
        )
        state["positions"][ticker]["protectiveConditionalId"] = sid2
        state["positions"][ticker]["protectiveQuantity"] = residual
        state["positions"][ticker]["protectiveExpireDate"] = stop_expire_date()
        _complete_pending_signal(state, sid)
        log_event("ENTRY_RECOVERY_RESIDUAL_PROTECTED", ticker=ticker,
                  quantity=residual, protectiveConditionalId=sid2)
    except Exception as exc:
        save_state(state)
        log_event("CRITICAL_ENTRY_RECOVERY_RESIDUAL_UNPROTECTED", ticker=ticker,
                  quantity=residual, error=repr(exc), killFile=str(KILL_FILE))


def recover_pending_entries(client: TossLiveClient, cfg: LiveConfig, state: dict[str, Any], cli_live: bool) -> None:
    """Crash recovery for the narrow window around BUY submission/fill.

    A submission marker is persisted before POST /orders. On restart, any still-open
    bot BUY is canceled first, then the actual holding is adopted and protected.
    """
    marked = [
        (sid, x) for sid, x in list(state.get("pending", {}).items())
        if bool(x.get("entrySubmissionStarted"))
    ]
    if not marked:
        return
    require_live(cfg, cli_live)
    hmap = reconcile(client, cfg, state)

    for sid, sig in marked:
        if sid not in state.get("pending", {}):
            continue
        ticker = str(sig["ticker"]).upper()
        if ticker in state.get("positions", {}):
            _complete_pending_signal(state, sid)
            log_event("ENTRY_RECOVERY_STATE_ALREADY_PRESENT", signalId=sid, ticker=ticker)
            continue

        pre_qty = int(math.floor(float(sig.get("preEntryQuantity") or 0.0) + 1e-9))
        if pre_qty != 0:
            KILL_FILE.touch(exist_ok=True)
            log_event("CRITICAL_ENTRY_RECOVERY_NONZERO_BASELINE", signalId=sid, ticker=ticker,
                      preEntryQuantity=pre_qty, killFile=str(KILL_FILE))
            continue

        expected_oid = str(sig.get("entryOrderId") or "")
        try:
            open_buys = [
                o for o in client.open_orders(cfg.accountSeq, ticker)
                if str(o.get("side") or "").upper() == "BUY"
            ]
        except Exception as exc:
            KILL_FILE.touch(exist_ok=True)
            log_event("CRITICAL_ENTRY_RECOVERY_ORDER_READ_FAILED", ticker=ticker, error=repr(exc))
            continue

        if expected_oid:
            unexpected = [o for o in open_buys if str(o.get("orderId") or "") != expected_oid]
            if unexpected:
                KILL_FILE.touch(exist_ok=True)
                log_event("CRITICAL_ENTRY_RECOVERY_UNEXPECTED_BUY", signalId=sid, ticker=ticker,
                          expectedOrderId=expected_oid, unexpectedCount=len(unexpected),
                          killFile=str(KILL_FILE))
                continue
            candidates = [o for o in open_buys if str(o.get("orderId") or "") == expected_oid]
        else:
            candidates = open_buys
        if len(candidates) > 1:
            KILL_FILE.touch(exist_ok=True)
            log_event("CRITICAL_ENTRY_RECOVERY_MULTIPLE_BUYS", signalId=sid, ticker=ticker,
                      count=len(candidates), killFile=str(KILL_FILE))
            continue
        if len(candidates) == 1:
            oid = str(candidates[0].get("orderId") or "")
            try:
                client.cancel_order(cfg.accountSeq, oid)
                log_event("ENTRY_RECOVERY_CANCEL_OPEN_BUY", signalId=sid, ticker=ticker, orderId=oid)
            except TossLiveError as exc:
                if exc.code not in {"already-filled", "already-canceled"}:
                    log_event("ENTRY_RECOVERY_CANCEL_ERROR", signalId=sid, ticker=ticker,
                              orderId=oid, code=exc.code)
            time.sleep(0.8)

        hmap, _ = holdings_map(client, cfg)
        h = hmap.get(ticker)
        total_qty = int(math.floor(broker_qty(hmap, ticker) + 1e-9))
        managed_qty = total_qty - pre_qty
        if managed_qty <= 0:
            _complete_pending_signal(state, sid)
            log_event("ENTRY_RECOVERY_NO_FILL", signalId=sid, ticker=ticker,
                      clientOrderId=sig.get("entryClientOrderId"), entryOrderId=sig.get("entryOrderId"))
            continue

        try:
            avg = float((h or {}).get("averagePurchasePrice") or 0.0)
        except Exception:
            avg = 0.0
        stop = float(sig["initialStop"])
        if avg <= stop:
            _emergency_flatten_recovery(
                client, cfg, state, sid, sig, ticker, managed_qty,
                "missing_or_invalid_average_purchase_price" if avg <= 0 else "recovered_entry_at_or_below_stop",
            )
            continue

        entry_date = str(sig.get("entryDate") or pd.Timestamp.now(tz=NY_TZ).strftime("%Y-%m-%d"))
        state["positions"][ticker] = _position_from_fill(
            sig, ticker, sid, entry_date, str(sig.get("entryOrderId") or "RECOVERED"), managed_qty, avg
        )
        save_state(state)
        try:
            stamp = "rec" + pd.Timestamp.now(tz=NY_TZ).strftime("%m%d%H%M%S")
            stop_id = _recover_or_create_protection(client, cfg, state, ticker, managed_qty, stop, stamp)
            p = state["positions"][ticker]
            p["protectiveConditionalId"] = stop_id
            p["protectiveQuantity"] = managed_qty
            p["protectiveExpireDate"] = stop_expire_date()
            _complete_pending_signal(state, sid)
            log_event("ENTRY_RECOVERED_PROTECTED", signalId=sid, ticker=ticker,
                      quantity=managed_qty, entryPrice=avg, stop=stop, protectiveConditionalId=stop_id)
        except Exception as exc:
            log_event("CRITICAL_ENTRY_RECOVERY_PROTECTION_FAILED", signalId=sid, ticker=ticker,
                      quantity=managed_qty, error=repr(exc), killFile=str(KILL_FILE))
            _emergency_flatten_recovery(client, cfg, state, sid, sig, ticker, managed_qty,
                                        "protective_stop_recovery_failed")


def process_entries(client: TossLiveClient, cfg: LiveConfig, state: dict[str, Any], cli_live: bool) -> None:
    now_ny = pd.Timestamp.now(tz=NY_TZ)
    today = now_ny.strftime("%Y-%m-%d")
    cal = client.us_calendar(today)
    today_row = cal.get("today") or {}
    reg = today_row.get("regularMarket")
    if not reg:
        return
    start = pd.Timestamp(reg["startTime"]).tz_convert(NY_TZ)
    end_entry = start + pd.Timedelta(minutes=cfg.entryWindowMinutes)
    if not (start <= now_ny <= end_entry):
        return

    recover_pending_entries(client, cfg, state, cli_live)

    stale_ids = [
        sid for sid, x in state.get("pending", {}).items()
        if str(x.get("entryDate") or "") and str(x.get("entryDate")) < today
        and not bool(x.get("entrySubmissionStarted"))
    ]
    for sid in stale_ids:
        x = state["pending"].pop(sid, {})
        if sid not in state["processedSignals"]:
            state["processedSignals"].append(sid)
        log_event("ENTRY_EXPIRED_MISSED_OPEN", signalId=sid, ticker=x.get("ticker"), entryDate=x.get("entryDate"))
    if stale_ids:
        save_state(state)

    pending = [
        x for x in state.get("pending", {}).values()
        if str(x.get("entryDate")) == today and not bool(x.get("entrySubmissionStarted"))
    ]
    pending.sort(key=lambda x: (-float(x["priorityBreakoutVol"]), float(x["priorityAtrRatio"]), float(x["priorityVolRatio"]), str(x["ticker"])))
    if not pending:
        return
    symbols = sorted(set([str(x["ticker"]).upper() for x in pending] + [str(x).upper() for x in state.get("positions", {})]))
    prices = client.prices(symbols)
    hmap = reconcile(client, cfg, state)
    open_order_symbols = {str(o.get("symbol") or "").upper() for o in client.open_orders(cfg.accountSeq)}
    try:
        open_conditional_symbols = {
            str(o.get("symbol") or "").upper()
            for o in client.open_conditional_orders(cfg.accountSeq)
            if o.get("symbol")
        }
    except Exception as exc:
        KILL_FILE.touch(exist_ok=True)
        log_event("ENTRY_BLOCK_CONDITIONAL_READ_FAILED", error=repr(exc), killFile=str(KILL_FILE))
        return
    equity, cash, _ = managed_equity_usd(client, cfg, state, hmap)
    if equity <= 0:
        log_event("ENTRY_BLOCK_NO_MANAGED_EQUITY", cashUsd=cash)
        return

    if KILL_FILE.exists():
        log_event("ENTRY_BLOCK_KILL_FILE", pendingToday=len(pending))
        return

    for sig in pending:
        sid = str(sig["signalId"]); ticker = str(sig["ticker"]).upper()
        if sid not in state["pending"]:
            continue
        reason = None
        px = float(prices.get(ticker) or 0.0)
        if ticker in state["positions"]:
            reason = "already_bot_active"
        elif cfg.blockExistingHoldings and broker_qty(hmap, ticker) > 0:
            reason = "preexisting_holding"
        elif ticker in open_order_symbols:
            reason = "open_order_exists"
        elif ticker in open_conditional_symbols:
            reason = "preexisting_conditional_order"
        elif len(state["positions"]) >= cfg.maxPositions:
            reason = "position_limit"
        elif px <= 0:
            reason = "no_price"
        elif px <= float(sig["initialStop"]):
            reason = "price_below_stop"
        elif max(0.0, px - float(sig["signalClose"])) > MAX_ENTRY_GAP_ATR * float(sig["atr20"]):
            reason = "gap_limit"
        if reason:
            log_event("ENTRY_SKIP", signalId=sid, ticker=ticker, reason=reason, price=px)
            _complete_pending_signal(state, sid)
            continue

        stop_frac = (px - float(sig["initialStop"])) / px
        desired_notional = min(equity, (cfg.riskPerTrade * equity) / stop_frac)
        open_risk = planned_open_risk_usd(state)
        desired_risk = desired_notional * stop_frac
        if open_risk + desired_risk > cfg.maxOpenRisk * equity + 1e-9:
            log_event("ENTRY_SKIP", signalId=sid, ticker=ticker, reason="open_risk_limit")
            _complete_pending_signal(state, sid)
            continue
        current_gross = planned_gross_usd(state)
        allowed_notional = min(desired_notional, max(0.0, cfg.maxGrossExposure * equity - current_gross), cash)
        qty = int(math.floor(allowed_notional / px))
        qty -= qty % 2
        if qty < 2:
            log_event("ENTRY_SKIP", signalId=sid, ticker=ticker, reason="size_below_even_2", desiredNotional=allowed_notional, price=px)
            _complete_pending_signal(state, sid)
            continue

        require_live(cfg, cli_live)
        stamp = today.replace("-", "")
        cid = client_order_id("B", ticker, stamp)
        state["pending"][sid].update({
            "entrySubmissionStarted": True,
            "entryClientOrderId": cid,
            "entrySubmissionStartedAt": pd.Timestamp.now(tz=KST_TZ).isoformat(),
            "preEntryQuantity": int(math.floor(broker_qty(hmap, ticker) + 1e-9)),
            "requestedQuantity": qty,
            "entryReferencePrice": px,
        })
        save_state(state)
        try:
            order = create_market_order(client, cfg, ticker, "BUY", qty, cid)
        except Exception as exc:
            log_event("ENTRY_SUBMIT_AMBIGUOUS_OR_FAILED", signalId=sid, ticker=ticker,
                      clientOrderId=cid, error=repr(exc))
            try:
                recover_pending_entries(client, cfg, state, cli_live)
            except Exception as rex:
                KILL_FILE.touch(exist_ok=True)
                log_event("CRITICAL_ENTRY_IMMEDIATE_RECOVERY_FAILED", ticker=ticker,
                          error=repr(rex), killFile=str(KILL_FILE))
            continue
        oid = str(order.get("orderId") or "")
        if not oid:
            KILL_FILE.touch(exist_ok=True)
            log_event("CRITICAL_ENTRY_RESPONSE_NO_ORDER_ID", signalId=sid, ticker=ticker,
                      clientOrderId=cid, killFile=str(KILL_FILE))
            continue
        state["pending"][sid]["entryOrderId"] = oid
        save_state(state)

        detail = wait_order_fill(client, cfg, oid)
        status = str(detail.get("status") or "")
        if status not in {"FILLED", "REJECTED", "CANCELED"}:
            try:
                client.cancel_order(cfg.accountSeq, oid)
                time.sleep(0.8)
                detail = client.order_detail(cfg.accountSeq, oid)
                status = str(detail.get("status") or status)
                log_event("ENTRY_REMAINDER_CANCEL", ticker=ticker, orderId=oid, status=status)
            except TossLiveError as exc:
                if exc.code not in {"already-filled", "already-canceled"}:
                    log_event("ENTRY_CANCEL_ERROR", ticker=ticker, orderId=oid, code=exc.code)
                detail = client.order_detail(cfg.accountSeq, oid)
                status = str(detail.get("status") or status)
        ex = detail.get("execution") or {}
        filled_qty = float(ex.get("filledQuantity") or 0.0)
        avg = float(ex.get("averageFilledPrice") or 0.0)
        managed_qty = int(math.floor(filled_qty + 1e-9))
        if managed_qty < 1 or avg <= 0:
            log_event("ENTRY_NOT_FILLED", ticker=ticker, orderId=oid, status=status, filledQuantity=filled_qty)
            # If the order API says no valid fill but broker holdings changed, the
            # durable marker remains and recovery owns the ambiguity.
            hcheck, _ = holdings_map(client, cfg)
            if int(math.floor(broker_qty(hcheck, ticker) + 1e-9)) > int(state["pending"][sid].get("preEntryQuantity") or 0):
                recover_pending_entries(client, cfg, state, cli_live)
            else:
                _complete_pending_signal(state, sid)
            continue
        if managed_qty != qty:
            log_event("ENTRY_PARTIAL_FILLED_MANAGE_ALL", ticker=ticker, requested=qty, filled=managed_qty, status=status)
        if avg <= float(sig["initialStop"]):
            _emergency_flatten_recovery(client, cfg, state, sid, sig, ticker, managed_qty,
                                        "live_fill_at_or_below_stop")
            continue
        target = avg + RR_TARGET * (avg - float(sig["initialStop"]))
        state["positions"][ticker] = _position_from_fill(sig, ticker, sid, today, oid, managed_qty, avg)
        save_state(state)
        try:
            stop_id = place_protective_stop(client, cfg, ticker, managed_qty, float(sig["initialStop"]), stamp)
        except Exception as exc:
            log_event("PROTECTIVE_STOP_FAILED", ticker=ticker, error=repr(exc), quantity=managed_qty)
            _emergency_flatten_recovery(client, cfg, state, sid, sig, ticker, managed_qty,
                                        "initial_protective_stop_failed")
            continue
        state["positions"][ticker]["protectiveConditionalId"] = stop_id
        state["positions"][ticker]["protectiveQuantity"] = managed_qty
        state["positions"][ticker]["protectiveExpireDate"] = stop_expire_date()
        _complete_pending_signal(state, sid)
        log_event("ENTRY_FILLED_PROTECTED", ticker=ticker, orderId=oid, quantity=managed_qty, entryPrice=avg,
                  stop=sig["initialStop"], target=target, protectiveConditionalId=stop_id)
        hmap, _ = holdings_map(client, cfg)
        equity, cash, _ = managed_equity_usd(client, cfg, state, hmap)

