#!/usr/bin/env python3
"""Compatibility shim. Prefer the packaged CLI: `redactus` or `python -m redactus`."""

from __future__ import annotations

try:
    from redactus.cli import main
except ImportError as exc:
    raise SystemExit(
        "Redactus is not installed in this Python.\n"
        "From the repo root:  python3.12 -m pip install -e ."
    ) from exc


if __name__ == "__main__":
    main()
