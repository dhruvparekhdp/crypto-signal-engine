"""
Set a new admin password. Run on the box that hosts the engine:

    .venv/bin/python scripts/set_admin_password.py

The web UI has no password-change screen, and ADMIN_PASSWORD in .env is only
read when the admin table is empty, so this is the way to rotate it. The
password is typed at a hidden prompt (never on the command line, where it
would land in shell history and `ps`). Setting it also clears the stored
session token, which logs out anyone holding the old one.
"""
from __future__ import annotations

import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.database import AsyncSessionFactory, init_db  # noqa: E402
from storage.repository import Repository  # noqa: E402

MIN_LENGTH = 12


def check(pw: str) -> str | None:
    """Why a password is refused, or None if it is acceptable."""
    if len(pw) < MIN_LENGTH:
        return f"use at least {MIN_LENGTH} characters"
    if pw.isdigit():
        return "all digits (a phone number is the password that leaked)"
    return None


async def main() -> int:
    first = getpass.getpass("New admin password: ")
    problem = check(first)
    if problem:
        print(f"Refused: {problem}.")
        return 2
    if getpass.getpass("Type it again: ") != first:
        print("The two entries differ. Nothing changed.")
        return 2
    await init_db()
    async with AsyncSessionFactory() as session:
        await Repository(session).set_admin_password(first)
    print("Admin password changed. Old sessions are logged out.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
