#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import sys
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIVE = ROOT / "live" / "US_FROZEN_V1"
OUT = ROOT / "fast_rebound_v013_final_readonly_broker_rehearsal"
ACTIVE = ROOT / "toss_us_live_open_v001.py"
BOT_LEDGER = LIVE / "bot_ledger.json"
V010_CORE = ROOT / "toss_us_integrated_writer_v010_candidate.py"
V010_LEDGER = LIVE / "integrated_writer_v010_ledger.json"
V012_AUDIT = ROOT / "fast_rebound_v012_activation_audit" / "FINAL_V012_ACTIVATION_AUDIT.json"
V012_MANIFEST = LIVE / "integrated_writer_v012_activation_manifest.json"
ENV_FILE = Path.home() / ".config" / "noramu" / "toss.env"
REPORT = OUT / "FINAL_V013_READONLY_BROKER_REHEARSAL.json"
SNAPSHOT = LIVE / "v013_readonly_broker_snapshot.json"

SYMS = ["TQQQ", "SOXL", "KORU", "UPRO"]
EPS = Decimal("0.000001")
ORDER_WRITES_ENABLED = False
LIVE_APPROVAL = False
BASE = "https://openapi.tossinvest.com"


def D(x) -> Decimal:
    if x is None or x == "":
        return Decimal("0")
    return Decimal(str(x))


