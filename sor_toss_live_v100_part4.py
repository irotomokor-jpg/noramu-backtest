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

    # Expire missed entry dates so a daemon outage does not create stale entries later.
    stale_ids = [
        sid for sid, x in state.get("pending", {}).items()
        if str(x.get("entryDate") or "") and str(x.get("entryDate")) < today
    ]
    for sid in stale_ids:
        x = state["pending"].pop(sid, {})
        if sid not in state["processedSignals"]:
            state["processedSignals"].append(sid)
        log_event("ENTRY_EXPIRED_MISSED_OPEN", signalId=sid, ticker=x.get("ticker"), entryDate=x.get("entryDate"))
    if stale_ids:
        save_state(state)

    pending = [x for x in state.get("pending", {}).values() if str(x.get("entryDate")) == today]
    pending.sort(key=lambda x: (-float(x["priorityBreakoutVol"]), float(x["priorityAtrRatio"]), float(x["priorityVolRatio"]), str(x["ticker"])))
    if not pending:
        return
    symbols = sorted(set([str(x["ticker"]).upper() for x in pending] + [str(x).upper() for x in state.get("positions", {})]))
    prices = client.prices(symbols)
    hmap = reconcile(client, cfg, state)
    open_order_symbols = {str(o.get("symbol") or "").upper() for o in client.open_orders(cfg.accountSeq)}
    equity, cash, _ = managed_equity_usd(client, cfg, state, hmap)
    if equity <= 0:
        log_event("ENTRY_BLOCK_NO_MANAGED_EQUITY", cashUsd=cash)
        return

    # KILL blocks new entries without consuming the signal; exits/reconcile remain live.
    if KILL_FILE.exists():
        log_event("ENTRY_BLOCK_KILL_FILE", pendingToday=len(pending))
        return

    # Important: entries are evaluated before any trend-off next-open exits,
    # preserving V010/V014 conservative same-open capacity convention.
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
            state["processedSignals"].append(sid)
            state["pending"].pop(sid, None)
            save_state(state)
            continue

        stop_frac = (px - float(sig["initialStop"])) / px
        desired_notional = min(equity, (cfg.riskPerTrade * equity) / stop_frac)
        open_risk = planned_open_risk_usd(state)
        desired_risk = desired_notional * stop_frac
        if open_risk + desired_risk > cfg.maxOpenRisk * equity + 1e-9:
            log_event("ENTRY_SKIP", signalId=sid, ticker=ticker, reason="open_risk_limit")
            state["processedSignals"].append(sid); state["pending"].pop(sid, None); save_state(state)
            continue
        current_gross = planned_gross_usd(state)
        allowed_notional = min(desired_notional, max(0.0, cfg.maxGrossExposure * equity - current_gross), cash)
        qty = int(math.floor(allowed_notional / px))
        qty -= qty % 2  # exact 50% partial with whole shares
        if qty < 2:
            log_event("ENTRY_SKIP", signalId=sid, ticker=ticker, reason="size_below_even_2", desiredNotional=allowed_notional, price=px)
            state["processedSignals"].append(sid); state["pending"].pop(sid, None); save_state(state)
            continue

        require_live(cfg, cli_live)
        stamp = today.replace("-", "")
        cid = client_order_id("B", ticker, stamp)
        order = create_market_order(client, cfg, ticker, "BUY", qty, cid)
        oid = str(order.get("orderId") or "")
        if not oid:
            raise RuntimeError(f"BUY {ticker} returned no orderId")
        detail = wait_order_fill(client, cfg, oid)
        status = str(detail.get("status") or "")
        # Never leave an unknown/open BUY remainder hanging while arming a stop.
        if status not in {"FILLED", "REJECTED", "CANCELED"}:
            try:
                client.cancel_order(cfg.accountSeq, oid)
                time.sleep(0.8)
                detail = client.order_detail(cfg.accountSeq, oid)
                status = str(detail.get("status") or status)
                log_event("ENTRY_REMAINDER_CANCEL", ticker=ticker, orderId=oid, status=status)
            except TossLiveError as exc:
                if exc.code != "already-filled":
                    log_event("ENTRY_CANCEL_ERROR", ticker=ticker, orderId=oid, code=exc.code)
                detail = client.order_detail(cfg.accountSeq, oid)
                status = str(detail.get("status") or status)
        ex = detail.get("execution") or {}
        filled_qty = float(ex.get("filledQuantity") or 0.0)
        avg = float(ex.get("averageFilledPrice") or 0.0)
        managed_qty = int(math.floor(filled_qty + 1e-9))
        if managed_qty < 1 or avg <= 0:
            log_event("ENTRY_NOT_FILLED", ticker=ticker, orderId=oid, status=status, filledQuantity=filled_qty)
            state["processedSignals"].append(sid); state["pending"].pop(sid, None); save_state(state)
            continue
        if managed_qty != qty:
            log_event("ENTRY_PARTIAL_FILLED_MANAGE_ALL", ticker=ticker, requested=qty, filled=managed_qty, status=status)
        target = avg + RR_TARGET * (avg - float(sig["initialStop"]))
        # Put the live holding in state BEFORE stop creation so any failure path still tracks it.
        state["positions"][ticker] = {
            "signalId": sid, "signalDate": sig["signalDate"], "entryDate": today,
            "entryOrderId": oid, "entryPrice": avg, "quantity": managed_qty,
            "initialQuantity": managed_qty, "initialStop": float(sig["initialStop"]),
            "entryNotionalUsd": managed_qty * avg,
            "plannedRiskUsd": managed_qty * max(0.0, avg - float(sig["initialStop"])),
            "activeStop": float(sig["initialStop"]), "target": target, "tp1Hit": False,
            "protectiveConditionalId": "", "protectiveQuantity": 0,
            "protectiveExpireDate": None, "exitNextOpen": False,
        }
        save_state(state)
        try:
            stop_id = place_protective_stop(client, cfg, ticker, managed_qty, float(sig["initialStop"]), stamp)
        except Exception as exc:
            log_event("PROTECTIVE_STOP_FAILED", ticker=ticker, error=repr(exc), quantity=managed_qty)
            # Fail closed: flatten immediately. If anything remains, track it and retry protection.
            try:
                eorder = create_market_order(client, cfg, ticker, "SELL", managed_qty, client_order_id("E", ticker, stamp))
                eoid = str(eorder.get("orderId") or "")
                edetail = wait_order_fill(client, cfg, eoid) if eoid else {}
                log_event("EMERGENCY_EXIT_SUBMITTED", ticker=ticker, orderId=eoid, status=edetail.get("status"))
            except Exception as eexc:
                log_event("EMERGENCY_EXIT_ERROR", ticker=ticker, error=repr(eexc))
            h2, _ = holdings_map(client, cfg)
            residual = int(math.floor(broker_qty(h2, ticker) + 1e-9))
            if residual <= 0:
                state["positions"].pop(ticker, None)
            else:
                state["positions"][ticker]["quantity"] = residual
                state["positions"][ticker]["initialQuantity"] = residual
                try:
                    sid2 = place_protective_stop(client, cfg, ticker, residual, float(sig["initialStop"]), stamp + "r")
                    state["positions"][ticker]["protectiveConditionalId"] = sid2
                    state["positions"][ticker]["protectiveQuantity"] = residual
                    state["positions"][ticker]["protectiveExpireDate"] = stop_expire_date()
                    log_event("EMERGENCY_RESIDUAL_PROTECTED", ticker=ticker, quantity=residual, protectiveConditionalId=sid2)
                except Exception as pexc:
                    KILL_FILE.touch(exist_ok=True)
                    log_event("CRITICAL_UNPROTECTED_RESIDUAL", ticker=ticker, quantity=residual, error=repr(pexc), killFile=str(KILL_FILE))
            state["processedSignals"].append(sid); state["pending"].pop(sid, None); save_state(state)
            continue
        state["positions"][ticker]["protectiveConditionalId"] = stop_id
        state["positions"][ticker]["protectiveQuantity"] = managed_qty
        state["positions"][ticker]["protectiveExpireDate"] = stop_expire_date()
        state["processedSignals"].append(sid); state["pending"].pop(sid, None); save_state(state)
        log_event("ENTRY_FILLED_PROTECTED", ticker=ticker, orderId=oid, quantity=managed_qty, entryPrice=avg, stop=sig["initialStop"], target=target, protectiveConditionalId=stop_id)
        # Refresh cash/equity after each accepted entry.
        hmap, _ = holdings_map(client, cfg)
        equity, cash, _ = managed_equity_usd(client, cfg, state, hmap)

