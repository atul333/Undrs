"""
╔══════════════════════════════════════════════════════════════════╗
║   API KEY MANAGER — admin CLI for your Undress AI API server     ║
╚══════════════════════════════════════════════════════════════════╝

Usage:
    python manage_keys.py create [--label "My Bot"] [--credits 500]
    python manage_keys.py list
    python manage_keys.py add-credits <key> <amount>
    python manage_keys.py revoke <key>

The server does NOT need to be running for this script to work.
It reads/writes the api_server.db directly.
"""

import argparse
import asyncio
import hashlib
import os
import secrets
import sys
from datetime import datetime, timezone

import aiosqlite

DB_PATH = "api_server.db"
DEFAULT_CREDITS = 100


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _generate_key() -> str:
    return "udt_" + secrets.token_hex(24)


async def create_key(label: str = None, telegram_id: int = None, credits: int = DEFAULT_CREDITS):
    key = _generate_key()
    key_hash = _hash_key(key)
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                key TEXT PRIMARY KEY,
                key_hash TEXT UNIQUE NOT NULL,
                telegram_id INTEGER,
                label TEXT,
                credits INTEGER DEFAULT 100,
                is_active INTEGER DEFAULT 1,
                can_create_photos INTEGER DEFAULT 1,
                can_create_videos INTEGER DEFAULT 1,
                last_bought_at TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute(
            "INSERT INTO api_keys (key, key_hash, telegram_id, label, credits, is_active, "
            "can_create_photos, can_create_videos, created_at) VALUES (?, ?, ?, ?, ?, 1, 1, 1, ?)",
            (key, key_hash, telegram_id, label, credits, now),
        )
        await db.commit()
    print("\n[OK] API Key Created Successfully!")
    print("-" * 60)
    print(f"  Key     : {key}")
    print(f"  Label   : {label or '(none)'}")
    print(f"  Credits : {credits}")
    print(f"  Created : {now}")
    print("-" * 60)
    print("[!] Save this key now -- it won't be shown again.\n")
    return key


async def list_keys():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute(
                "SELECT key, label, telegram_id, credits, is_active, created_at "
                "FROM api_keys ORDER BY created_at DESC"
            ) as cur:
                rows = await cur.fetchall()
        except aiosqlite.OperationalError:
            print("No keys found (DB not initialised yet).")
            return

    if not rows:
        print("No API keys found.")
        return

    print(f"\n{'KEY':<55} {'LABEL':<20} {'CREDITS':>7} {'ACTIVE':>6}  CREATED")
    print("-" * 120)
    for r in rows:
        active = "YES" if r["is_active"] else "NO "
        label = (r["label"] or "")[:18]
        print(f"  {r['key']:<53} {label:<20} {r['credits']:>7} {active:>6}  {r['created_at'][:19]}")
    print()


async def add_credits(key: str, amount: int):
    key_hash = _hash_key(key)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE api_keys SET credits = credits + ?, last_bought_at = ? WHERE key_hash = ?",
            (amount, datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), key_hash),
        )
        await db.commit()
        if cursor.rowcount == 0:
            print(f"[ERR] Key not found: {key}")
            return
        async with db.execute("SELECT credits FROM api_keys WHERE key_hash = ?", (key_hash,)) as cur:
            row = await cur.fetchone()
            print(f"[OK] Added {amount} credits. New balance: {row[0]}")


async def revoke_key(key: str):
    key_hash = _hash_key(key)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE api_keys SET is_active = 0 WHERE key_hash = ?", (key_hash,)
        )
        await db.commit()
        if cursor.rowcount == 0:
            print(f"[ERR] Key not found: {key}")
        else:
            print(f"[OK] Key revoked: {key}")


def main():
    parser = argparse.ArgumentParser(description="Undress API Key Manager")
    sub = parser.add_subparsers(dest="command")

    # create
    p_create = sub.add_parser("create", help="Generate a new API key")
    p_create.add_argument("--label", default=None, help="Human-readable label")
    p_create.add_argument("--telegram-id", type=int, default=None)
    p_create.add_argument("--credits", type=int, default=DEFAULT_CREDITS)

    # list
    sub.add_parser("list", help="List all API keys")

    # add-credits
    p_add = sub.add_parser("add-credits", help="Add credits to a key")
    p_add.add_argument("key", help="The API key (udt_...)")
    p_add.add_argument("amount", type=int, help="Credits to add")

    # revoke
    p_rev = sub.add_parser("revoke", help="Revoke an API key")
    p_rev.add_argument("key", help="The API key (udt_...)")

    args = parser.parse_args()

    if args.command == "create":
        asyncio.run(create_key(
            label=args.label,
            telegram_id=args.telegram_id,
            credits=args.credits,
        ))
    elif args.command == "list":
        asyncio.run(list_keys())
    elif args.command == "add-credits":
        asyncio.run(add_credits(args.key, args.amount))
    elif args.command == "revoke":
        asyncio.run(revoke_key(args.key))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
