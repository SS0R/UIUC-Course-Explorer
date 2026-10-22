import contextlib
import copy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from scripts import update_catalog as catalog


SUBJECT = (Path(__file__).parent / "fixtures" / "subject.xml").read_bytes()
TERM = b'<term id="120271"><parents><calendarYear id="2027">2027</calendarYear></parents><subjects><subject id="CS">Computer Science</subject></subjects></term>'


class Response(io.BytesIO):
    pass


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = catalog.open_db(self.root / "cache" / "catalog.sqlite3")
        self.courses = catalog.courses_from_xml(SUBJECT, "CS", 2027, "spring")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def args(self, *extra):
        return catalog.parse_args(["--year", "2027", "--term", "spring",
            "--cache-dir", str(self.root / "cache"), "--output", str(self.root / "public"), *extra])

    def run_collect(self, args, bodies=None):
        calls = []

        def opener(request, **_):
            path = request.full_url.removeprefix(catalog.BASE)
            calls.append(path)
            return Response((bodies or {"2027/spring.xml": TERM,
                "2027/spring/CS.xml?mode=cascade": SUBJECT})[path])

        client_class = catalog.Client

        def client(*args):
            return client_class(*args, opener=opener, sleep=lambda _: None)

        with patch.object(catalog, "Client", client), contextlib.redirect_stdout(io.StringIO()):
            catalog.collect(args, self.db)
        return calls

    def snapshot(self):
        directory = self.root / "public"
        entry = catalog.read_manifest(directory)["terms"]["2027-sp"]
        return catalog.read_snapshot(directory, entry)

    def test_descriptions_geneds_instructors_and_every_meeting(self):
        c = self.courses[0]
        self.assertEqual(c["desc"], "Lists, trees & graphs. Prerequisite: CS 128. Same as TEST 225.")
        self.assertEqual(c["credits"], "4")
        self.assertEqual(c["gened"], "Quantitative Reasoning II")
        self.assertEqual(c["sections"][0]["instructors"], ["Beckman, M", "Solomon, B"])
        self.assertEqual(len(c["sections"]), 3)
        self.assertEqual(catalog.counts(self.courses), {"courses": 1, "sections": 2})
        self.assertTrue(c["sections"][-1]["isTBA"])
        self.assertEqual(c["sections"][-1]["status"], "Unknown")
        self.assertEqual(c["sections"][-1]["instructors"], [])
        import csv
        rows = list(csv.DictReader(io.StringIO(catalog.csv_bytes(self.courses).decode())))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["Instructors"], "Beckman, M; Solomon, B")
        self.assertEqual(rows[0]["Description"], c["desc"])

    def test_wrong_term_html_and_shallow_subject_are_rejected(self):
        with self.assertRaises(catalog.CatalogError):
            catalog.subjects_from_xml(TERM, 2026, "spring")
        for body in [b'<html>Slow down</html>', SUBJECT.replace(b"Spring 2027", b"Fall 2027"),
                     SUBJECT.replace(b"cascadingCourses", b"courses"), SUBJECT[:-20]]:
            with self.subTest(body=body[:30]), self.assertRaises(catalog.CatalogError):
                catalog.courses_from_xml(body, "CS", 2027, "spring")

    def test_legitimate_course_without_description_is_preserved(self):
        start = SUBJECT.index(b'<description>')
        end = SUBJECT.index(b'</description>', start) + len(b'</description>')
        course = catalog.courses_from_xml(SUBJECT[:start] + SUBJECT[end:], "CS", 2027, "spring")[0]
        self.assertEqual(course["desc"], "")
        self.assertEqual(course["name"], "Data Structures")
        self.assertEqual(len(course["sections"]), 3)

    def test_retry_after_is_respected_and_success_is_checkpointed(self):
        waits, requests = [], []

        def opener(*args, **kwargs):
            requests.append(args)
            if len(requests) == 1:
                raise urllib.error.HTTPError("test", 429, "throttled", {"Retry-After": "120"}, None)
            return Response(SUBJECT)

        client = catalog.Client(self.db, "run", opener=opener, sleep=waits.append, monotonic=lambda: 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(client.fetch("test", lambda b: catalog.xml(b, "subject")), SUBJECT)
            client.fetch("test", lambda b: catalog.xml(b, "subject"))
        self.assertEqual(len(requests), 2)  # second fetch was served from SQLite
        self.assertIn(120, waits)
        self.assertEqual(client.delay, 10)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM responses").fetchone()[0], 1)

    def test_http_date_retry_after(self):
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=300), usegmt=True)
        attempts, waits = [], []

        def opener(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise urllib.error.HTTPError("test", 503, "busy", {"Retry-After": future}, None)
            return Response(TERM)

        with contextlib.redirect_stdout(io.StringIO()):
            catalog.Client(self.db, "run", opener=opener, sleep=waits.append).fetch(
                "test", lambda b: catalog.xml(b, "term"))
        self.assertTrue(any(295 < wait <= 300 for wait in waits))

    def test_bad_response_is_not_cached_and_404_is_not_retried(self):
        calls = []

        def opener(*args, **kwargs):
            calls.append(1)
            return Response(b'<html>Slow down</html>')

        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(catalog.CatalogError):
            catalog.Client(self.db, "run", retries=1, opener=opener, sleep=lambda _: None).fetch(
                "test", lambda b: catalog.xml(b, "term"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM responses").fetchone()[0], 0)

        def missing(*args, **kwargs):
            raise urllib.error.HTTPError("test", 404, "missing", {}, None)

        with patch.object(catalog.time, "sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(catalog.CatalogError, "HTTP 404"):
                catalog.Client(self.db, "run", opener=missing, sleep=sleep).fetch("test", lambda b: b)
            self.assertEqual(sleep.call_count, 1)  # pacing only; no backoff

    def test_preview_resume_publishes_without_another_request(self):
        self.assertEqual(len(self.run_collect(self.args("--no-publish"))), 2)
        self.assertFalse((self.root / "public" / "manifest.json").exists())
        self.assertEqual(self.run_collect(self.args("--resume")), [])
        self.assertEqual(self.snapshot()["courses"], self.courses)
        entry = catalog.read_manifest(self.root / "public")["terms"]["2027-sp"]
        self.assertEqual(entry["courses"], 1)
        self.assertEqual(entry["sections"], 2)
        first_fetch = self.db.execute("SELECT MIN(fetched_at) FROM responses").fetchone()[0]
        self.assertEqual(entry["fullRefreshedAt"], first_fetch)
        with self.assertRaisesRegex(catalog.CatalogError, "No unpublished run"):
            self.run_collect(self.args("--resume"))

    def test_fresh_run_rechecks_assigned_instructors(self):
        self.run_collect(self.args())
        updated = SUBJECT.replace(b"Solomon, B", b"Newperson, A")
        calls = self.run_collect(self.args(), {"2027/spring.xml": TERM,
            "2027/spring/CS.xml?mode=cascade": updated})
        self.assertEqual(len(calls), 2)
        self.assertIn("Newperson, A", self.snapshot()["courses"][0]["sections"][0]["instructors"])

    def test_failed_department_keeps_snapshot_and_resumes_saved_term(self):
        self.run_collect(self.args())
        original = (self.root / "public" / "manifest.json").read_bytes()
        with self.assertRaises(catalog.CatalogError):
            self.run_collect(self.args(), {"2027/spring.xml": TERM,
                "2027/spring/CS.xml?mode=cascade": b'<html>Service unavailable</html>'})
        self.assertEqual((self.root / "public" / "manifest.json").read_bytes(), original)
        self.assertEqual(self.run_collect(self.args("--resume")), ["2027/spring/CS.xml?mode=cascade"])

    def test_scoped_refresh_merges_and_protects_against_large_deletions(self):
        other = copy.deepcopy(self.courses[0])
        other["subject"], other["number"] = "MATH", "241"
        newer = copy.deepcopy(self.courses[0])
        newer["sections"][0]["instructors"] = ["Newperson, A"]
        merged, report = catalog.merge_courses([self.courses[0], other], [newer], "CS", "225")
        self.assertEqual(merged[-1], other)
        self.assertEqual(report, {"added": 0, "removed": 0, "changed": 1})
        with self.assertRaises(catalog.CatalogError):
            catalog.merge_courses(self.courses, [], "CS", "225")
        smaller = copy.deepcopy(newer)
        smaller["sections"] = smaller["sections"][:1]
        with self.assertRaisesRegex(catalog.CatalogError, "sections fell"):
            catalog.merge_courses(self.courses, [smaller])
        merged, _ = catalog.merge_courses([self.courses[0], other], [], "CS", "225", True)
        self.assertEqual(merged, [other])

    def test_scoped_initial_run_cannot_publish_incomplete_semester(self):
        with self.assertRaisesRegex(catalog.CatalogError, "existing full snapshot"):
            self.run_collect(self.args("--subject", "CS"))
        self.assertFalse((self.root / "public" / "manifest.json").exists())

    def test_scoped_refresh_from_fresh_clone_preserves_full_date_and_other_courses(self):
        self.run_collect(self.args())
        full_date = self.snapshot()["fullRefreshedAt"]
        # The published JSON, rather than local DB history, is the merge baseline.
        with self.db:
            self.db.execute("DELETE FROM snapshots")
        updated = SUBJECT.replace(b"Solomon, B", b"Newperson, A")
        self.run_collect(self.args("--subject", "CS", "--course", "225"), {
            "2027/spring.xml": TERM, "2027/spring/CS.xml?mode=cascade": updated})
        self.assertEqual(self.snapshot()["fullRefreshedAt"], full_date)
        self.assertIn("Newperson, A", self.snapshot()["courses"][0]["sections"][0]["instructors"])

    def test_failed_manifest_write_preserves_last_usable_snapshot(self):
        self.run_collect(self.args())
        original_manifest = (self.root / "public" / "manifest.json").read_bytes()
        original_snapshot = self.snapshot()
        write = catalog.atomic_write

        def fail_manifest(path, body):
            if path.name == "manifest.json":
                raise OSError("disk write failure")
            write(path, body)

        with patch.object(catalog, "atomic_write", fail_manifest), self.assertRaises(OSError):
            self.run_collect(self.args())
        self.assertEqual((self.root / "public" / "manifest.json").read_bytes(), original_manifest)
        self.assertEqual(self.snapshot(), original_snapshot)

    def test_exclusive_lock_blocks_second_updater(self):
        self.root.joinpath("cache").mkdir(exist_ok=True)
        lock = sqlite3.connect(self.root / "cache" / "updater-lock.sqlite3")
        lock.execute("BEGIN EXCLUSIVE")
        try:
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                result = catalog.main(["--year", "2027", "--term", "spring", "--cache-dir",
                    str(self.root / "cache"), "--output", str(self.root / "public")])
            self.assertEqual(result, 1)
            self.assertIn("Another updater", errors.getvalue())
        finally:
            lock.close()


if __name__ == "__main__":
    unittest.main()
