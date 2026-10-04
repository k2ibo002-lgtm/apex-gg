#!/usr/bin/env python3
"""APEX hourly scanner job (runs on GitHub Actions). Imports the all-in-one backend."""
import os, sys, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "api"))

import index  # noqa: E402  (the all-in-one backend)


def main():
    if not index.config.DATABASE_URL:
        print("DATABASE_URL missing", flush=True)
        return 1
    index.init()
    index.scan_once()
    index.track_outcomes()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
