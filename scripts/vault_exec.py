#!/usr/bin/env python3
"""Load Vault secrets into os.environ and exec the remaining argv (no shell eval)."""

from __future__ import annotations

import os
import sys


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: vault_exec.py <command> [args...]", file=sys.stderr)
        raise SystemExit(2)

    import vault_env_loader  # noqa: F401 — loads secrets on import

    cmd = sys.argv[1]
    args = sys.argv[1:]
    os.execvpe(cmd, args, os.environ)


if __name__ == "__main__":
    main()
