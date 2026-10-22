#!/usr/bin/env python3
"""Collect Illinois schedule XML and publish validated static catalog snapshots.

Python 3.9+, standard library only. See docs/UPDATING_COURSE_DATA.md.
"""
import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://courses.illinois.edu/cisapp/explorer/schedule/"
TERMS = {"spring": "sp", "summer": "su", "fall": "fa", "winter": "wi"}
FIELDS = ["Subject", "Number", "Name", "Description", "Credit Hours",
          "Degree Attributes", "CRN", "Section", "Type", "Type Code",
          "Days of Week", "Start Time", "End Time", "Room", "Building",
          "Enrollment Status", "Instructors", "Part of Term"]


class CatalogError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def response_bytes(body):
    """Read both legacy plain XML and compressed checkpoint blobs losslessly."""
    return gzip.decompress(body) if body.startswith(b"\x1f\x8b") else body


def xml(body, root_tag):
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise CatalogError("Invalid XML: %s" % exc) from exc
    # Illinois namespaces only some elements. Normalize all of them.
    for elem in root.iter():
        elem.tag = elem.tag.rsplit("}", 1)[-1]
    if root.tag != root_tag:
        raise CatalogError("Expected <%s>, received <%s>" % (root_tag, root.tag))
    return root


def value(elem, path, default=""):
    child = elem.find(path)
    return " ".join("".join(child.itertext()).split()) if child is not None else default


def subjects_from_xml(body, year, term):
    root = xml(body, "term")
    expected = "1%s%s" % (year, {"spring": "1", "summer": "5", "fall": "8", "winter": "0"}[term])
    # Winter term conventions can differ; validate its calendar year below.
    if term != "winter" and root.get("id") != expected:
        raise CatalogError("API returned a different term: %s" % root.get("id"))
    parent = root.find("parents/calendarYear")
    if parent is None or parent.get("id") != str(year):
        raise CatalogError("API returned a different calendar year")
    container = root.find("subjects")
    if container is None:
        raise CatalogError("Term response has no subject listing")
    subjects = [s.get("id", "") for s in container.findall("subject")]
    if not subjects or len(subjects) != len(set(subjects)):
        raise CatalogError("Empty or duplicate subject listing")
    if any(not re.fullmatch(r"[A-Z]{2,8}", s) for s in subjects):
        raise CatalogError("Unexpected subject identifier")
    return sorted(subjects)


def courses_from_xml(body, subject, year, term):
    root = xml(body, "subject")
    if root.get("id") != subject:
        raise CatalogError("API returned the wrong subject")
    parent = root.find("parents/term")
    if parent is None or value(root, "parents/calendarYear") != str(year):
        raise CatalogError("Subject response is missing its year/term")
    if value(root, "parents/term").lower() != "%s %s" % (term, year):
        raise CatalogError("Subject response is for a different term")
    container = root.find("cascadingCourses")
    if container is None:
        raise CatalogError("Bulk response lacks cascadingCourses; refusing an incomplete import")
    courses = []
    seen = set()
    for elem in container.findall("cascadingCourse"):
        code = elem.get("id", "")
        if not re.fullmatch(re.escape(subject) + r" \d{1,5}", code) or code in seen:
            raise CatalogError("Invalid or duplicate course: %s" % code)
        seen.add(code)
        if not value(elem, "label"):
            raise CatalogError("Course is missing its title: %s" % code)
        # Some legitimate offerings (e.g. CWL 593, Special Studies) omit
        # description entirely. Preserve them without inventing catalog text.
        details = elem.find("detailedSections")
        if details is None:
            raise CatalogError("Course lacks detailedSections: %s" % code)
        attrs = value(elem, "sectionDegreeAttributes")
        if not attrs:
            attrs = "; ".join(value(a, ".") for a in elem.findall(".//genEdAttribute"))
        course = dict(subject=subject, number=code.split()[1], name=value(elem, "label"),
                      desc=value(elem, "description"),
                      credits=re.sub(r"\s+hours?\.?$", "", value(elem, "creditHours")),
                      gened=attrs, sections=[])
        seen_sections = set()
        for section in details.findall("detailedSection"):
            crn = section.get("id", "")
            if not re.fullmatch(r"\d+", crn) or crn in seen_sections:
                raise CatalogError("Invalid or duplicate CRN in %s" % code)
            seen_sections.add(crn)
            # Preserve every meeting, including multiple meetings per CRN.
            # The current website and CSV format use one entry per meeting.
            meetings = section.findall("meetings/meeting") or [ET.Element("meeting")]
            for meeting in meetings:
                kind = meeting.find("type")
                start = value(meeting, "start")
                days = value(meeting, "daysOfTheWeek")
                instructors = list(dict.fromkeys(value(i, ".") for i in
                                   meeting.findall("instructors/instructor") if value(i, ".")))
                course["sections"].append(dict(
                    crn=crn, section=value(section, "sectionNumber"),
                    typeName=value(meeting, "type"), typeCode=kind.get("code", "") if kind is not None else "",
                    days=days, startTime=start, endTime=value(meeting, "end"),
                    isTBA=not start or start == "ARRANGED" or days == "ARRANGED",
                    room=value(meeting, "roomNumber"), building=value(meeting, "buildingName"),
                    status=value(section, "enrollmentStatus", "Unknown"),
                    instructors=instructors, partOfTerm=value(section, "partOfTerm", "1")))
        # A listed course without sections is not an offered course.
        if course["sections"]:
            courses.append(course)
    return courses