def dec(x: Decimal) -> str:
    s = format(x, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def jread(path: Path, default=None):
    if not path.exists():
        return {} if default is None else default
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def check(name: str, ok: bool, detail=""):
    return {"name": name, "pass": bool(ok), "detail": str(detail)}


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"IMPORT_SPEC_FAIL:{path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def parse_env(path: Path) -> dict:
    out = dict(os.environ)
    if path.exists():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k:
                out[k] = v
    return out


def pick_env(env: dict, exact: list[str], contains: list[str]):
    for k in exact:
        if env.get(k):
            return env[k], k
    for k, v in env.items():
        ku = k.upper()
        if v and all(x in ku for x in contains):
            return v, k
    return None, None


def oauth_token(env: dict) -> tuple[str, dict]:
    cid, cid_key = pick_env(env, ["TOSS_CLIENT_ID", "TOSSINVEST_CLIENT_ID", "CLIENT_ID"], ["CLIENT", "ID"])
    sec, sec_key = pick_env(env, ["TOSS_CLIENT_SECRET", "TOSSINVEST_CLIENT_SECRET", "CLIENT_SECRET"], ["SECRET"])
    if not cid or not sec:
        raise RuntimeError(f"TOSS_CREDENTIAL_ENV_NOT_RESOLVED client_id_key={cid_key} secret_key={sec_key}")
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cid, "client_secret": sec}).encode("utf-8")
    req = urllib.request.Request(BASE + "/oauth2/token", data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    token = data.get("access_token") or data.get("accessToken") or data.get("result", {}).get("accessToken") or data.get("result", {}).get("access_token")
    if not token:
        raise RuntimeError(f"OAUTH_TOKEN_RESPONSE_NO_TOKEN keys={list(data.keys())}")
    return str(token), {"client_id_env_key": cid_key, "client_secret_env_key": sec_key, "token_persisted": False}


def resolve_account(env: dict) -> tuple[str, str]:
    val, key = pick_env(env, ["TOSS_ACCOUNT_SEQ", "TOSS_ACCOUNT", "ACCOUNT_SEQ", "TOSS_ACCOUNT_ID"], ["ACCOUNT"])
    if val:
        return str(val), f"env:{key}"
    return "1", "fallback:account_seq_1"


def get_qty_map(raw) -> dict[str, Decimal]:
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        try:
            out[str(k).upper()] = D(v)
        except Exception:
            continue
    return out


def order_items(result):
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ["orders", "items", "data", "content"]:
            if isinstance(result.get(key), list):
                return result[key]
    return []


def pending_order_summary(result):
    items = order_items(result)
    pending_states = {"OPEN", "PENDING", "WAITING", "WORKING", "NEW", "RECEIVED", "PARTIALLY_FILLED", "PARTIAL_FILLED", "PARTIAL"}
    terminal_states = {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "CLOSED", "DONE", "COMPLETED", "EXPIRED"}
    pending = []
    unknown = []
    for row in items:
        if not isinstance(row, dict):
            unknown.append({"type": type(row).__name__})
            continue
        state = None
        for key in ["status", "orderStatus", "state"]:
            if row.get(key) is not None:
                state = str(row.get(key)).upper()
                break
        slim = {k: row.get(k) for k in ["orderId", "clientOrderId", "symbol", "side", "status", "orderStatus", "state"] if k in row}
        if state in pending_states:
            pending.append(slim)
        elif state in terminal_states:
            pass
        else:
            unknown.append(slim or {"keys": sorted(row.keys())[:20]})
    complete = len(unknown) == 0
    return {"items_seen": len(items), "pending_count": len(pending), "pending": pending, "unknown_count": len(unknown), "unknown": unknown[:10], "classification_complete": complete}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    checks = []
    required = [ACTIVE, BOT_LEDGER, V010_CORE, V010_LEDGER, V012_AUDIT, V012_MANIFEST, ENV_FILE]
    for p in required:
        checks.append(check(f"FILE_EXISTS::{p.name}", p.exists(), p))
    if not all(p.exists() for p in required):
        atomic_json(REPORT, {"version": "V013", "pass": False, "checks": checks, "order_writes": False, "live_approval": False})
        raise SystemExit("V013_REQUIRED_FILE_MISSING")

    v12 = jread(V012_AUDIT)
    checks.append(check("V012_AUDIT_PASS", v12.get("activation_candidate_audit_pass") is True and int(v12.get("checks_failed", 99)) == 0, v12.get("checks_failed")))

    active_before = sha256_file(ACTIVE)
    bot_before = sha256_file(BOT_LEDGER)
    v010_before = sha256_file(V010_LEDGER)

    env = parse_env(ENV_FILE)
    token, auth_meta = oauth_token(env)
    account, account_source = resolve_account(env)

    active = load_module("v013_active_readonly", ACTIVE)
    core = load_module("v013_v010_core", V010_CORE)
    original_api = active.api
    network_methods = []

    def readonly_api(token_arg, method, path, *args, **kwargs):
        m = str(method).upper()
        network_methods.append({"method": m, "path": str(path)})
        if m != "GET":
            raise RuntimeError(f"V013_BLOCK_NON_GET_API method={m} path={path}")
        return original_api(token_arg, method, path, *args, **kwargs)

    active.api = readonly_api

    broker_holdings = active.holdings_map(token, account)
    buying_power = active.buying_power_usd(token, account)
    sellable = {s: active.sellable_qty(token, account, s) for s in SYMS}
    orders_response = active.api(token, "GET", "/api/v1/orders", account=account)
    orders_result = orders_response.get("result", orders_response) if isinstance(orders_response, dict) else orders_response
    order_summary = pending_order_summary(orders_result)

    holdings = get_qty_map(broker_holdings)
    bot = jread(BOT_LEDGER)
    integ = jread(V010_LEDGER)
    sleeves = bot.get("sleeves", {}) if isinstance(bot, dict) else {}
    ownership = {}
    ownership_ok = True
    sell_safety_ok = True
    for s in SYMS:
        broker_qty = D(holdings.get(s, 0))
        frozen_qty = D(sleeves.get(s, {}).get("bot_qty", 0))
        rsi_qty = D(integ.get("positions", {}).get("rsi", {}).get(s, {}).get("qty", 0))
        fast_qty = D(integ.get("positions", {}).get("fast", {}).get(s, {}).get("qty", 0))
        strategy_owned = frozen_qty + rsi_qty + fast_qty
        enough = broker_qty + EPS >= strategy_owned
        protected = max(Decimal("0"), broker_qty - strategy_owned) if enough else Decimal("0")
        if not enough:
            ownership_ok = False
        book = core.OwnershipBook(protected, frozen_qty, rsi_qty, fast_qty)
        broker_sellable = D(sellable.get(s, 0))
        safe = {}
        for owner in ["frozen", "rsi", "fast"]:
            q = core.max_safe_sell(book, owner, broker_qty, broker_sellable)
            safe[owner] = dec(q)
            if q > getattr(book, owner) + EPS:
                sell_safety_ok = False
        ownership[s] = {
            "broker_qty": dec(broker_qty),
            "protected_baseline_qty": dec(protected),
            "frozen_qty": dec(frozen_qty),
            "rsi_qty": dec(rsi_qty),
            "fast_qty": dec(fast_qty),
            "strategy_owned_qty": dec(strategy_owned),
            "broker_sellable_qty": dec(broker_sellable),
            "broker_covers_all_strategy_owned": enough,
            "max_safe_sell_qty": safe,
        }

    gross, parts = core.current_total_principal(bot, integ)
    hard_cap = D(parts.get("cap", 0))
    cap_ok = gross <= hard_cap + EPS
    local_bot_pending = len(bot.get("pending_orders", [])) if isinstance(bot, dict) else 999
    local_integrated_pending = len(integ.get("pending_orders", [])) if isinstance(integ, dict) else 999

    checks.append(check("NETWORK_ONLY_GET_AFTER_OAUTH", all(x["method"] == "GET" for x in network_methods), network_methods))
    checks.append(check("BROKER_HOLDINGS_READ", isinstance(broker_holdings, dict), type(broker_holdings).__name__))
    checks.append(check("BROKER_BUYING_POWER_READ", D(buying_power) >= 0, buying_power))
    checks.append(check("BROKER_SELLABLE_READ_ALL_SYMBOLS", len(sellable) == len(SYMS), sellable))
    checks.append(check("BROKER_COVERS_STRATEGY_OWNED_QTY", ownership_ok, ownership))
    checks.append(check("OWNER_MAX_SAFE_SELL_NEVER_EXCEEDS_OWNER_QTY", sell_safety_ok, ownership))
    checks.append(check("TOTAL_PRINCIPAL_HARD_CAP", cap_ok, f"gross={gross} cap={hard_cap}"))
    checks.append(check("LOCAL_FROZEN_PENDING_ZERO", local_bot_pending == 0, local_bot_pending))
    checks.append(check("LOCAL_INTEGRATED_PENDING_ZERO", local_integrated_pending == 0, local_integrated_pending))
    checks.append(check("BROKER_ORDER_STATUS_CLASSIFICATION_COMPLETE", order_summary["classification_complete"], order_summary))
    checks.append(check("BROKER_PENDING_ORDERS_ZERO", order_summary["pending_count"] == 0, order_summary))
    checks.append(check("ORDER_WRITES_CONSTANT_FALSE", ORDER_WRITES_ENABLED is False))
    checks.append(check("LIVE_APPROVAL_CONSTANT_FALSE", LIVE_APPROVAL is False))
    checks.append(check("ACTIVE_ENGINE_UNCHANGED", sha256_file(ACTIVE) == active_before, active_before))
    checks.append(check("BOT_LEDGER_UNCHANGED", sha256_file(BOT_LEDGER) == bot_before, bot_before))
    checks.append(check("V010_LEDGER_UNCHANGED", sha256_file(V010_LEDGER) == v010_before, v010_before))

    failed = [x for x in checks if not x["pass"]]
    rehearsal_pass = len(failed) == 0
    snapshot = {
        "version": "US_MULTI_STRATEGY_V013_READONLY_BROKER_SNAPSHOT",
        "rehearsal_pass": rehearsal_pass,
        "order_writes_enabled": False,
        "live_approval": False,
        "live_ready": False,
        "auth": auth_meta,
        "account_seq": account,
        "account_source": account_source,
        "buying_power_usd": dec(D(buying_power)),
        "ownership": ownership,
        "broker_order_summary": order_summary,
        "network_calls_after_oauth": network_methods,
        "principal": {k: dec(D(v)) for k, v in parts.items()},
        "gross_principal_usd": dec(gross),
        "hard_cap_usd": dec(hard_cap),
        "protected_policy": "BROKER_QTY_MINUS_FROZEN_MINUS_RSI_MINUS_FAST_AT_V013_SNAPSHOT; NEVER_SELL_BELOW_THIS_BASELINE",
        "token_persisted": False,
    }
    atomic_json(SNAPSHOT, snapshot)

    report = {
        "version": "FAST_REBOUND_V013_FINAL_READONLY_BROKER_REHEARSAL",
        "final_no_order_rehearsal_pass": rehearsal_pass,
        "checks_passed": len(checks) - len(failed),
        "checks_total": len(checks),
        "checks_failed": len(failed),
        "order_writes_enabled": False,
        "live_approval": False,
        "live_ready": False,
        "active_engine_unchanged": sha256_file(ACTIVE) == active_before,
        "bot_ledger_unchanged": sha256_file(BOT_LEDGER) == bot_before,
        "v010_ledger_unchanged": sha256_file(V010_LEDGER) == v010_before,
        "account_seq": account,
        "account_source": account_source,
        "buying_power_usd": dec(D(buying_power)),
        "broker_pending_orders": order_summary["pending_count"],
        "protected_baselines": {s: ownership[s]["protected_baseline_qty"] for s in SYMS},
        "ownership": ownership,
        "next": "V014_EXPLICIT_LIVE_ENABLE_DECISION_AND_ACTIVATION_PACKAGE" if rehearsal_pass else "PATCH_ONLY_FAILED_V013_CHECKS_DO_NOT_ENABLE_ORDERS",
        "checks": checks,
    }
    atomic_json(REPORT, report)

    print("FAST_REBOUND_V013_FINAL_READONLY_BROKER_REHEARSAL")
    print(f"CHECKS={report['checks_passed']}/{report['checks_total']}")
    print(f"FINAL_NO_ORDER_REHEARSAL_PASS={rehearsal_pass}")
    print("ORDER_WRITES=False")
    print("LIVE_APPROVAL=False")
    print("LIVE_READY=False")
    print(f"ACCOUNT_SEQ={account} SOURCE={account_source}")
    print(f"BUYING_POWER_USD={report['buying_power_usd']}")
    print(f"BROKER_PENDING_ORDERS={report['broker_pending_orders']}")
    print(f"ACTIVE_ENGINE_UNCHANGED={report['active_engine_unchanged']}")
    print(f"BOT_LEDGER_UNCHANGED={report['bot_ledger_unchanged']}")
    print(f"V010_LEDGER_UNCHANGED={report['v010_ledger_unchanged']}")
    print("===== OWNERSHIP SNAPSHOT =====")
    for s in SYMS:
        x = ownership[s]
        print(f"{s} broker={x['broker_qty']} protected={x['protected_baseline_qty']} frozen={x['frozen_qty']} rsi={x['rsi_qty']} fast={x['fast_qty']} sellable={x['broker_sellable_qty']} safe={x['max_safe_sell_qty']}")
    print("===== FAILED CHECKS =====")
    if failed:
        for x in failed:
            print(f"FAIL {x['name']} :: {x['detail']}")
    else:
        print("NONE")
    print(f"SNAPSHOT={SNAPSHOT}")
    print(f"REPORT={REPORT}")
    print(f"NEXT={report['next']}")


if __name__ == "__main__":
    main()
