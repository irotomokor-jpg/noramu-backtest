def execute_trend_exits(client: TossLiveClient, cfg: LiveConfig, state: dict[str, Any], cli_live: bool) -> None:
    now_ny = pd.Timestamp.now(tz=NY_TZ)
    today = now_ny.strftime("%Y-%m-%d")
    cal = client.us_calendar(today)
    reg = (cal.get("today") or {}).get("regularMarket")
    if not reg:
        return
    start = pd.Timestamp(reg["startTime"]).tz_convert(NY_TZ)
    if not (start <= now_ny <= start + pd.Timedelta(minutes=cfg.entryWindowMinutes)):
        return
    for ticker in list(state.get("positions", {})):
        p = state["positions"].get(ticker)
        if not p or not bool(p.get("exitNextOpen")):
            continue
        require_live(cfg, cli_live)
        hmap = reconcile(client, cfg, state)
        if ticker not in state["positions"]:
            continue
        if client.open_orders(cfg.accountSeq, ticker):
            log_event("TREND_EXIT_DEFER_OPEN_ORDER", ticker=ticker)
            continue
        p = state["positions"][ticker]
        actual = int(math.floor(broker_qty(hmap, ticker) + 1e-9))
        if actual <= 0:
            continue
        # Keep the protective stop LIVE until the discretionary market sell is resolved.
        # If the daemon dies after submit, the remaining position is still protected.
        order = create_market_order(client, cfg, ticker, "SELL", actual, client_order_id("T", ticker, pd.Timestamp.now(tz=NY_TZ).strftime("%Y%m%d%H%M%S")))
        oid = str(order.get("orderId") or "")
        detail = wait_order_fill(client, cfg, oid) if oid else {}
        status = str(detail.get("status") or "")
        if status not in {"FILLED", "REJECTED", "CANCELED"} and oid:
            try:
                client.cancel_order(cfg.accountSeq, oid)
                time.sleep(0.5)
            except TossLiveError as exc:
                if exc.code != "already-filled":
                    log_event("TREND_EXIT_ORDER_CANCEL_ERROR", ticker=ticker, code=exc.code)
        h2, _ = holdings_map(client, cfg)
        residual = int(math.floor(broker_qty(h2, ticker) + 1e-9))
        log_event("TREND_EXIT", ticker=ticker, orderId=oid, status=status, requested=actual, residual=residual)
        p = state["positions"].get(ticker)
        if p is None:
            continue
        if residual <= 0:
            stop_id = str(p.get("protectiveConditionalId") or "")
            if stop_id:
                try:
                    client.cancel_conditional(cfg.accountSeq, stop_id)
                except TossLiveError as exc:
                    if exc.code != "conditional-order-not-found":
                        log_event("POST_EXIT_STOP_CANCEL_ERROR", ticker=ticker, code=exc.code)
            state["positions"].pop(ticker, None)
            save_state(state)
            continue
        # Reconcile resizes the still-live protective stop to the actual residual.
        p["quantity"] = residual
        save_state(state)
        reconcile(client, cfg, state)

