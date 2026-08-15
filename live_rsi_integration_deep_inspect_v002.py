#!/usr/bin/env python3
from __future__ import annotations

import ast
import hashlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILES = [
    ROOT / "toss_us_live_open_v001.py",
    ROOT / "toss_us_live_intraday_signal_v001.py",
    ROOT / "toss_us_live_bootstrap_v001.py",
    ROOT / "sanggu_live_dashboard_v002.py",
    ROOT / "run_us_live_open_watcher.sh",
]
KEYS = [
    "reconcile", "pending", "order", "sellable", "buying", "commission",
    "applied_tax", "bot_qty", "cash_usd", "orderAmount", "client_order_id",
    "protected", "ledger", "execute", "plan", "BUY", "SELL",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def tracked(path: Path) -> bool:
    try:
        rel = str(path.relative_to(ROOT))
        r = subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:
        return False


def print_range(lines: list[str], a: int, b: int):
    a = max(1, a)
    b = min(len(lines), b)
    for i in range(a, b + 1):
        print(f"{i:05d}: {lines[i-1]}")


def inspect_python(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    print(f"\n===== FILE={path.name} bytes={path.stat().st_size} sha256={sha256(path)} tracked={tracked(path)} =====")
    try:
        tree = ast.parse(text)
    except Exception as e:
        print(f"AST_ERROR={type(e).__name__}:{e}")
        return

    funcs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            funcs.append((node.lineno, end, node.name))
    funcs.sort()
    print("FUNCTIONS=" + ", ".join(f"{n}@{a}-{b}" for a, b, n in funcs))

    selected = []
    for a, b, name in funcs:
        lname = name.lower()
        body = "\n".join(lines[a-1:b]).lower()
        if any(k.lower() in lname or k.lower() in body for k in KEYS):
            selected.append((a, b, name))

    for a, b, name in selected:
        print(f"\n--- FUNCTION {name} lines {a}-{b} ---")
        print_range(lines, max(1, a - 3), min(len(lines), b + 3))

    # Module-level keyword windows not covered by function extraction.
    hits = []
    for i, line in enumerate(lines, 1):
        if any(k.lower() in line.lower() for k in KEYS):
            hits.append(i)
    clusters = []
    for i in hits:
        if not clusters or i > clusters[-1][1] + 12:
            clusters.append([max(1, i - 8), min(len(lines), i + 12)])
        else:
            clusters[-1][1] = min(len(lines), max(clusters[-1][1], i + 12))
    print(f"\nMODULE_KEYWORD_CLUSTERS={len(clusters)}")
    for a, b in clusters:
        print(f"\n--- KEYWORD WINDOW {a}-{b} ---")
        print_range(lines, a, b)


def inspect_shell(path: Path):
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    print(f"\n===== FILE={path.name} bytes={path.stat().st_size} sha256={sha256(path)} tracked={tracked(path)} =====")
    print_range(lines, 1, len(lines))


def main():
    print("LIVE_RSI_INTEGRATION_DEEP_INSPECT_V002")
    try:
        s = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True, stderr=subprocess.STDOUT)
        print("\n===== GIT STATUS PORCELAIN =====")
        print(s.rstrip() or "CLEAN")
    except Exception as e:
        print(f"GIT_STATUS_ERROR={type(e).__name__}:{e}")

    for p in FILES:
        if not p.exists():
            print(f"\nMISSING={p.name}")
            continue
        if p.suffix == ".py":
            inspect_python(p)
        else:
            inspect_shell(p)

    print("\n===== DONE =====")


if __name__ == "__main__":
    main()
