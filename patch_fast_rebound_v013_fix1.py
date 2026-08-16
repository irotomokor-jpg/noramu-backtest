#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
P = ROOT / "fast_rebound_v013_final_readonly_broker_rehearsal.py"

if not P.exists():
    raise SystemExit(f"MISSING={P}")

src = P.read_text(encoding="utf-8")

# 1) Toss Order History GET requires status=OPEN or status=CLOSED.
old_call = 'orders_response = active.api(token, "GET", "/api/v1/orders", account=account)'
new_call = 'orders_response = active.api(token, "GET", "/api/v1/orders?status=OPEN", account=account)'
if old_call not in src and new_call not in src:
    raise SystemExit("BLOCK_ORDER_QUERY_SNIPPET_NOT_FOUND")
src = src.replace(old_call, new_call)

# 2) OPEN response item states from current Toss OpenAPI.
old_states = 'pending_states = {"OPEN", "PENDING", "WAITING", "WORKING", "NEW", "RECEIVED", "PARTIALLY_FILLED", "PARTIAL_FILLED", "PARTIAL"}'
new_states = 'pending_states = {"PENDING", "PARTIAL_FILLED", "PENDING_CANCEL", "PENDING_REPLACE"}'
if old_states not in src and new_states not in src:
    raise SystemExit("BLOCK_PENDING_STATE_SNIPPET_NOT_FOUND")
src = src.replace(old_states, new_states)

# 3) Prefer the bot's existing shared token cache so this readonly rehearsal does not
# unnecessarily mint a new token and invalidate the token used by the active watcher.
helper_anchor = 'def oauth_token(env: dict) -> tuple[str, dict]:\n'
helper_code = '''def _find_token_value(obj):
    if isinstance(obj, dict):
        for key in ["access_token", "accessToken"]:
            v = obj.get(key)
            if isinstance(v, str) and len(v) > 20:
                return v
        for v in obj.values():
            got = _find_token_value(v)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _find_token_value(v)
            if got:
                return got
    return None


def shared_cached_token(active):
    candidates = []
    for name, value in vars(active).items():
        nu = str(name).upper()
        if not any(x in nu for x in ["TOKEN", "AUTH", "OAUTH", "CACHE"]):
            continue
        if isinstance(value, Path):
            candidates.append(value)
        elif isinstance(value, str) and (value.endswith(".json") or "/" in value):
            try:
                candidates.append(Path(value).expanduser())
            except Exception:
                pass
    for base in [LIVE, ENV_FILE.parent, ROOT]:
        if not base.exists():
            continue
        for pattern in ["*token*.json", "*auth*.json", "*oauth*.json"]:
            candidates.extend(base.glob(pattern))
    seen = set()
    existing = []
    for p in candidates:
        try:
            p = p.expanduser().resolve()
        except Exception:
            continue
        if p in seen or not p.is_file():
            continue
        seen.add(p)
        existing.append(p)
    existing.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for p in existing:
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        token = _find_token_value(obj)
        if token:
            return token, {"source": "shared_token_cache", "cache_path": str(p), "token_persisted": False}
    return None, {"source": "shared_token_cache_not_found", "token_persisted": False}


def shared_or_oauth_token(env: dict, active):
    token, meta = shared_cached_token(active)
    if token:
        return token, meta
    token, oauth_meta = oauth_token(env)
    oauth_meta["source"] = "direct_oauth_fallback"
    return token, oauth_meta


'''
if 'def shared_or_oauth_token(env: dict, active):' not in src:
    if helper_anchor not in src:
        raise SystemExit("BLOCK_OAUTH_HELPER_ANCHOR_NOT_FOUND")
    src = src.replace(helper_anchor, helper_code + helper_anchor)

old_main_auth = '''    env = parse_env(ENV_FILE)
    token, auth_meta = oauth_token(env)
    account, account_source = resolve_account(env)

    active = load_module("v013_active_readonly", ACTIVE)
    core = load_module("v013_v010_core", V010_CORE)
'''
new_main_auth = '''    env = parse_env(ENV_FILE)
    active = load_module("v013_active_readonly", ACTIVE)
    token, auth_meta = shared_or_oauth_token(env, active)
    account, account_source = resolve_account(env)
    core = load_module("v013_v010_core", V010_CORE)
'''
if old_main_auth in src:
    src = src.replace(old_main_auth, new_main_auth)
elif new_main_auth not in src:
    raise SystemExit("BLOCK_MAIN_AUTH_SNIPPET_NOT_FOUND")

old_holdings = '    broker_holdings = active.holdings_map(token, account)\n'
new_holdings = '''    try:
        broker_holdings = active.holdings_map(token, account)
    except RuntimeError as e:
        msg = str(e).lower()
        if auth_meta.get("source") == "shared_token_cache" and ("401" in msg or "unauthorized" in msg or "invalid-token" in msg or "token" in msg):
            token, refresh_meta = oauth_token(env)
            refresh_meta["source"] = "direct_oauth_after_cached_token_rejected"
            auth_meta = refresh_meta
            broker_holdings = active.holdings_map(token, account)
        else:
            raise
'''
if old_holdings in src:
    src = src.replace(old_holdings, new_holdings)
elif 'direct_oauth_after_cached_token_rejected' not in src:
    raise SystemExit("BLOCK_HOLDINGS_AUTH_RETRY_SNIPPET_NOT_FOUND")

anchor = 'checks.append(check("BROKER_ORDER_STATUS_CLASSIFICATION_COMPLETE", order_summary["classification_complete"], order_summary))'
insert = 'checks.append(check("BROKER_OPEN_ORDER_QUERY_HAS_REQUIRED_STATUS", any(x.get("method") == "GET" and "/api/v1/orders?status=OPEN" in x.get("path", "") for x in network_methods), network_methods))\n    ' + anchor
if 'BROKER_OPEN_ORDER_QUERY_HAS_REQUIRED_STATUS' not in src:
    if anchor not in src:
        raise SystemExit("BLOCK_ORDER_CHECK_ANCHOR_NOT_FOUND")
    src = src.replace(anchor, insert)

P.write_text(src, encoding="utf-8")
compile(src, str(P), "exec")
print("FAST_REBOUND_V013_FIX1=PASS")
print("FIX=GET_/api/v1/orders_requires_status_OPEN")
print("FIX=OPEN_lifecycle_states_PEND_PARTIAL_CANCEL_REPLACE")
print("FIX=prefer_shared_token_cache_before_OAuth_fallback")
print("COMPILE=PASS")
print("ORDER_WRITES=OFF")