def manage_tp1(client: TossLiveClient, cfg: LiveConfig, state: dict[str, Any], cli_live: bool) -> None:
    if not state.get("positions"):
        return
    hmap = reconcile(client, cfg, state)
    tickers = list(state.get("positions", {}))
    if not tickers:
        return
    prices = client.prices(tickers)
    for ticker in list(tickers):
        p = state["positions"].get(ticker)
        if not p or bool(p.get("tp1Hit")) or bool(p.get("exitNextOpen")):
            continue
        px = float(prices.get(ticker) or 0.0)
        if px < float(p["target"]):
            continue
        require_live(cfg, cli_live)
        if client.open_orders(cfg.accountSeq, ticker):
            log_event("TP1_DEFER_OPEN_ORDER", ticker=ticker)
            continue
        hmap = reconcile(client, cfg, state)
        if ticker not in state["positions"]:
            continue
        p = state["positions"][ticker]
        actual = int(math.floor(broker_qty(hmap, ticker) + 1e-9))
        initial = int(p.get("initialQuantity") or actual)
        desired_tp_total = max(1, int(math.floor(initial * PARTIAL)))
        already_sold = max(0, initial - actual)
        to_sell = max(0, desired_tp_total - already_sold)
        if to_sell <= 0:
            remain_after = actual
        else:
            remain_after = actual - to_sell
        if actual < 1 or remain_after < 1:
            continue
        stop_id = str(p.get("protectiveConditionalId") or "")
        if not stop_id:
            KILL_FILE.touch(exist_ok=True)
            log_event("TP1_BLOCK_NO_PROTECTIVE_STOP", ticker=ticker, quantity=actual)
            continue
        # Reduce stop quantity only to the expected post-TP remainder, retaining initial stop.
        try:
            new_stop_id, new_expire = safe_modify_protective(
                client, cfg, ticker, stop_id, remain_after, float(p["initialStop"])
            )
            p["protectiveConditionalId"] = new_stop_id
            p["protectiveQuantity"] = remain_after
            p["protectiveExpireDate"] = new_expire
            save_state(state)
        except Exception as exc:
            log_event("TP1_BLOCK_STOP_MODIFY_FAILED", ticker=ticker, error=repr(exc))
            continue
        if to_sell > 0:
            try:
                order = create_market_order(
                    client, cfg, ticker, "SELL", to_sell,
                    client_order_id("P", ticker, pd.Timestamp.now(tz=NY_TZ).strftime("%Y%m%d%H%M%S")),
                )
            except Exception as exc:
                # Stop was intentionally reduced before the TP sell.  If the sell
                # cannot even be submitted, restore protection to ALL actual shares now.
                try:
                    restored_id, restored_expire = safe_modify_protective(
                        client, cfg, ticker, str(p["protectiveConditionalId"]),
                        actual, float(p["initialStop"])
                    )
                    p["protectiveConditionalId"] = restored_id
                    p["protectiveQuantity"] = actual
                    p["protectiveExpireDate"] = restored_expire
                    save_state(state)
                except Exception as rex:
                    KILL_FILE.touch(exist_ok=True)
                    log_event(
                        "CRITICAL_TP1_SUBMIT_FAILED_UNDERPROTECTED", ticker=ticker,
                        submitError=repr(exc), restoreError=repr(rex), killFile=str(KILL_FILE),
                    )
                else:
                    log_event("TP1_SUBMIT_FAILED_REPROTECTED", ticker=ticker, error=repr(exc), quantity=actual)
                continue
            oid = str(order.get("orderId") or "")
            detail = wait_order_fill(client, cfg, oid) if oid else {}
            status = str(detail.get("status") or "")
            if status not in {"FILLED", "REJECTED", "CANCELED"} and oid:
                try:
                    client.cancel_order(cfg.accountSeq, oid)
                except TossLiveError as exc:
                    if exc.code != "already-filled":
                        log_event("TP1_ORDER_CANCEL_ERROR", ticker=ticker, code=exc.code)
            h2, _ = holdings_map(client, cfg)
            actual_after = int(math.floor(broker_qty(h2, ticker) + 1e-9))
            if actual_after > remain_after:
                # TP sell was partial/failed. Restore initial-stop protection to ALL actual shares.
                try:
                    restored_id, restored_expire = safe_modify_protective(
                        client, cfg, ticker, str(p["protectiveConditionalId"]),
                        actual_after, float(p["initialStop"])
                    )
                    p["protectiveConditionalId"] = restored_id
                    p["protectiveQuantity"] = actual_after
                    p["protectiveExpireDate"] = restored_expire
                    p["quantity"] = actual_after
                    save_state(state)
                    log_event("TP1_PARTIAL_REPROTECTED", ticker=ticker, desiredSell=to_sell, actualRemaining=actual_after, status=status)
                except Exception as exc:
                    KILL_FILE.touch(exist_ok=True)
                    log_event("CRITICAL_TP1_PARTIAL_UNDERPROTECTED", ticker=ticker, actualRemaining=actual_after, error=repr(exc))
                continue
            actual = actual_after
            if actual <= 0:
                state["positions"].pop(ticker, None); save_state(state)
                continue
        # Desired 50% cumulative sale is complete; move all remaining protection to breakeven.
        try:
            be_id, be_expire = safe_modify_protective(
                client, cfg, ticker, str(p["protectiveConditionalId"]),
                actual, float(p["entryPrice"])
            )
            p["protectiveConditionalId"] = be_id
            p["protectiveQuantity"] = actual
            p["protectiveExpireDate"] = be_expire
            p["quantity"] = actual
            p["activeStop"] = float(p["entryPrice"])
            p["tp1Hit"] = True
            save_state(state)
            log_event("TP1_FILLED_BE_ARMED", ticker=ticker, cumulativeSold=initial-actual, remaining=actual, entry=p["entryPrice"], newStopId=p["protectiveConditionalId"])
        except Exception as exc:
            log_event("BE_STOP_MODIFY_FAILED", ticker=ticker, error=repr(exc), remaining=actual)
            # Initial protective stop is still active; keep tp1Hit false so BE arm is retried.

