#!/usr/bin/env python3
"""Archive every public Illinois term, preserving complete API responses.

Run --all --background to start; --status to inspect; --stop to stop safely.
The default run resumes saved work. See docs/UPDATING_COURSE_DATA.md.
"""
import argparse
import errno
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
import zipfile
from types import SimpleNamespace

try:
    from . import update_catalog as catalog
except ImportError:
    import update_catalog as catalog

API = "https://courses.illinois.edu/cisapp/explorer/"
PHASES = ("schedule", "catalog", "sessions")
OPTIONAL_PHASES = ("finals", "catalog-details")
ORDER = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}


def is_storage_error(exc):
    if isinstance(exc, OSError):
        return exc.errno in (errno.ENOSPC, errno.EDQUOT)
    if isinstance(exc, sqlite3.OperationalError):
        return str(exc).lower() in ("unable to open database file", "database or disk is full", "disk i/o error")
    return False


def discover_terms(body):
    root = catalog.xml(body, "schedule")
    terms = []
    for year in root.findall("calendarYears/calendarYearSummary"):
        for term in year.findall("terms/termDetail"):
            label = catalog.value(term, "label")
            match = re.fullmatch(r"(Winter|Spring|Summer|Fall) (\d{4})", label)
            if not match or match[2] != year.get("id"):
                raise catalog.CatalogError("Unexpected term in discovery: %s" % label)
            terms.append(dict(year=int(match[2]), term=match[1].lower(), id=term.get("id"),
                public=catalog.value(term, "publicIndicator") == "Y",
                archived=catalog.value(term, "archiveIndicator") == "Y",
                default=catalog.value(term, "defaultTerm") == "Y",
                enrolling=catalog.value(term, "enrollingTerm") == "Y"))
    if not terms or len({(t["year"], t["term"]) for t in terms}) != len(terms):
        raise catalog.CatalogError("Empty or duplicate term discovery")
    return sorted(terms, key=lambda t: (t["year"], ORDER[t["term"]]))


def subject_ids(body, year, term):
    root = catalog.xml(body, "term")
    if catalog.value(root, "parents/calendarYear") != str(year):
        raise catalog.CatalogError("Wrong year in term response")
    expected_id = "1%s%s" % (year, {"winter": "0", "spring": "1", "summer": "5", "fall": "8"}[term])
    if root.get("id") != expected_id:
        raise catalog.CatalogError("Wrong term ID in term response")
    # Catalog term labels contain only the year; schedule labels include the
    # season. The Banner term ID verifies the season in both representations.
    if catalog.value(root, "label").lower() not in (str(year), "%s %s" % (term, year)):
        raise catalog.CatalogError("Wrong label in term response")
    container = root.find("subjects")
    if container is None:
        raise catalog.CatalogError("Missing subjects container")
    subjects = [s.get("id", "") for s in container.findall("subject")]
    if any(not re.fullmatch(r"[A-Z]{2,8}", s) for s in subjects) or len(subjects) != len(set(subjects)):
        raise catalog.CatalogError("Invalid or duplicate subjects")
    return sorted(subjects)


def validate_subject(body, subject, year, term):
    root = catalog.xml(body, "subject")
    if root.get("id") != subject or catalog.value(root, "parents/term").lower() != "%s %s" % (term, year):
        raise catalog.CatalogError("Subject response has the wrong identity")
    courses = root.find("cascadingCourses")
    if courses is None:
        raise catalog.CatalogError("Bulk subject response is incomplete")
    seen = set()
    for course in courses.findall("cascadingCourse"):
        code = course.get("id", "")
        if not re.fullmatch(re.escape(subject) + r" \d{1,5}", code) or code in seen:
            raise catalog.CatalogError("Invalid/duplicate catalog course: %s" % code)
        seen.add(code)
    return root


def validate_course(body, subject, number):
    root = catalog.xml(body, "course")
    if root.get("id") != "%s %s" % (subject, number):
        raise catalog.CatalogError("Course response has the wrong identity")
    return root


def validate_final(body, subject, number):
    root = catalog.xml(body, "finalExam")
    if catalog.value(root, "subjectCode") != subject or catalog.value(root, "courseNumber") != number:
        raise catalog.CatalogError("Final exam response has the wrong course")
    return root


def term_key(term):
    return "%s-%s" % (term["year"], catalog.TERMS[term["term"]])


