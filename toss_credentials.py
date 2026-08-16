#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persist Toss Open API credentials in the OS credential store.

Windows: Python keyring uses the Windows credential backend.
Other platforms: behavior depends on the installed keyring backend.

Secrets are never written to this repository or printed to stdout.
Environment variables remain supported and take precedence in TossReplayClient.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from typing import Optional

SERVICE = "noramu-backtest-toss-openapi"
CLIENT_ID_KEY = "TOSS_CLIENT_ID"
CLIENT_SECRET_KEY = "TOSS_CLIENT_SECRET"


def _keyring_module():
    try:
        import keyring  # type: ignore
        return keyring
    except ImportError:
        return None


def load_saved_toss_credentials() -> tuple[str, str]:
    """Return (client_id, client_secret) from the OS keyring, or empty strings."""
    keyring = _keyring_module()
    if keyring is None:
        return "", ""
    try:
        client_id = keyring.get_password(SERVICE, CLIENT_ID_KEY) or ""
        client_secret = keyring.get_password(SERVICE, CLIENT_SECRET_KEY) or ""
        return client_id.strip(), client_secret.strip()
    except Exception:
        # Keep env-only/server workflows working even when no usable desktop
        # keyring backend exists (e.g. some headless Linux hosts).
        return "", ""


def save_toss_credentials(client_id: str, client_secret: str) -> None:
    keyring = _keyring_module()
    if keyring is None:
        raise RuntimeError("keyring is not installed; run: python -m pip install keyring")
    client_id = client_id.strip()
    client_secret = client_secret.strip()
    if not client_id or not client_secret:
        raise ValueError("client_id and client_secret must both be non-empty")
    keyring.set_password(SERVICE, CLIENT_ID_KEY, client_id)
    keyring.set_password(SERVICE, CLIENT_SECRET_KEY, client_secret)


def delete_toss_credentials() -> None:
    keyring = _keyring_module()
    if keyring is None:
        return
    for username in (CLIENT_ID_KEY, CLIENT_SECRET_KEY):
        try:
            keyring.delete_password(SERVICE, username)
        except Exception:
            pass


def status() -> int:
    env_id = bool(os.getenv(CLIENT_ID_KEY, "").strip())
    env_secret = bool(os.getenv(CLIENT_SECRET_KEY, "").strip())
    saved_id, saved_secret = load_saved_toss_credentials()
    print(f"ENV_CLIENT_ID={'YES' if env_id else 'NO'}")
    print(f"ENV_CLIENT_SECRET={'YES' if env_secret else 'NO'}")
    print(f"SAVED_CLIENT_ID={'YES' if saved_id else 'NO'}")
    print(f"SAVED_CLIENT_SECRET={'YES' if saved_secret else 'NO'}")
    if (env_id and env_secret) or (saved_id and saved_secret):
        print("TOSS_CREDENTIALS=READY")
        return 0
    print("TOSS_CREDENTIALS=MISSING")
    return 1


def setup_from_prompt() -> None:
    current_id, _ = load_saved_toss_credentials()
    suffix = f" [{current_id}]" if current_id else ""
    client_id = input(f"Toss Client ID{suffix}: ").strip() or current_id
    client_secret = getpass.getpass("Toss Client Secret (hidden): ").strip()
    if not client_id:
        raise ValueError("Client ID is required")
    if not client_secret:
        raise ValueError("Client Secret is required")
    save_toss_credentials(client_id, client_secret)
    print("Saved Toss Open API credentials to the OS credential store.")
    print("The secret was not written to the repository and was not printed.")


def migrate_env() -> None:
    client_id = os.getenv(CLIENT_ID_KEY, "").strip()
    client_secret = os.getenv(CLIENT_SECRET_KEY, "").strip()
    if not client_id or not client_secret:
        raise ValueError("TOSS_CLIENT_ID / TOSS_CLIENT_SECRET are not both set in this process")
    save_toss_credentials(client_id, client_secret)
    print("Saved current Toss environment credentials to the OS credential store.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Manage Toss Open API credentials safely")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="Prompt once and save to the OS credential store")
    sub.add_parser("status", help="Show whether credentials are available (never prints values)")
    sub.add_parser("delete", help="Delete saved credentials from the OS credential store")
    sub.add_parser("migrate-env", help="Save currently-set TOSS_CLIENT_* env vars to the OS credential store")
    args = ap.parse_args()

    if args.command == "setup":
        setup_from_prompt()
    elif args.command == "status":
        raise SystemExit(status())
    elif args.command == "delete":
        delete_toss_credentials()
        print("Deleted saved Toss Open API credentials, if present.")
    elif args.command == "migrate-env":
        migrate_env()


if __name__ == "__main__":
    main()
