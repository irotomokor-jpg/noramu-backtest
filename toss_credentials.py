#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persist Toss Open API credentials in a local plaintext JSON file.

The credential file is intentionally local-only and must remain gitignored.
Environment variables are still supported and take precedence in TossReplayClient.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path

CREDENTIAL_FILE = Path(__file__).resolve().parent / "toss_credentials.local.json"
CLIENT_ID_KEY = "TOSS_CLIENT_ID"
CLIENT_SECRET_KEY = "TOSS_CLIENT_SECRET"


def load_saved_toss_credentials() -> tuple[str, str]:
    """Return (client_id, client_secret) from the local JSON file, or empty strings."""
    if not CREDENTIAL_FILE.exists():
        return "", ""
    try:
        obj = json.loads(CREDENTIAL_FILE.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            return "", ""
        client_id = str(obj.get(CLIENT_ID_KEY) or obj.get("client_id") or "").strip()
        client_secret = str(obj.get(CLIENT_SECRET_KEY) or obj.get("client_secret") or "").strip()
        return client_id, client_secret
    except Exception:
        return "", ""


def save_toss_credentials(client_id: str, client_secret: str) -> None:
    client_id = client_id.strip()
    client_secret = client_secret.strip()
    if not client_id or not client_secret:
        raise ValueError("client_id and client_secret must both be non-empty")
    payload = {
        CLIENT_ID_KEY: client_id,
        CLIENT_SECRET_KEY: client_secret,
    }
    CREDENTIAL_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def delete_toss_credentials() -> None:
    try:
        CREDENTIAL_FILE.unlink()
    except FileNotFoundError:
        pass


def status() -> int:
    env_id = bool(os.getenv(CLIENT_ID_KEY, "").strip())
    env_secret = bool(os.getenv(CLIENT_SECRET_KEY, "").strip())
    saved_id, saved_secret = load_saved_toss_credentials()
    print(f"ENV_CLIENT_ID={'YES' if env_id else 'NO'}")
    print(f"ENV_CLIENT_SECRET={'YES' if env_secret else 'NO'}")
    print(f"FILE={CREDENTIAL_FILE}")
    print(f"FILE_CLIENT_ID={'YES' if saved_id else 'NO'}")
    print(f"FILE_CLIENT_SECRET={'YES' if saved_secret else 'NO'}")
    if (env_id and env_secret) or (saved_id and saved_secret):
        print("TOSS_CREDENTIALS=READY")
        return 0
    print("TOSS_CREDENTIALS=MISSING")
    return 1


def setup_from_prompt() -> None:
    current_id, _ = load_saved_toss_credentials()
    suffix = f" [{current_id}]" if current_id else ""
    client_id = input(f"Toss Client ID{suffix}: ").strip() or current_id
    client_secret = getpass.getpass("Toss Client Secret (hidden while typing): ").strip()
    if not client_id:
        raise ValueError("Client ID is required")
    if not client_secret:
        raise ValueError("Client Secret is required")
    save_toss_credentials(client_id, client_secret)
    print(f"Saved plaintext Toss credentials to: {CREDENTIAL_FILE}")
    print("This local file is gitignored. Do not upload or share it.")


def migrate_env() -> None:
    client_id = os.getenv(CLIENT_ID_KEY, "").strip()
    client_secret = os.getenv(CLIENT_SECRET_KEY, "").strip()
    if not client_id or not client_secret:
        raise ValueError("TOSS_CLIENT_ID / TOSS_CLIENT_SECRET are not both set in this process")
    save_toss_credentials(client_id, client_secret)
    print(f"Saved current Toss environment credentials to: {CREDENTIAL_FILE}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Manage local Toss Open API credentials")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="Prompt once and save to toss_credentials.local.json")
    sub.add_parser("status", help="Show whether credentials are available (never prints values)")
    sub.add_parser("delete", help="Delete toss_credentials.local.json")
    sub.add_parser("migrate-env", help="Save current TOSS_CLIENT_* env vars to the local JSON file")
    args = ap.parse_args()

    if args.command == "setup":
        setup_from_prompt()
    elif args.command == "status":
        raise SystemExit(status())
    elif args.command == "delete":
        delete_toss_credentials()
        print(f"Deleted {CREDENTIAL_FILE}, if present.")
    elif args.command == "migrate-env":
        migrate_env()


if __name__ == "__main__":
    main()
