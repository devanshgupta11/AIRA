r"""Refresh the data the deployed console serves.

The cloud image ships a snapshot of the incident database and the vector store, because the
working copies are gitignored and because the live qdrant directory holds a lock file that would
stop the container starting. Run this before deploying, with the backend running or not.

    backend/.venv/Scripts/python.exe scripts/refresh_deploy_seed.py
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "deploy" / "seed"


def main() -> int:
    SEED.mkdir(parents=True, exist_ok=True)

    db_src = ROOT / "backend" / "data" / "aira.db"
    if not db_src.exists():
        print(f"no database at {db_src}")
        return 1
    # sqlite's backup API, not a file copy: it takes a consistent snapshot even while the backend
    # is writing, which a copy of a live database cannot promise.
    src = sqlite3.connect(f"file:{db_src}?mode=ro", uri=True)
    dst = sqlite3.connect(SEED / "aira.db")
    src.backup(dst)
    dst.close()
    src.close()

    q_src = ROOT / "qdrant"
    if q_src.exists():
        shutil.rmtree(SEED / "qdrant", ignore_errors=True)
        # .lock belongs to the process that has the store open. Shipping one makes the container
        # refuse to start on a lock nobody is holding.
        shutil.copytree(q_src, SEED / "qdrant",
                        ignore=shutil.ignore_patterns(".lock", "*.lock"))

    size = sum(f.stat().st_size for f in SEED.rglob("*") if f.is_file())
    n = sqlite3.connect(SEED / "aira.db").execute("select count(*) from incidents").fetchone()[0]
    print(f"seeded {SEED}  ({size / 1048576:.1f} MiB, {n} incidents)")
    print("Commit deploy/seed/ for the deployed console to serve real data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
