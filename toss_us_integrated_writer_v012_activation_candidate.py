#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIVE = ROOT / "live" / "US_FROZEN_V1"
ACTIVE_ENGINE = ROOT / "toss_us_live_open_v001.py"
V010_CORE = ROOT / "toss_us_integrated_writer_v010_candidate.py"
RSI_PROVIDER = ROOT / "rsi_live_shadow_parity_v001.py"
FAST_PROVIDER = ROOT / "fast_rebound_koru_v1_shadow_runtime.py"
V009_MANIFEST = LIVE / "v009_pre_live_candidate_manifest.json"
V011_FIX1_REPORT = ROOT / "fast_rebound_v011_fix1_order_write_audit" / "FINAL_V011_FIX1_ORDER_WRITE_AUDIT.json"
STATUS = LIVE / "integrated_writer_v012_status.json"
ACTIVATION_MANIFEST = LIVE / "integrated_writer_v012_activation_manifest.json"

ORDER_WRITES_ENABLED = False
LIVE_APPROVAL = False


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


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"IMPORT_SPEC_FAIL:{path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_active():
    return load_module("v012_active_engine", ACTIVE_ENGINE)


def load_v010_core():
    return load_module("v012_v010_core", V010_CORE)


def load_rsi_provider():
    return load_module("v012_rsi_provider", RSI_PROVIDER)


def load_fast_provider():
    return load_module("v012_fast_provider", FAST_PROVIDER)


def assert_preconditions():
    required = [ACTIVE_ENGINE, V010_CORE, RSI_PROVIDER, FAST_PROVIDER, V009_MANIFEST, V011_FIX1_REPORT]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise RuntimeError(f"V012_REQUIRED_FILE_MISSING:{missing}")
    fx = jread(V011_FIX1_REPORT)
    if fx.get("pass") is not True:
        raise RuntimeError("V011_FIX1_NOT_PASS")
    if int(fx.get("post_order_contexts", 0)) != 1:
        raise RuntimeError("V011_FIX1_POST_CONTEXT_NOT_EXACT_ONE")
    if int(fx.get("ambiguous_order_contexts", 99)) != 0:
        raise RuntimeError("V011_FIX1_AMBIGUOUS_POST_CONTEXT")


def read_holdings(token, account):
    active = load_active()
    return active.holdings_map(token, account)


def read_buying_power(token, account):
    active = load_active()
    return active.buying_power_usd(token, account)


def read_sellable(token, account, symbol):
    active = load_active()
    return active.sellable_qty(token, account, symbol)


def reconcile_pending(token, account, ledger):
    active = load_active()
    return active.reconcile_pending(token, account, ledger)


def submit_order_exact(token, account, body):
    # Exact production write path proven by V011 FIX1, intentionally unreachable until explicit activation.
    if not ORDER_WRITES_ENABLED or not LIVE_APPROVAL:
        raise RuntimeError("ORDER_WRITES_DISABLED_V012")
    active = load_active()
    result = active.api(
        token,
        "POST",
        "/api/v1/orders",
        account=account,
        body=body,
    )["result"]
    return result


def stable_client_order_id(strategy: str, symbol: str, side: str, signal_key: str) -> str:
    core = load_v010_core()
    return core.stable_client_order_id(strategy, symbol, side, signal_key)


def fee_safe_amount(budget) -> Decimal:
    core = load_v010_core()
    return core.fee_safe_amount(Decimal(str(budget)))


def max_safe_sell(book, owner: str, broker_total, broker_sellable):
    core = load_v010_core()
    return core.max_safe_sell(book, owner, Decimal(str(broker_total)), Decimal(str(broker_sellable)))


def signal_bindings():
    rsi = load_rsi_provider()
    fast = load_fast_provider()
    rsi_required = ["ensure_engine", "load_pair_day", "live_entry", "latest_common_date"]
    fast_required = ["validate_rule", "signal_mask", "process_entry", "process_exit"]
    return {
        "rsi": {"module": RSI_PROVIDER.name, "functions": rsi_required, "complete": all(callable(getattr(rsi, x, None)) for x in rsi_required)},
        "fast": {"module": FAST_PROVIDER.name, "functions": fast_required, "complete": all(callable(getattr(fast, x, None)) for x in fast_required)},
    }


def build_activation_manifest():
    assert_preconditions()
    m9 = jread(V009_MANIFEST)
    bindings = signal_bindings()
    obj = {
        "version": "US_MULTI_STRATEGY_V012_EXPLICIT_ACTIVATION_CANDIDATE",
        "mode": "DRY_RUN_ORDER_WRITES_OFF",
        "order_writes_enabled": False,
        "live_approval": False,
        "active_engine_sha256": sha256_file(ACTIVE_ENGINE),
        "v010_core_sha256": sha256_file(V010_CORE),
        "rsi_provider_sha256": sha256_file(RSI_PROVIDER),
        "fast_provider_sha256": sha256_file(FAST_PROVIDER),
        "v009_manifest_sha256": sha256_file(V009_MANIFEST),
        "current_live_cap_usd": m9.get("current_live_cap_usd"),
        "rsi_single_trade_cap_usd": m9.get("projected_rsi_single_trade_cap_usd"),
        "fast_single_trade_cap_usd": m9.get("projected_fast_single_trade_cap_usd"),
        "priority": m9.get("priority"),
        "ownership": m9.get("ownership"),
        "broker_adapter": {
            "holdings": "active.holdings_map(token,account)",
            "buying_power": "active.buying_power_usd(token,account)",
            "sellable": "active.sellable_qty(token,account,symbol)",
            "reconcile": "active.reconcile_pending(token,account,ledger)",
            "order_write": "active.api(token,'POST','/api/v1/orders',account=account,body=body)['result']",
            "order_write_guard": "ORDER_WRITES_ENABLED and LIVE_APPROVAL",
        },
        "signal_bindings": bindings,
        "activation_requirements": [
            "V012 audit pass with zero failed checks",
            "active engine hash unchanged",
            "bot ledger unchanged",
            "order writes remain disabled during rehearsal",
            "explicit separate activation step required",
        ],
    }
    atomic_json(ACTIVATION_MANIFEST, obj)
    return obj


def main():
    obj = build_activation_manifest()
    status = {
        "version": obj["version"],
        "activation_candidate_built": True,
        "signal_provider_binding_complete": bool(obj["signal_bindings"]["rsi"]["complete"] and obj["signal_bindings"]["fast"]["complete"]),
        "broker_read_adapter_complete": True,
        "broker_reconcile_adapter_complete": True,
        "broker_write_adapter_code_present": True,
        "exact_post_path": "POST /api/v1/orders",
        "order_writes_enabled": False,
        "live_approval": False,
        "live_ready": False,
        "next": "RUN_V012_ACTIVATION_AUDIT_THEN_EXPLICIT_ACTIVATION_STEP",
    }
    atomic_json(STATUS, status)
    print("US_MULTI_STRATEGY_V012_EXPLICIT_ACTIVATION_CANDIDATE")
    for k, v in status.items():
        if k != "version":
            print(f"{k.upper()}={v}")
    print(f"ACTIVATION_MANIFEST={ACTIVATION_MANIFEST}")


if __name__ == "__main__":
    main()
