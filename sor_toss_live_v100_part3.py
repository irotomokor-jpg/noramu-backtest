def reconcile(client: TossLiveClient, cfg: LiveConfig, state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Reconcile bot state to holdings AND broker-side protection.

    This is crash recovery. Every bot-managed residual must be either protected by
    its known SINGLE MARKET stop or already covered by an active normal SELL order.
    Unknown/paused/conflicting conditions block new entries via SOR_LIVE.KILL.
    """
    hmap, _ = holdings_map(client, cfg)
    changed = False
    try:
        conditional_rows = client.open_conditional_orders(cfg.accountSeq)
    except Exception as exc:
        KILL_FILE.touch(exist_ok=True)
        log_event("CRITICAL_CONDITIONAL_RECONCILE_READ_FAILED", error=repr(exc), killFile=str(KILL_FILE))
        return hmap
    try:
        normal_open = client.open_orders(cfg.accountSeq)
    except Exception as exc:
        KILL_FILE.touch(exist_ok=True)
        log_event("CRITICAL_OPEN_ORDER_RECONCILE_READ_FAILED", error=repr(exc), killFile=str(KILL_FILE))
        return hmap
    open_sell_symbols = {
        str(o.get("symbol") or "").upper()
        for o in normal_open
        if str(o.get("side") or "").upper() == "SELL"
    }
    by_id = {str(r.get("conditionalOrderId") or ""): r for r in conditional_rows if r.get("conditionalOrderId")}
    today = pd.Timestamp.now(tz=NY_TZ).date()

    for ticker in list(state.get("positions", {})):
        p = state["positions"][ticker]
        actual_f = broker_qty(hmap, ticker)
        actual = int(math.floor(actual_f + 1e-9)) if actual_f >= 1 else 0
        expected = int(float(p.get("quantity", 0)))
        stop_id = str(p.get("protectiveConditionalId") or "")

        if actual <= 0:
            if stop_id and stop_id in by_id:
                try:
                    client.cancel_conditional(cfg.accountSeq, stop_id)
                except TossLiveError as exc:
                    if exc.code != "conditional-order-not-found":
                        log_event("ORPHAN_STOP_CANCEL_ERROR", ticker=ticker, code=exc.code)
            log_event("POSITION_GONE_RECONCILE", ticker=ticker, expected=expected)
            state["positions"].pop(ticker, None)
            changed = True
            continue

        if actual != expected:
            log_event("POSITION_QTY_RECONCILE", ticker=ticker, expected=expected, actual=actual)
            p["quantity"] = actual
            changed = True

        active_stop = float(p.get("activeStop") or p.get("initialStop") or 0.0)
        desired_trigger = valid_us_trigger(active_stop)
        broker_row = by_id.get(stop_id) if stop_id else None

        if broker_row is not None:
            top_status = str(broker_row.get("status") or "")
            first_status = str((broker_row.get("first") or {}).get("status") or "")
            # A triggered conditional is no longer a watching stop; its generated
            # SELL order becomes the protection path. Do not create a duplicate.
            if top_status in {"ORDERING", "ORDERED"} or first_status in {"ORDERING", "ORDERED"}:
                log_event("PROTECTIVE_TRIGGER_IN_PROGRESS", ticker=ticker, stopId=stop_id,
                          conditionalStatus=top_status, legStatus=first_status,
                          sellOrderOpen=ticker in open_sell_symbols)
                continue
            if top_status == "PAUSED" or first_status == "PAUSED":
                KILL_FILE.touch(exist_ok=True)
                log_event("CRITICAL_PROTECTIVE_STOP_PAUSED", ticker=ticker, stopId=stop_id,
                          killFile=str(KILL_FILE))
                continue

        # If our stored id disappeared (crash after modify, manual edit, trigger, expiry),
        # adopt only a UNIQUE exact STOP/SINGLE/MARKET condition. Entry logic forbids
        # pre-existing conditionals on a ticker before the bot takes ownership.
        if broker_row is None:
            exact = _matching_protective_rows(conditional_rows, ticker, actual, active_stop, None)
            if len(exact) == 1:
                broker_row = exact[0]
                stop_id = str(broker_row.get("conditionalOrderId") or "")
                p["protectiveConditionalId"] = stop_id
                p["protectiveQuantity"] = actual
                p["protectiveExpireDate"] = broker_row.get("expireDate")
                changed = True
                log_event("PROTECTION_ID_RECOVERED", ticker=ticker, stopId=stop_id, quantity=actual, stop=active_stop)
            elif len(exact) > 1:
                KILL_FILE.touch(exist_ok=True)
                log_event("CRITICAL_MULTIPLE_MATCHING_STOPS", ticker=ticker, matches=len(exact), killFile=str(KILL_FILE))
                continue
            else:
                other_single = [r for r in conditional_rows if _is_protective_row(r, ticker)]
                if ticker in open_sell_symbols:
                    # A triggered stop/trend/TP sell is already working. Do not create
                    # another SELL path until the normal order resolves.
                    log_event("PROTECTION_ABSENT_BUT_SELL_ORDER_OPEN", ticker=ticker, quantity=actual)
                    continue
                if other_single:
                    KILL_FILE.touch(exist_ok=True)
                    log_event(
                        "CRITICAL_UNKNOWN_SINGLE_STOP_CONFLICT", ticker=ticker,
                        count=len(other_single), killFile=str(KILL_FILE),
                    )
                    continue
                try:
                    stamp = "rc" + pd.Timestamp.now(tz=NY_TZ).strftime("%m%d%H%M%S")
                    stop_id = place_protective_stop(client, cfg, ticker, actual, active_stop, stamp)
                    p["protectiveConditionalId"] = stop_id
                    p["protectiveQuantity"] = actual
                    p["protectiveExpireDate"] = stop_expire_date()
                    changed = True
                    log_event("PROTECTION_RECOVERED", ticker=ticker, quantity=actual, stop=active_stop, stopId=stop_id)
                except Exception as exc:
                    KILL_FILE.touch(exist_ok=True)
                    log_event("CRITICAL_PROTECTION_RECREATE_FAILED", ticker=ticker, actual=actual, error=repr(exc), killFile=str(KILL_FILE))
                continue

        # Stored/adopted stop exists in broker OPEN state. Broker fields, not local
        # protectiveQuantity, decide whether it is adequate.
        broker_qty_stop = _conditional_qty(broker_row)
        broker_trigger = _conditional_trigger(broker_row)
        expire_s = str(broker_row.get("expireDate") or "")
        expire_near = True
        try:
            expire_near = pd.Timestamp(expire_s).date() <= today + timedelta(days=STOP_REFRESH_DAYS)
        except Exception:
            expire_near = True
        needs_modify = broker_qty_stop != actual or broker_trigger != desired_trigger or expire_near
        if needs_modify:
            if ticker in open_sell_symbols:
                # Avoid resizing a stop while another normal SELL is actively changing qty.
                log_event(
                    "PROTECTION_RESIZE_DEFER_SELL_ORDER_OPEN", ticker=ticker,
                    actual=actual, brokerStopQty=broker_qty_stop, brokerTrigger=broker_trigger,
                )
                continue
            try:
                new_id, new_expire = safe_modify_protective(client, cfg, ticker, stop_id, actual, active_stop)
                p["protectiveConditionalId"] = new_id
                p["protectiveQuantity"] = actual
                p["protectiveExpireDate"] = new_expire
                changed = True
                log_event(
                    "PROTECTION_RECONCILED", ticker=ticker, fromQty=broker_qty_stop,
                    toQty=actual, fromTrigger=broker_trigger, toTrigger=desired_trigger,
                    stopId=new_id, expireDate=new_expire,
                )
            except Exception as exc:
                KILL_FILE.touch(exist_ok=True)
                log_event(
                    "CRITICAL_PROTECTION_RECONCILE_FAILED", ticker=ticker,
                    actual=actual, brokerStopQty=broker_qty_stop, error=repr(exc),
                    killFile=str(KILL_FILE),
                )
        else:
            if int(float(p.get("protectiveQuantity") or 0)) != actual or str(p.get("protectiveExpireDate") or "") != expire_s:
                p["protectiveQuantity"] = actual
                p["protectiveExpireDate"] = expire_s
                changed = True

    if changed:
        save_state(state)
    return hmap