class Archive:
    def __init__(self, args, db):
        self.args, self.db = args, db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS archive_terms (
              term_key TEXT PRIMARY KEY, run_id TEXT NOT NULL, completed TEXT NOT NULL,
              errors TEXT NOT NULL);
        """)
        self.stop_file = args.cache_dir / "archive.stop"
        self.status_file = args.cache_dir / "archive-status.json"
        self.status = dict(pid=os.getpid(), state="running", startedAt=catalog.now(),
                           phase="discovery", currentTerm=None, currentPath=None)
        self.client = catalog.Client(db, "archive-discovery", delay=args.delay, retries=args.retries,
            timeout=args.timeout, sleep=self.sleep, base=API)
        self.terms = []
        self.last_stats = 0
        self.refresh_applied = False

    def check_stop(self):
        if self.stop_file.exists():
            raise KeyboardInterrupt

    def sleep(self, seconds):
        deadline = time.monotonic() + seconds
        while True:
            self.check_stop()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 1))

    def save_status(self, **updates):
        self.status.update(updates, updatedAt=catalog.now())
        try:
            self.refresh_statistics(force="state" in updates)
            self.status.pop("storageError", None)
        except sqlite3.OperationalError as exc:
            if not is_storage_error(exc):
                raise
            self.status["storageError"] = str(exc)
        self.persist_status()

    def refresh_statistics(self, force=False):
        selected = {term_key(t) for t in self.terms}
        stages = [(completed, errors) for key, completed, errors in
            self.db.execute("SELECT term_key, completed, errors FROM archive_terms") if not selected or key in selected]
        self.status["completedByPhase"] = {phase: sum(phase in json.loads(row[0]) for row in stages) for phase in PHASES}
        self.status["termsWithErrors"] = sum(bool(json.loads(row[1])) for row in stages)
        self.status["publicTerms"] = len(self.terms)
        if "cachedResponses" not in self.status or time.monotonic() - self.last_stats >= 60 or force:
            self.status["cachedResponses"] = self.db.execute(
                "SELECT COUNT(*) FROM responses WHERE run_id >= 'archive-' AND run_id < 'archive.'").fetchone()[0]
            self.status["unavailableResponses"] = self.db.execute(
                "SELECT COUNT(*) FROM missing_responses WHERE run_id >= 'archive-' AND run_id < 'archive.'").fetchone()[0]
            self.last_stats = time.monotonic()

    def persist_status(self):
        try:
            catalog.atomic_write(self.status_file, (json.dumps(self.status, indent=2) + "\n").encode())
        except OSError as exc:
            if exc.errno not in (errno.ENOSPC, errno.EDQUOT):
                raise
            # Status reporting must not terminate the low-space waiting loop.
            # Keep the state in memory; the next successful update persists it.

    def storage_log(self, message):
        try:
            print(message, flush=True)
        except OSError as exc:
            if not is_storage_error(exc):
                raise

    def run_with_recovery(self):
        while True:
            try:
                return self.run()
            except Exception as exc:
                if not is_storage_error(exc):
                    raise
                try:
                    self.db.rollback()
                except sqlite3.OperationalError as rollback_error:
                    if not is_storage_error(rollback_error):
                        raise
                self.status.update(state="waiting_for_storage", error=str(exc), updatedAt=catalog.now())
                self.persist_status()
                self.storage_log("Storage temporarily unavailable (%s); retrying saved work after 30 seconds." % exc)
                self.sleep(30)
                self.wait_for_space()
                self.status.update(state="running", error=None, updatedAt=catalog.now())
                self.persist_status()

    def wait_for_space(self):
        directories = (self.args.cache_dir, self.args.output, self.args.archive_output)
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
        waiting = False
        while True:
            self.check_stop()
            low = None
            for directory in directories:
                free = shutil.disk_usage(directory).free
                if free < self.args.min_free_mb * 1024 * 1024:
                    low = (directory, free)
                    break
            if low is None:
                if waiting:
                    self.storage_log("Disk space recovered; resuming saved work.")
                    self.save_status(state="running", diskDirectory=None, diskFreeMb=None)
                return
            if not waiting:
                self.storage_log("Low disk space at %s; waiting for at least %s MB free. Checking every 30 seconds." %
                                 (low[0], self.args.min_free_mb))
            waiting = True
            self.save_status(state="waiting_for_disk", diskDirectory=str(low[0]),
                             diskFreeMb=round(low[1] / 1024 / 1024, 1))
            self.sleep(30)

    def fetch(self, path, validate, optional=False):
        self.check_stop()
        self.save_status(currentPath=path)
        self.wait_for_space()
        return self.client.fetch(path, validate, allow_missing=optional)

    def run_id(self, term):
        key = term_key(term)
        row = self.db.execute("SELECT run_id FROM archive_terms WHERE term_key=?", (key,)).fetchone()
        if row:
            return row[0]
        run_id = "archive-" + uuid.uuid4().hex
        if not self.args.refresh and self.restore_term(term, run_id):
            return run_id
        with self.db:
            self.db.execute("INSERT INTO archive_terms VALUES (?, ?, '[]', '{}')", (key, run_id))
        # Reuse the previous full semester import, preserving fetch timestamps.
        scope = catalog.compact(dict(year=term["year"], term=term["term"], subject=None, course=None))
        previous = self.db.execute("SELECT id FROM runs WHERE scope=? AND status='published' ORDER BY rowid DESC LIMIT 1", (scope,)).fetchone()
        if previous:
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO responses SELECT ?, 'schedule/' || path, fetched_at, body "
                    "FROM responses WHERE run_id=?", (run_id, previous[0]))
        return run_id

    def restore_term(self, term, run_id):
        """Recover checkpoints from committed archives after a fresh clone/cache loss."""
        manifest_path = self.args.archive_output / "manifest.json"
        if not manifest_path.exists():
            return False
        key = term_key(term)
        entry = json.loads(manifest_path.read_text())["terms"].get(key)
        if not entry:
            return False
        if entry.get("file") != key + ".zip":
            raise catalog.CatalogError("Unexpected archive filename for %s" % key)
        path = self.args.archive_output / entry["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise catalog.CatalogError("Archive checksum mismatch for %s; keep the ZIP and inspect it" % key)
        with zipfile.ZipFile(path) as bundle, self.db:
            metadata = json.loads(bundle.read("manifest.json"))
            if metadata.get("schemaVersion") != 1 or metadata.get("year") != term["year"] or metadata.get("term") != term["term"]:
                raise catalog.CatalogError("Archive identity mismatch for %s" % key)
            seen = set()
            self.db.execute("INSERT INTO archive_terms VALUES (?, ?, ?, ?)",
                (key, run_id, catalog.compact(metadata["completedPhases"]), catalog.compact(metadata["errors"])))
            for resource in metadata["resources"]:
                body = bundle.read(resource["file"])
                request_path = resource["path"]
                if request_path in seen or hashlib.sha256(body).hexdigest() != resource["sha256"]:
                    raise catalog.CatalogError("Duplicate resource or corrupt body in %s" % key)
                seen.add(request_path)
                packed = gzip.compress(body, compresslevel=6, mtime=0)
                if resource["status"] == 200:
                    self.db.execute("INSERT INTO responses VALUES (?, ?, ?, ?)",
                                    (run_id, request_path, resource["fetchedAt"], packed))
                elif resource["status"] == 404:
                    self.db.execute("INSERT INTO missing_responses VALUES (?, ?, ?, ?, ?)",
                                    (run_id, request_path, resource["fetchedAt"], 404, packed))
                else:
                    raise catalog.CatalogError("Unsupported response status in archive")
        print("Restored %s from its verified ZIP (%s resources)" % (key, len(seen)), flush=True)
        return True

    def completed(self, term):
        row = self.db.execute("SELECT completed FROM archive_terms WHERE term_key=?", (term_key(term),)).fetchone()
        return json.loads(row[0]) if row else []

    def cached_schedule_ready(self, term):
        prefix = "schedule/%s/%s" % (term["year"], term["term"])
        row = self.db.execute("SELECT body FROM responses WHERE run_id=? AND path=?",
                              (self.run_id(term), prefix + ".xml")).fetchone()
        if not row:
            return False
        subjects = subject_ids(catalog.response_bytes(row[0]), term["year"], term["term"])
        paths = {r[0] for r in self.db.execute("SELECT path FROM responses WHERE run_id=?", (self.run_id(term),))}
        return all(prefix + "/%s.xml?mode=cascade" % subject in paths for subject in subjects)

    def set_phase(self, term, phase, error=None):
        key = term_key(term)
        row = self.db.execute("SELECT completed,errors FROM archive_terms WHERE term_key=?", (key,)).fetchone()
        completed, errors = json.loads(row[0]), json.loads(row[1])
        if error:
            errors[phase] = str(error)
        else:
            if phase not in completed:
                completed.append(phase)
            errors.pop(phase, None)
        with self.db:
            self.db.execute("UPDATE archive_terms SET completed=?, errors=? WHERE term_key=?",
                            (catalog.compact(completed), catalog.compact(errors), key))

    def export(self, term):
        run_id = self.run_id(term)
        key = term_key(term)
        directory = self.args.archive_output
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (key + ".zip")
        temp = path.with_suffix(".zip.tmp")
        resources = []
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
            for table, status in (("responses", 200), ("missing_responses", 404)):
                for request_path, fetched, body in self.db.execute(
                        "SELECT path,fetched_at,body FROM %s WHERE run_id=? ORDER BY path" % table, (run_id,)):
                    body = catalog.response_bytes(body)
                    name = ("responses/" if status == 200 else "unavailable/") + request_path.split("?")[0]
                    bundle.writestr(name, body)
                    resources.append(dict(path=request_path, url=API + request_path, status=status,
                        fetchedAt=fetched, file=name, sha256=hashlib.sha256(body).hexdigest(), bytes=len(body)))
            completed = self.completed(term)
            errors = json.loads(self.db.execute("SELECT errors FROM archive_terms WHERE term_key=?", (key,)).fetchone()[0])
            metadata = dict(schemaVersion=1, year=term["year"], term=term["term"],
                generatedAt=catalog.now(), completedPhases=completed, errors=errors,
                requiredPhases=list(PHASES), complete=all(p in completed for p in PHASES), resources=resources)
            bundle.writestr("manifest.json", json.dumps(metadata, indent=2))
        os.replace(temp, path)
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else dict(schemaVersion=1, terms={})
        manifest["terms"][key] = dict(file=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            year=term["year"], term=term["term"], complete=metadata["complete"],
            completedPhases=completed, requiredPhases=list(PHASES), errors=errors, updatedAt=metadata["generatedAt"],
            responses=sum(r["status"] == 200 for r in resources), unavailable=sum(r["status"] == 404 for r in resources))
        catalog.atomic_write(manifest_path, (json.dumps(manifest, indent=2) + "\n").encode())

    def bulk(self, term, module):
        year, name = term["year"], term["term"]
        prefix = "%s/%s/%s" % (module, year, name)
        body = self.fetch(prefix + ".xml", lambda b: subject_ids(b, year, name))
        courses = []
        for subject in subject_ids(body, year, name):
            body = self.fetch(prefix + "/%s.xml?mode=cascade" % subject,
                lambda b: validate_subject(b, subject, year, name))
            if module == "schedule":
                courses.extend(catalog.courses_from_xml(body, subject, year, name))
        return courses

    def course_ids(self, term, module):
        prefix = "%s/%s/%s/" % (module, term["year"], term["term"])
        result = set()
        for path, body in self.db.execute("SELECT path,body FROM responses WHERE run_id=? AND path LIKE ?",
                                         (self.run_id(term), prefix + "%.xml?mode=cascade")):
            for course in catalog.xml(catalog.response_bytes(body), "subject").findall("cascadingCourses/cascadingCourse"):
                subject, number = course.get("id").split()
                result.add((subject, number))
        return sorted(result)

    def phase(self, term, phase):
        year, name = term["year"], term["term"]
        self.client.run_id = self.run_id(term)
        self.save_status(phase=phase, currentTerm=term_key(term))
        if phase in ("schedule", "catalog"):
            courses = self.bulk(term, phase)
            if phase == "schedule":
                args = SimpleNamespace(year=year, term=name, subject=None, course=None, allow_removals=False,
                                       allow_empty=True, compress_csv=True)
                entry = catalog.read_manifest(self.args.output)["terms"].get(term_key(term))
                earliest = self.db.execute("SELECT MIN(fetched_at) FROM responses WHERE run_id=? AND path LIKE 'schedule/%'",
                                            (self.client.run_id,)).fetchone()[0]
                if not entry or entry["updatedAt"] <= earliest:
                    catalog.publish(self.args.output, self.db, args, courses, self.client.run_id)
                else:
                    print("Keeping the existing website snapshot, which was updated after this archive collection began.", flush=True)
        elif phase == "sessions":
            self.fetch("schedule/sessions/%s/%s.xml" % (year, name), lambda b: catalog.xml(b, "sessions"), True)
        else:
            module = "schedule" if phase == "finals" else "catalog"
            if module not in self.completed(term):
                raise catalog.CatalogError("%s requires a completed %s phase" % (phase, module))
            courses = self.course_ids(term, module)
            self.save_status(phaseResources=len(courses))
            for index, (subject, number) in enumerate(courses, 1):
                self.save_status(phaseProgress=index)
                if phase == "finals":
                    path = "schedule/%s/%s/%s/%s/finalexam.xml" % (year, name, subject, number)
                    self.fetch(path, lambda b: validate_final(b, subject, number), True)
                else:
                    path = "catalog/%s/%s/%s/%s.xml" % (year, name, subject, number)
                    self.fetch(path, lambda b: validate_course(b, subject, number), True)
                if index % 100 == 0:
                    self.export(term)

    def run(self):
        # Discovery is always refreshed. Term responses remain resumable.
        self.client.run_id = "archive-discovery-" + uuid.uuid4().hex
        discovered = {}
        discovery_resources = []
        discovery_zip = io.BytesIO()
        discovery_bundle = zipfile.ZipFile(discovery_zip, "w", compression=zipfile.ZIP_DEFLATED)
        for module in ("schedule", "catalog"):
            body = self.fetch(module + ".xml?mode=summary", discover_terms)
            discovered[module] = discover_terms(body)
            filename = module + "-summary.xml"
            discovery_bundle.writestr(filename, body)
            discovery_resources.append(dict(file=filename, url=API + module + ".xml?mode=summary",
                sha256=hashlib.sha256(body).hexdigest(), fetchedAt=self.db.execute(
                    "SELECT fetched_at FROM responses WHERE run_id=? AND path=?",
                    (self.client.run_id, module + ".xml?mode=summary")).fetchone()[0]))
        discovery_bundle.writestr("manifest.json", json.dumps(dict(resources=discovery_resources), indent=2))
        discovery_bundle.close()
        public = {(t["year"], t["term"]): t for t in discovered["schedule"] if t["public"] and t["year"] >= self.args.start_year}
        self.terms = list(public.values())
        if self.args.year:
            self.terms = [t for t in self.terms if t["year"] == self.args.year and t["term"] == self.args.term]
        if not self.terms:
            raise catalog.CatalogError("No matching public terms are available")
        # Publish discovery independently from readiness; the site never fabricates
        # unavailable seasons (e.g. Spring 2004) or uses an external catalog.
        manifest = catalog.read_manifest(self.args.output)
        manifest["availableTerms"] = [t for t in discovered["schedule"] if t["public"] and t["year"] >= self.args.start_year]
        catalog.atomic_write(self.args.output / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
        self.args.archive_output.mkdir(parents=True, exist_ok=True)
        catalog.atomic_write(self.args.archive_output / "discovery.zip", discovery_zip.getvalue())
        catalog.atomic_write(self.args.archive_output / "discovery.json", (json.dumps(dict(
            fetchedAt=catalog.now(), modules=discovered), indent=2) + "\n").encode())
        # Current attending/enrolling term first, then the oldest remaining terms.
        def priority(t):
            return (0 if t.get("default") or t.get("enrolling") else 1, t["year"], ORDER[t["term"]])
        self.terms.sort(key=priority)
        for term in self.terms:
            self.run_id(term)
        if self.args.refresh and not self.refresh_applied:
            # Explicit full refresh starts new resource versions; old ZIPs stay
            # available until replaced by successfully collected phases.
            with self.db:
                for term in self.terms:
                    self.db.execute("UPDATE archive_terms SET run_id=?,completed='[]',errors='{}' WHERE term_key=?",
                                    ("archive-" + uuid.uuid4().hex, term_key(term)))
            self.refresh_applied = True
        if "schedule" in self.args.phases:
            for term in self.terms:
                if "schedule" not in self.completed(term) and self.cached_schedule_ready(term):
                    try:
                        self.phase(term, "schedule")
                        self.set_phase(term, "schedule")
                    except (catalog.CatalogError, OSError, ValueError, sqlite3.Error) as exc:
                        if is_storage_error(exc):
                            raise
                        self.set_phase(term, "schedule", exc)
                    self.export(term)
        for phase in self.args.phases:
            for index, term in enumerate(self.terms, 1):
                self.check_stop()
                if phase in self.completed(term):
                    continue
                print("\n%s [%s/%s] %s" % (phase.upper(), index, len(self.terms), term_key(term)), flush=True)
                try:
                    self.phase(term, phase)
                    self.set_phase(term, phase)
                except (catalog.CatalogError, OSError, ValueError, sqlite3.Error) as exc:
                    if is_storage_error(exc):
                        raise
                    self.set_phase(term, phase, exc)
                    print("FAILED %s %s: %s" % (term_key(term), phase, exc), flush=True)
                self.export(term)
                self.save_status()
        incomplete = [term_key(t) for t in self.terms if not all(p in self.completed(t) for p in self.args.phases)]
        self.save_status(state="incomplete" if incomplete else "complete", incompleteTerms=incomplete)
        print("Archive %s. See %s" % (self.status["state"], self.status_file), flush=True)
        return 1 if incomplete else 0


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true", help="Collect every discovered public term")
    selection.add_argument("--year", type=int, help="Collect one year/term")
    parser.add_argument("--term", choices=catalog.TERMS)
    parser.add_argument("--start-year", type=int, default=2004)
    parser.add_argument("--phases", nargs="+", choices=PHASES + OPTIONAL_PHASES, default=list(PHASES))
    parser.add_argument("--refresh", action="store_true", help="Start fresh versions for selected terms")
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--stop", action="store_true", help="Ask the running worker to stop at a safe checkpoint")
    parser.add_argument("--delay", type=float, default=5)
    parser.add_argument("--retries", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--min-free-mb", type=int, default=512, help="Wait when free disk space falls below this threshold")
    parser.add_argument("--cache-dir", type=Path, default=catalog.ROOT / ".catalog-cache")
    parser.add_argument("--output", type=Path, default=catalog.ROOT / "data" / "catalog")
    parser.add_argument("--archive-output", type=Path, default=catalog.ROOT / "data" / "archive")
    args = parser.parse_args(argv)
    if bool(args.year) != bool(args.term):
        parser.error("--year and --term must be supplied together")
    if not (args.all or args.year or args.status or args.stop):
        parser.error("choose --all, --year/--term, --status, or --stop")
    if args.refresh and args.all:
        parser.error("refresh a specific --year/--term; archived history need not be redownloaded")
    if not math.isfinite(args.delay) or args.delay < 2 or args.retries < 0 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("delay must be >=2, retries >=0, timeout >0 (all finite)")
    if args.min_free_mb < 128:
        parser.error("--min-free-mb must be at least 128")
    return args


def main(argv=None):
    args = arguments(argv)
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    status_path = args.cache_dir / "archive-status.json"
    lock = sqlite3.connect(args.cache_dir / "updater-lock.sqlite3", timeout=0)
    try:
        if args.status:
            status = json.loads(status_path.read_text()) if status_path.exists() else {"state": "not started"}
            try:
                lock.execute("BEGIN EXCLUSIVE")
                status["workerRunning"] = False
                if status.get("state") in ("running", "waiting_for_disk", "waiting_for_storage"):
                    status["state"] = "interrupted"
            except sqlite3.OperationalError:
                status["workerRunning"] = True
            print(json.dumps(status, indent=2))
            return 0
        if args.stop:
            catalog.atomic_write(args.cache_dir / "archive.stop", b"stop\n")
            print("Stop requested. The worker will checkpoint and stop; a pending HTTP request can take up to the configured timeout.")
            return 0
        if args.background:
            try:
                lock.execute("BEGIN EXCLUSIVE")
                lock.rollback()
            except sqlite3.OperationalError:
                print("Another updater is already running; use --status.", file=sys.stderr)
                return 1
            raw = list(sys.argv[1:] if argv is None else argv)
            raw.remove("--background")
            # The child owns the same updater lock as foreground collection.
            # start_new_session and file-backed streams let it outlive this terminal.
            with (args.cache_dir / "archive.log").open("ab") as log:
                child = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()), *raw],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True, cwd=str(catalog.ROOT))
            print("Started archive worker PID %s; log: %s" % (child.pid, args.cache_dir / "archive.log"))
            return 0
        try:
            lock.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError:
            print("Another updater is running. Use --status or --stop before starting another.", file=sys.stderr)
            return 1
        (args.cache_dir / "archive.stop").unlink(missing_ok=True)
        db = catalog.open_db(args.cache_dir / "catalog.sqlite3")
        worker = Archive(args, db)
        try:
            return worker.run_with_recovery()
        except KeyboardInterrupt:
            worker.save_status(state="stopped")
            print("Stopped; responses saved. Repeat the command to resume.", flush=True)
            return 130
        except Exception as exc:
            worker.save_status(state="failed", error=str(exc))
            print("Archive failed: %s. Responses saved; repeat the command to resume." % exc, file=sys.stderr)
            return 1
        finally:
            db.close()
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