def open_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, scope TEXT NOT NULL, started_at TEXT NOT NULL,
          status TEXT NOT NULL, error TEXT);
        CREATE TABLE IF NOT EXISTS responses (
          run_id TEXT NOT NULL, path TEXT NOT NULL, fetched_at TEXT NOT NULL,
          body BLOB NOT NULL, PRIMARY KEY (run_id, path));
        CREATE TABLE IF NOT EXISTS snapshots (
          term_key TEXT PRIMARY KEY, updated_at TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS missing_responses (
          run_id TEXT NOT NULL, path TEXT NOT NULL, fetched_at TEXT NOT NULL,
          status_code INTEGER NOT NULL, body BLOB NOT NULL,
          PRIMARY KEY (run_id, path));
    """)
    return db


class Client:
    def __init__(self, db, run_id, delay=5, retries=6, timeout=90,
                 opener=urllib.request.urlopen, sleep=time.sleep, monotonic=time.monotonic,
                 base=BASE):
        self.db, self.run_id = db, run_id
        self.delay, self.retries, self.timeout = delay, retries, timeout
        self.opener, self.sleep, self.monotonic = opener, sleep, monotonic
        self.next_request = 0
        self.base = base

    def fetch(self, path, validate, allow_missing=False):
        cached = self.db.execute("SELECT body FROM responses WHERE run_id=? AND path=?",
                                 (self.run_id, path)).fetchone()
        if cached:
            body = response_bytes(cached[0])
            validate(body)
            print("  cached %s" % path, flush=True)
            return body
        if allow_missing and self.db.execute(
                "SELECT 1 FROM missing_responses WHERE run_id=? AND path=?", (self.run_id, path)).fetchone():
            print("  cached unavailable %s" % path, flush=True)
            return None
        for attempt in range(self.retries + 1):
            self.sleep(max(0, self.next_request - self.monotonic()))
            try:
                request = urllib.request.Request(self.base + path, headers={
                    "User-Agent": "UIUC-Course-Explorer/1.0 (local catalog updater)",
                    "Accept": "application/xml"})
                print("  GET %s" % path, flush=True)
                with self.opener(request, timeout=self.timeout) as response:
                    body = response.read()
                validate(body)  # Never checkpoint HTML errors or incomplete XML.
                with self.db:
                    self.db.execute("INSERT INTO responses VALUES (?, ?, ?, ?)",
                                    (self.run_id, path, now(), gzip.compress(body, compresslevel=6, mtime=0)))
                return body
            except (urllib.error.URLError, TimeoutError, OSError, CatalogError) as exc:
                code = getattr(exc, "code", None)
                if code == 404 and allow_missing:
                    body = exc.read()
                    with self.db:
                        self.db.execute("INSERT OR REPLACE INTO missing_responses VALUES (?, ?, ?, ?, ?)",
                                        (self.run_id, path, now(), code, gzip.compress(body, compresslevel=6, mtime=0)))
                    print("  unavailable (HTTP 404): %s" % path, flush=True)
                    return None
                if code is not None and code not in (408, 429, 500, 502, 503, 504):
                    raise CatalogError("HTTP %s for %s; check that this term is released" % (code, path)) from exc
                if attempt == self.retries:
                    raise CatalogError("Retries exhausted for %s: %s" % (path, exc)) from exc
                wait = max(self.delay, 30 * 2 ** attempt) + random.uniform(0, 3)
                retry_after = (getattr(exc, "headers", None) or {}).get("Retry-After")
                if retry_after:
                    try:
                        seconds = float(retry_after)
                    except ValueError:
                        try:
                            seconds = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                        except (ValueError, TypeError, OverflowError):
                            seconds = 0
                    wait = max(wait, seconds)
                # Slow later successful requests too after a throttling response.
                if code == 429:
                    self.delay = min(max(self.delay * 2, 10), 120)
                print("  %s; retry %s/%s in %.0fs" % (exc, attempt + 1, self.retries, wait), flush=True)
                self.sleep(wait)
            finally:
                self.next_request = self.monotonic() + self.delay
        raise AssertionError("unreachable")


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("wb") as out:
        out.write(content)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)


def read_manifest(directory):
    path = directory / "manifest.json"
    if not path.exists():
        return {"schemaVersion": 1, "terms": {}}
    data = json.loads(path.read_text())
    if data.get("schemaVersion") != 1 or not isinstance(data.get("terms"), dict):
        raise CatalogError("Unsupported catalog manifest")
    return data


def read_snapshot(directory, entry):
    name = entry["file"]
    if Path(name).name != name:
        raise CatalogError("Invalid snapshot filename")
    body = (directory / name).read_bytes()
    if hashlib.sha256(body).hexdigest() != entry["sha256"]:
        raise CatalogError("Existing snapshot checksum does not match manifest")
    return json.loads(body)


def course_key(course):
    return "%s %s" % (course["subject"], course["number"])


def counts(courses):
    return {"courses": len(courses), "sections": len({
        (c["subject"], c["number"], s["crn"]) for c in courses for s in c["sections"]})}


def merge_courses(previous, incoming, subject=None, number=None, allow_removals=False):
    def in_scope(c):
        return (not subject or c["subject"] == subject) and (not number or c["number"] == number)
    before = {course_key(c): c for c in previous if in_scope(c)}
    after = {course_key(c): c for c in incoming}
    old_counts, new_counts = counts(list(before.values())), counts(incoming)
    for kind in ("courses", "sections"):
        if old_counts[kind] and new_counts[kind] < old_counts[kind] * .85 and not allow_removals:
            raise CatalogError("%s fell from %s to %s in this scope (>15%%). Inspect the API and rerun "
                               "with --resume --allow-removals only if legitimate." %
                               (kind, old_counts[kind], new_counts[kind]))
    report = dict(added=len(after.keys() - before.keys()), removed=len(before.keys() - after.keys()),
                  changed=sum(before[k] != after[k] for k in before.keys() & after.keys()))
    merged = [c for c in previous if not in_scope(c)] + incoming
    return sorted(merged, key=lambda c: (c["subject"], c["number"])), report


def csv_bytes(courses):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, FIELDS, lineterminator="\n")
    writer.writeheader()
    for c in courses:
        for s in c["sections"]:
            writer.writerow(dict(zip(FIELDS, [c["subject"], c["number"], c["name"], c["desc"],
                c["credits"], c["gened"], s["crn"], s["section"], s["typeName"], s["typeCode"],
                s["days"], s["startTime"], s["endTime"], s["room"], s["building"], s["status"],
                "; ".join(s["instructors"]), s["partOfTerm"]])))
    return out.getvalue().encode("utf-8")


def publish(directory, db, args, incoming, run_id):
    manifest = read_manifest(directory)
    key = "%s-%s" % (args.year, TERMS[args.term])
    entry = manifest["terms"].get(key)
    previous = read_snapshot(directory, entry) if entry else None
    if args.subject and previous is None:
        raise CatalogError("A scoped refresh needs an existing full snapshot. Run a full term first, "
                           "or use --no-publish to test collection without changing the site.")
    courses, report = merge_courses(previous["courses"] if previous else [], incoming,
                                     args.subject, args.course, args.allow_removals)
    if not courses and not getattr(args, "allow_empty", False):
        raise CatalogError("Refusing to publish an empty term")
    dates = db.execute("SELECT MIN(fetched_at), MAX(fetched_at) FROM responses WHERE run_id=?", (run_id,)).fetchone()
    refreshed_at = now()
    payload = dict(schemaVersion=1, year=args.year, term=args.term, courses=courses,
                   updatedAt=refreshed_at, fetchStartedAt=dates[0], fetchFinishedAt=dates[1],
                   fullRefreshedAt=previous["fullRefreshedAt"] if args.subject else dates[0])
    body = compact(payload).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    filename = "%s-%s.json" % (key, digest[:16])
    manifest["terms"][key] = dict(file=filename, sha256=digest, updatedAt=refreshed_at,
        fullRefreshedAt=payload["fullRefreshedAt"], fetchStartedAt=dates[0], fetchFinishedAt=dates[1],
        year=args.year, term=args.term, **counts(courses))
    # Write immutable JSON before switching the manifest. Old versions stay usable.
    atomic_write(directory / filename, body)
    csv_body = csv_bytes(courses)
    if getattr(args, "compress_csv", False):
        atomic_write(directory / (key + ".csv.gz"), gzip.compress(csv_body, compresslevel=6, mtime=0))
    if not getattr(args, "compress_csv", False) or (directory / (key + ".csv")).exists():
        atomic_write(directory / (key + ".csv"), csv_body)
    atomic_write(directory / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
    with db:
        db.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?)", (key, refreshed_at, body.decode()))
    print("Published %s: %s; course changes: %s" % (filename, counts(courses), report), flush=True)


def collect(args, db):
    scope = compact(dict(year=args.year, term=args.term, subject=args.subject, course=args.course))
    run = None
    if args.resume:
        run = db.execute("SELECT id, started_at FROM runs WHERE scope=? AND status != 'published' "
                         "ORDER BY rowid DESC LIMIT 1", (scope,)).fetchone()
        if not run:
            raise CatalogError("No unpublished run matches this year, term, and scope. Omit --resume to start one.")
    run_id = run[0] if run else uuid.uuid4().hex
    with db:
        if not run:
            db.execute("INSERT INTO runs VALUES (?, ?, ?, 'running', NULL)", (run_id, scope, now()))
        else:
            db.execute("UPDATE runs SET status='running', error=NULL WHERE id=?", (run_id,))
    print("%s run %s%s" % ("Resuming" if run else "Starting", run_id,
          " (started %s; cached responses retain their original dates)" % run[1] if run else ""), flush=True)
    client = Client(db, run_id, args.delay, args.retries, args.timeout)
    try:
        prefix = "%s/%s" % (args.year, args.term)
        body = client.fetch(prefix + ".xml", lambda b: subjects_from_xml(b, args.year, args.term))
        subjects = subjects_from_xml(body, args.year, args.term)
        if args.subject:
            if args.subject not in subjects:
                raise CatalogError("Subject %s is not listed for this term" % args.subject)
            subjects = [args.subject]
        incoming = []
        for index, subject in enumerate(subjects, 1):
            print("[%s/%s] %s" % (index, len(subjects), subject), flush=True)
            body = client.fetch(prefix + "/%s.xml?mode=cascade" % subject,
                                lambda b: courses_from_xml(b, subject, args.year, args.term))
            courses = courses_from_xml(body, subject, args.year, args.term)
            if args.course:
                courses = [c for c in courses if c["number"] == args.course]
            incoming.extend(courses)
        with db:
            db.execute("UPDATE runs SET status='collected' WHERE id=?", (run_id,))
        if args.no_publish:
            print("Collection validated (%s). Site unchanged. To export these responses, rerun with "
                  "--resume and without --no-publish." % counts(incoming), flush=True)
        else:
            publish(args.output, db, args, incoming, run_id)
            with db:
                db.execute("UPDATE runs SET status='published' WHERE id=?", (run_id,))
    except (Exception, KeyboardInterrupt) as exc:
        with db:
            db.execute("UPDATE runs SET status='interrupted', error=? WHERE id=?", (str(exc), run_id))
        raise


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", required=True, type=int)
    parser.add_argument("--term", required=True, choices=TERMS)
    parser.add_argument("--subject", type=str.upper, help="Refresh one department in an existing full snapshot")
    parser.add_argument("--course", help="Course number; requires --subject")
    parser.add_argument("--resume", action="store_true", help="Reuse validated responses from the last unpublished matching run")
    parser.add_argument("--no-publish", action="store_true", help="Collect and validate only; do not change website data")
    parser.add_argument("--allow-removals", action="store_true", help="Allow a drop larger than 15%% after inspection")
    parser.add_argument("--delay", type=float, default=5, help="Seconds between requests (default 5; minimum 2)")
    parser.add_argument("--retries", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--cache-dir", type=Path, default=ROOT / ".catalog-cache")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "catalog")
    args = parser.parse_args(argv)
    if not 2000 <= args.year <= 2100:
        parser.error("year must be between 2000 and 2100")
    if args.subject and not re.fullmatch(r"[A-Z]{2,8}", args.subject):
        parser.error("invalid subject code")
    if args.course and (not args.subject or not re.fullmatch(r"\d{1,5}", args.course)):
        parser.error("--course needs --subject and a 1–5 digit course number")
    if not math.isfinite(args.delay) or args.delay < 2 or args.retries < 0 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("delay must be >=2, retries >=0, and timeout >0 (all finite)")
    return args


def main(argv=None):
    args = parse_args(argv)
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    # A separate SQLite exclusive transaction locks the updater across processes,
    # including on Windows, without holding the response database transaction open.
    lock = sqlite3.connect(args.cache_dir / "updater-lock.sqlite3", timeout=0)
    db = None
    try:
        try:
            lock.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            raise CatalogError("Another updater is using this cache directory") from exc
        db = open_db(args.cache_dir / "catalog.sqlite3")
        collect(args, db)
        return 0
    except KeyboardInterrupt:
        print("\nStopped. Progress saved; rerun the same command with --resume.", file=sys.stderr)
        return 130
    except (CatalogError, OSError, ValueError, sqlite3.Error) as exc:
        print("Update failed: %s\nProgress saved. Fix the issue and rerun with --resume." % exc, file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.close()
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
