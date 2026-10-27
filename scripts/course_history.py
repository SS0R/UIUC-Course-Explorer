#!/usr/bin/env python3
"""Build first-offering and offered-semester indexes locally; no API requests."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
TERM_CODES = {"winter": "wi", "spring": "sp", "summer": "su", "fall": "fa"}
TERM_ORDER = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}
BASELINE = "2004-fa"


def prepare_indexes(directory, manifest):
    snapshots = manifest["terms"]
    discovered = manifest.get("availableTerms")
    expected = discovered if discovered is not None else list(snapshots.values())
    terms = {"%s-%s" % (t["year"], TERM_CODES[t["term"]]): t for t in expected}
    discovered_keys = set(terms)
    # Include a just-published new term even if discovery hasn't caught up yet.
    terms.update({key: entry for key, entry in snapshots.items() if key not in terms})
    ordered = sorted(terms, key=lambda key: (terms[key]["year"], TERM_ORDER[terms[key]["term"]]))
    contiguous = discovered is not None and bool(ordered) and ordered[0] == BASELINE
    first_offered, offered_terms, source_snapshots, eligible = {}, {}, {}, []
    for key in ordered:
        if key not in discovered_keys:
            contiguous = False
        entry = snapshots.get(key)
        if entry is None:
            contiguous = False
            continue
        filename = entry["file"]
        if Path(filename).name != filename:
            raise ValueError("Invalid snapshot filename for %s" % key)
        body = (directory / filename).read_bytes()
        if hashlib.sha256(body).hexdigest() != entry["sha256"]:
            raise ValueError("Snapshot checksum mismatch for %s" % key)
        data = json.loads(body)
        if (data.get("schemaVersion") != 1 or data.get("year") != entry["year"] or
                data.get("term") != entry["term"] or not isinstance(data.get("courses"), list)):
            raise ValueError("Invalid snapshot for %s" % key)
        source_snapshots[key] = entry["sha256"]
        for course in data["courses"]:
            # Metadata-only catalog entries are not evidence of an offering.
            if course.get("sections"):
                course_key = "%s %s" % (course["subject"], course["number"])
                first_offered.setdefault(course_key, key)
                offerings = offered_terms.setdefault(course_key, [])
                if not offerings or offerings[-1] != key:
                    offerings.append(key)
        # Fall 2004 is a baseline, not proof all its courses were new then.
        if contiguous and key != BASELINE:
            eligible.append(key)
    payloads = {
        "courseHistory": ("course-history", dict(schemaVersion=1, coverageStart=BASELINE, eligibleTerms=eligible,
            sourceSnapshots=source_snapshots, firstOffered=first_offered)),
        "courseOfferings": ("course-offerings", dict(schemaVersion=1, coverageStart=BASELINE,
            coverageComplete=contiguous, sourceSnapshots=source_snapshots, offeredTerms=offered_terms))
    }
    result = {}
    for name, (prefix, payload) in payloads.items():
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        digest = hashlib.sha256(body).hexdigest()
        filename = "%s-%s.json" % (prefix, digest[:16])
        result[name] = (filename, body, dict(file=filename, sha256=digest, coverageStart=BASELINE))
    return result


def prepare_history(directory, manifest):
    return prepare_indexes(directory, manifest)["courseHistory"]


def main():
    try:
        from . import update_catalog as catalog
    except ImportError:
        import update_catalog as catalog
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "catalog")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / ".catalog-cache")
    args = parser.parse_args()
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    lock = sqlite3.connect(args.cache_dir / "updater-lock.sqlite3", timeout=0)
    try:
        try:
            lock.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError:
            parser.exit(1, "Another updater is running. Stop it before rebuilding history.\n")
        manifest = catalog.read_manifest(args.output)
        indexes = prepare_indexes(args.output, manifest)
        for name, (filename, body, entry) in indexes.items():
            catalog.atomic_write(args.output / filename, body)
            manifest[name] = entry
            print("Built %s: %s bytes" % (filename, len(body)))
        catalog.atomic_write(args.output / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
    finally:
        lock.close()


if __name__ == "__main__":
    main()
