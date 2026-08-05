"""Move stored files into the domain-mirrored layout and sweep what nothing references.

    python -m tools.migrate_storage              # dry run: report only, touches nothing
    python -m tools.migrate_storage --apply      # move files + rewrite DB paths
    python -m tools.migrate_storage --apply --prune   # ...and delete the orphans

`--prune` is deliberately a second flag and runs strictly after the move + a full
verification pass: every path in the DB has to resolve to a file that exists before
anything is deleted, so a file the DB still points at can never be swept as an orphan.
"""
import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app import layout  # noqa: E402
from app.config import DB_PATH, STORAGE_DIR  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Project  # noqa: E402


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return str(n)


def backup_db() -> Path:
    dest = DB_PATH.with_name(f"{DB_PATH.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(DB_PATH, dest)
    return dest


def verify(db, project_id: int) -> list[str]:
    """Paths the DB records that have no file behind them."""
    return [rel for rel in layout.owners(db, project_id) if not (STORAGE_DIR / rel).exists()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually move files")
    ap.add_argument("--prune", action="store_true", help="delete unreferenced files (needs --apply)")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        project_ids = [p.id for p in db.scalars(select(Project)).all()]

        # Baseline: whatever is already missing was missing before this ran, and must not
        # be blamed on (or block) the migration.
        pre_missing = {pid: set(verify(db, pid)) for pid in project_ids}
        for pid, missing in pre_missing.items():
            if missing:
                print(f"project {pid}: {len(missing)} recorded file(s) already missing from disk")
                for rel in sorted(missing)[:10]:
                    print(f"    - {rel}")

        if not args.apply:
            for pid in project_ids:
                plan = {
                    rel: folder for rel, folder in layout.owners(db, pid).items()
                    if (STORAGE_DIR / rel).exists()
                    and (STORAGE_DIR / rel).parent.resolve() != (STORAGE_DIR / folder).resolve()
                }
                report = layout.prune_orphans(db, pid, dry_run=True)
                print(f"\nproject {pid}: would move {len(plan)} file(s), keep {report['kept']}, "
                      f"delete {len(report['deleted'])} orphan(s) ({human(report['bytes'])})")
                for rel, folder in list(plan.items())[:5]:
                    print(f"    {rel}\n      -> {folder}/")
            print("\nDry run — nothing changed. Re-run with --apply (and --prune to sweep).")
            return 0

        print(f"DB backed up to {backup_db().name}")
        for pid in project_ids:
            result = layout.reconcile(db, pid)
            print(f"project {pid}: moved {len(result['moved'])} file(s)")

        # Nothing is deleted until every recorded path resolves again.
        broke = False
        for pid in project_ids:
            now_missing = set(verify(db, pid)) - pre_missing[pid]
            if now_missing:
                broke = True
                print(f"!! project {pid}: {len(now_missing)} path(s) no longer resolve after the move")
                for rel in sorted(now_missing)[:20]:
                    print(f"    - {rel}")
        if broke:
            print("\nAborting before the sweep — the DB backup above is the way back.")
            return 1
        print("Verified: every recorded file resolves.")

        if args.prune:
            total = 0
            for pid in project_ids:
                report = layout.prune_orphans(db, pid, dry_run=False)
                total += report["bytes"]
                print(f"project {pid}: deleted {len(report['deleted'])} orphan(s), "
                      f"freed {human(report['bytes'])}, kept {report['kept']}")
            still_missing = [rel for pid in project_ids for rel in set(verify(db, pid)) - pre_missing[pid]]
            if still_missing:
                print(f"!! {len(still_missing)} recorded file(s) went missing during the sweep")
                return 1
            print(f"Freed {human(total)}. Every recorded file still resolves.")
        else:
            print("Skipped the orphan sweep (pass --prune to run it).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
