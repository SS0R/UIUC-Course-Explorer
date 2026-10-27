#!/usr/bin/env python3
"""Remove collected finals from local checkpoints and exported archives, offline."""
import argparse
import json
from pathlib import Path
import sqlite3

try:
    from . import archive_catalog as archive
    from . import update_catalog as catalog
except ImportError:
    import archive_catalog as archive
    import update_catalog as catalog


def remove_final_records(db):
    removed = {}
    # Clear deleted blob contents in SQLite's reusable pages as well.
    db.execute("PRAGMA secure_delete=ON")
    with db:
        for table in ("responses", "missing_responses"):
            removed[table] = db.execute("DELETE FROM %s WHERE path LIKE '%%/finalexam.xml' "
                "OR path LIKE '%%/finalexam.xml?%%'" % table).rowcount
        rows = db.execute("SELECT term_key,completed,errors FROM archive_terms").fetchall()
        for key, completed, errors in rows:
            phases = [p for p in json.loads(completed) if p != "finals"]
            errors = json.loads(errors)
            errors.pop("finals", None)
            db.execute("UPDATE archive_terms SET completed=?,errors=? WHERE term_key=?",
                       (catalog.compact(phases), catalog.compact(errors), key))
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=catalog.ROOT / ".catalog-cache")
    parser.add_argument("--output", type=Path, default=catalog.ROOT / "data" / "catalog")
    parser.add_argument("--archive-output", type=Path, default=catalog.ROOT / "data" / "archive")
    args = parser.parse_args()
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    lock = sqlite3.connect(args.cache_dir / "updater-lock.sqlite3", timeout=0)
    db = None
    try:
        try:
            lock.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError:
            parser.exit(1, "Another updater is running. Stop it before deleting finals.\n")
        db = catalog.open_db(args.cache_dir / "catalog.sqlite3")
        worker_args = archive.arguments(["--all", "--cache-dir", str(args.cache_dir),
            "--output", str(args.output), "--archive-output", str(args.archive_output)])
        worker = archive.Archive(worker_args, db)
        manifest_path = args.archive_output / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"terms": {}}
        terms = [dict(year=e["year"], term=e["term"]) for e in manifest["terms"].values()]
        # Recover archived resources first if the local cache was recreated.
        for term in terms:
            worker.run_id(term)
        before = {table: db.execute("SELECT count(*) FROM %s" % table).fetchone()[0]
                  for table in ("responses", "missing_responses")}
        removed = remove_final_records(db)
        print("Removed final-exam records: %s" % removed, flush=True)
        worker.terms = terms
        for index, term in enumerate(terms, 1):
            worker.export(term)
            if index % 10 == 0 or index == len(terms):
                print("Rebuilt %s/%s term archives without finals" % (index, len(terms)), flush=True)
        log_path = args.cache_dir / "archive.log"
        if log_path.exists():
            lines = log_path.read_bytes().splitlines(keepends=True)
            kept = [line for line in lines if b"/finalexam.xml" not in line and not line.strip().startswith(b"FINALS [")]
            catalog.atomic_write(log_path, b"".join(kept))
        for path in args.archive_output.glob("*.zip.tmp"):
            if b"/finalexam.xml" in path.read_bytes():
                path.unlink()
        for table in ("responses", "missing_responses"):
            assert db.execute("SELECT count(*) FROM %s WHERE path LIKE '%%/finalexam.xml' "
                "OR path LIKE '%%/finalexam.xml?%%'" % table).fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM %s" % table).fetchone()[0] == before[table] - removed[table]
        complete = all(all(p in worker.completed(term) for p in archive.PHASES) for term in terms)
        worker.status.update(pid=None, phase=None, currentTerm=None, currentPath=None,
                             skippedPhases=list(archive.OPTIONAL_PHASES), completedAt=catalog.now())
        worker.save_status(state="complete" if complete else "incomplete", error=None)
        print("Finals removed; retained schedules, catalogs, and sessions. Collection state: %s." % worker.status["state"], flush=True)
    finally:
        if db is not None:
            db.close()
        lock.close()


if __name__ == "__main__":
    main()
