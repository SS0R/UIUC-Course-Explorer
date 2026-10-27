import contextlib
import errno
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

from scripts import archive_catalog as archive
from scripts import update_catalog as catalog
from scripts.prune_finals import remove_final_records


SUBJECT = (Path(__file__).parent / 'fixtures' / 'subject.xml').read_bytes()
DISCOVERY = b'''<schedule><calendarYears>
<calendarYearSummary id="2004"><terms><termDetail id="120048"><label>Fall 2004</label><publicIndicator>Y</publicIndicator><archiveIndicator>Y</archiveIndicator></termDetail></terms></calendarYearSummary>
<calendarYearSummary id="2027"><terms>
<termDetail id="120271"><label>Spring 2027</label><publicIndicator>Y</publicIndicator><archiveIndicator>N</archiveIndicator></termDetail>
<termDetail id="120275"><label>Summer 2027</label><publicIndicator>N</publicIndicator><archiveIndicator>N</archiveIndicator></termDetail>
</terms></calendarYearSummary></calendarYears></schedule>'''
TERM = b'<term id="120271"><parents><calendarYear id="2027">2027</calendarYear></parents><label>Spring 2027</label><subjects><subject id="CS">Computer Science</subject></subjects></term>'
FINAL = b'<finalExam id="120271 CS 225"><subjectCode>CS</subjectCode><courseNumber>225</courseNumber><finalExamEvent><date>2027-05-10</date><location>Exam hall</location><futureField>Keep me</futureField></finalExamEvent></finalExam>'
DETAIL = b'<course id="CS 225"><label>Data Structures</label><termsOffered><course id="225">Fall 2004</course></termsOffered><futureField keep="yes">Unmodeled text</futureField></course>'


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.args = archive.arguments(['--year','2027','--term','spring', '--retries','0',
            '--cache-dir',str(self.root/'cache'), '--output',str(self.root/'site'),
            '--archive-output',str(self.root/'archive'), '--phases', *archive.PHASES, *archive.OPTIONAL_PHASES])
        self.db = catalog.open_db(self.args.cache_dir/'catalog.sqlite3')
        self.responses = {
            'schedule.xml?mode=summary': DISCOVERY,
            'catalog.xml?mode=summary': DISCOVERY,
            'schedule/2027/spring.xml': TERM,
            'catalog/2027/spring.xml': TERM.replace(b'<label>Spring 2027</label>',b'<label>2027</label>'),
            'schedule/2027/spring/CS.xml?mode=cascade': SUBJECT,
            'catalog/2027/spring/CS.xml?mode=cascade': SUBJECT,
            'schedule/sessions/2027/spring.xml': b'<sessions><session><partOfTerm id="1">Full term</partOfTerm></session></sessions>',
            'schedule/2027/spring/CS/225/finalexam.xml': FINAL,
            'catalog/2027/spring/CS/225.xml': DETAIL,
        }
        self.requests = []

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def run_worker(self, recover=False):
        original = catalog.Client

        def opener(request, **kwargs):
            path = request.full_url.removeprefix(archive.API)
            self.requests.append(path)
            body = self.responses[path]
            if isinstance(body, Exception):
                raise body
            return io.BytesIO(body)

        def client(*args, **kwargs):
            kwargs.update(opener=opener, sleep=lambda _: None)
            return original(*args, **kwargs)

        with patch.object(catalog, 'Client', client), contextlib.redirect_stdout(io.StringIO()):
            worker = archive.Archive(self.args, self.db)
            return worker.run_with_recovery() if recover else worker.run()

    def test_discover_only_available_terms_and_preserve_flags(self):
        terms = archive.discover_terms(DISCOVERY)
        self.assertEqual([(t['year'],t['term']) for t in terms if t['public']], [(2004,'fall'),(2027,'spring')])
        self.assertFalse(terms[-1]['public'])
        with self.assertRaises(catalog.CatalogError):
            archive.discover_terms(b'<html>error</html>')

    def test_default_collection_skips_finals_and_individual_catalog_details(self):
        args = archive.arguments(['--all'])
        self.assertEqual(args.phases,['schedule','catalog','sessions'])
        self.args.phases = args.phases
        self.assertEqual(self.run_worker(),0)
        self.assertFalse(any('/finalexam.xml' in p or p=='catalog/2027/spring/CS/225.xml' for p in self.requests))

    def test_prune_removes_finals_but_preserves_other_responses_and_archives(self):
        self.run_worker()
        before = {path:catalog.response_bytes(body) for path,body in self.db.execute('SELECT path,body FROM responses')
                  if '/finalexam.xml' not in path}
        with self.db:
            self.db.execute('INSERT INTO missing_responses VALUES (?,?,?,?,?)',
                (self.db.execute('SELECT run_id FROM archive_terms').fetchone()[0],
                 'schedule/2027/spring/CS/999/finalexam.xml','2026-10-27',404,b'not found'))
        removed = remove_final_records(self.db)
        self.assertEqual(removed,{'responses':1,'missing_responses':1})
        after = {path:catalog.response_bytes(body) for path,body in self.db.execute('SELECT path,body FROM responses')}
        self.assertEqual(after,before)
        worker = archive.Archive(self.args,self.db)
        worker.export(dict(year=2027,term='spring'))
        with zipfile.ZipFile(self.args.archive_output/'2027-sp.zip') as bundle:
            self.assertFalse(any('/finalexam.xml' in name for name in bundle.namelist()))
            metadata=json.loads(bundle.read('manifest.json'))
            self.assertTrue(metadata['complete'])
            self.assertNotIn('finals',metadata['completedPhases'])
            self.assertEqual(bundle.read('responses/catalog/2027/spring/CS/225.xml'),DETAIL)
        self.assertEqual(remove_final_records(self.db),{'responses':0,'missing_responses':0})
        self.args.phases=list(archive.PHASES)
        initial=len(self.requests)
        self.assertEqual(self.run_worker(),0)
        self.assertEqual(self.requests[initial:],['schedule.xml?mode=summary','catalog.xml?mode=summary'])

    def test_catalog_year_label_is_accepted_only_with_correct_term_identity(self):
        body = self.responses['catalog/2027/spring.xml']
        self.assertEqual(archive.subject_ids(body,2027,'spring'),['CS'])
        with self.assertRaisesRegex(catalog.CatalogError,'Wrong term ID'):
            archive.subject_ids(body.replace(b'id="120271"',b'id="120278"'),2027,'spring')
        with self.assertRaisesRegex(catalog.CatalogError,'Wrong year'):
            archive.subject_ids(body,2026,'spring')

    def test_archive_is_lossless_including_finals_and_unmodeled_fields(self):
        self.assertEqual(self.run_worker(), 0)
        with zipfile.ZipFile(self.args.archive_output/'2027-sp.zip') as bundle:
            metadata = json.loads(bundle.read('manifest.json'))
            self.assertTrue(metadata['complete'])
            self.assertEqual(set(metadata['completedPhases']), set(self.args.phases))
            self.assertEqual(metadata['requiredPhases'],list(archive.PHASES))
            for record in metadata['resources']:
                body = bundle.read(record['file'])
                self.assertEqual(body, self.responses[record['path']])
                self.assertEqual(hashlib.sha256(body).hexdigest(),record['sha256'])
            self.assertEqual(bundle.read('responses/schedule/2027/spring/CS/225/finalexam.xml'),FINAL)
            self.assertEqual(bundle.read('responses/catalog/2027/spring/CS/225.xml'),DETAIL)
        manifest = catalog.read_manifest(self.args.output)
        self.assertIn('2027-sp', manifest['terms'])
        self.assertEqual(len(manifest['availableTerms']), 2)
        self.assertTrue((self.args.output/'2027-sp.csv.gz').exists())
        self.assertFalse((self.args.output/'2027-sp.csv').exists())
        body = self.db.execute("SELECT body FROM responses WHERE path='schedule/2027/spring/CS.xml?mode=cascade'").fetchone()[0]
        self.assertTrue(body.startswith(b'\x1f\x8b'))
        self.assertEqual(catalog.response_bytes(body), SUBJECT)
        with zipfile.ZipFile(self.args.archive_output/'discovery.zip') as bundle:
            self.assertEqual(bundle.read('schedule-summary.xml'),DISCOVERY)
            self.assertEqual(bundle.read('catalog-summary.xml'),DISCOVERY)

    def test_rerun_only_discovers_again_without_repeating_completed_resources(self):
        self.run_worker()
        initial = len(self.requests)
        self.assertEqual(self.run_worker(), 0)
        self.assertEqual(self.requests[initial:], ['schedule.xml?mode=summary','catalog.xml?mode=summary'])

    def test_committed_zip_restores_cache_without_downloading_the_term_again(self):
        self.run_worker()
        with self.db:
            self.db.execute('DELETE FROM archive_terms')
            self.db.execute('DELETE FROM responses')
            self.db.execute('DELETE FROM missing_responses')
        initial = len(self.requests)
        self.assertEqual(self.run_worker(),0)
        self.assertEqual(self.requests[initial:], ['schedule.xml?mode=summary','catalog.xml?mode=summary'])
        run_id = self.db.execute('SELECT run_id FROM archive_terms').fetchone()[0]
        body = self.db.execute('SELECT body FROM responses WHERE run_id=? AND path=?',
            (run_id,'schedule/2027/spring/CS/225/finalexam.xml')).fetchone()[0]
        self.assertEqual(catalog.response_bytes(body),FINAL)

    def test_corrupt_zip_is_rejected_without_overwriting_it(self):
        self.run_worker()
        with self.db:
            self.db.execute('DELETE FROM archive_terms')
        path = self.args.archive_output/'2027-sp.zip'
        body = path.read_bytes() + b'corrupt'
        path.write_bytes(body)
        with self.assertRaisesRegex(catalog.CatalogError,'checksum mismatch'):
            self.run_worker()
        self.assertEqual(path.read_bytes(),body)

    def test_resumed_old_collection_does_not_overwrite_newer_website_data(self):
        self.args.phases = ['schedule']
        self.run_worker()
        manifest = catalog.read_manifest(self.args.output)
        entry = manifest['terms']['2027-sp']
        newer = catalog.read_snapshot(self.args.output,entry)
        newer['courses'][0]['name'] = 'Updated after collection began'
        body = catalog.compact(newer).encode()
        digest = hashlib.sha256(body).hexdigest()
        entry.update(file='2027-sp-'+digest[:16]+'.json', sha256=digest, updatedAt='2099-01-01T00:00:00+00:00')
        catalog.atomic_write(self.args.output/entry['file'],body)
        catalog.atomic_write(self.args.output/'manifest.json',json.dumps(manifest).encode())
        with self.db:
            self.db.execute("UPDATE archive_terms SET completed='[]'")
        self.assertEqual(self.run_worker(),0)
        restored = catalog.read_snapshot(self.args.output,catalog.read_manifest(self.args.output)['terms']['2027-sp'])
        self.assertEqual(restored['courses'][0]['name'],'Updated after collection began')

    def test_unavailable_final_is_recorded_and_distinguished_from_empty_success(self):
        path = 'schedule/2027/spring/CS/225/finalexam.xml'
        self.responses[path] = urllib.error.HTTPError('test',404,'not public',{},io.BytesIO(b'<errorMessage>Not found</errorMessage>'))
        self.assertEqual(self.run_worker(), 0)
        with zipfile.ZipFile(self.args.archive_output/'2027-sp.zip') as bundle:
            metadata = json.loads(bundle.read('manifest.json'))
            record = next(r for r in metadata['resources'] if r['path'] == path)
            self.assertEqual(record['status'],404)
            self.assertEqual(bundle.read(record['file']), b'<errorMessage>Not found</errorMessage>')
        self.assertEqual(self.run_worker(), 0)
        self.assertEqual(self.requests.count(path),1)

    def test_failed_schedule_does_not_publish_partial_term_and_resume_uses_saved_term(self):
        self.args.phases = ['schedule']
        path = 'schedule/2027/spring/CS.xml?mode=cascade'
        self.responses[path] = b'<html>temporary outage</html>'
        self.assertEqual(self.run_worker(), 1)
        self.assertFalse(catalog.read_manifest(self.args.output)['terms'])
        self.responses[path] = SUBJECT
        self.assertEqual(self.run_worker(), 0)
        self.assertEqual(self.requests.count('schedule/2027/spring.xml'),1)
        self.assertIn('2027-sp',catalog.read_manifest(self.args.output)['terms'])

    def test_disk_floor_and_stop_file_prevent_another_request(self):
        worker = archive.Archive(self.args,self.db)
        worker.stop_file.write_text('stop')
        with self.assertRaises(KeyboardInterrupt):
            worker.fetch('schedule.xml',lambda b:b)
        worker.stop_file.unlink()
        def stop_during_wait(seconds):
            self.assertEqual(worker.status['state'],'waiting_for_disk')
            worker.stop_file.write_text('stop')
            worker.check_stop()
        with patch.object(archive.shutil,'disk_usage',return_value=type('Disk',(),{'free':0})()), \
             patch.object(worker,'sleep',side_effect=stop_during_wait), \
             patch.object(worker.client,'fetch') as fetch, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                worker.fetch('schedule.xml',lambda b:b)
            fetch.assert_not_called()
        self.assertFalse(self.requests)

    def test_low_disk_space_waits_and_automatically_resumes_without_fetching_early(self):
        worker = archive.Archive(self.args,self.db)
        free = [0]
        def disk_usage(path):
            return type('Disk',(),{'free':free[0]})()
        def recover(seconds):
            self.assertEqual(seconds,30)
            self.assertEqual(worker.status['state'],'waiting_for_disk')
            fetch.assert_not_called()
            free[0] = 2 * 1024**3
        with patch.object(archive.shutil,'disk_usage',side_effect=disk_usage), \
             patch.object(worker,'sleep',side_effect=recover) as sleep, \
             patch.object(worker.client,'fetch',return_value=b'complete') as fetch, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(worker.fetch('schedule.xml',lambda b:b),b'complete')
            sleep.assert_called_once_with(30)
            fetch.assert_called_once()
        self.assertEqual(worker.status['state'],'running')

    def test_status_disk_full_does_not_crash_waiting_and_recovers(self):
        worker = archive.Archive(self.args,self.db)
        free = [0]
        write = catalog.atomic_write
        def status_write(path,body):
            if free[0] == 0:
                raise OSError(errno.ENOSPC,'No space left on device')
            write(path,body)
        def recover(seconds):
            self.assertEqual(worker.status['state'],'waiting_for_disk')
            fetch.assert_not_called()
            free[0] = 2 * 1024**3
        with patch.object(catalog,'atomic_write',side_effect=status_write), \
             patch.object(archive.shutil,'disk_usage',side_effect=lambda p:type('Disk',(),{'free':free[0]})()), \
             patch.object(worker,'sleep',side_effect=recover), \
             patch.object(worker.client,'fetch',return_value=b'complete') as fetch, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(worker.fetch('schedule.xml',lambda b:b),b'complete')
        self.assertEqual(json.loads(worker.status_file.read_text())['state'],'running')
        with patch.object(catalog,'atomic_write',side_effect=OSError(errno.EACCES,'Permission denied')):
            with self.assertRaises(OSError):
                worker.save_status()

    def test_storage_error_recovers_without_redownloading_or_resetting_refresh(self):
        self.args.refresh = True
        phase = archive.Archive.phase
        attempts = []
        def fail_after_saved_responses(worker,term,kind):
            if kind == 'schedule':
                attempts.append(worker.run_id(term))
                phase(worker,term,kind)
                if len(attempts) == 1:
                    raise archive.sqlite3.OperationalError('unable to open database file')
            else:
                phase(worker,term,kind)
        def waiting(worker,seconds):
            self.assertEqual(seconds,30)
            self.assertEqual(worker.status['state'],'waiting_for_storage')
        with patch.object(archive.Archive,'phase',fail_after_saved_responses), \
             patch.object(archive.Archive,'sleep',waiting):
            self.assertEqual(self.run_worker(recover=True),0)
        self.assertEqual(len(attempts),2)
        self.assertEqual(attempts[0],attempts[1])
        self.assertEqual(self.requests.count('schedule/2027/spring.xml'),1)
        self.assertEqual(self.requests.count('schedule/2027/spring/CS.xml?mode=cascade'),1)
        self.assertTrue(json.loads((self.args.cache_dir/'archive-status.json').read_text())['state']=='complete')

    def test_status_survives_temporarily_unreadable_database(self):
        worker = archive.Archive(self.args,self.db)
        with patch.object(worker,'refresh_statistics',side_effect=archive.sqlite3.OperationalError('unable to open database file')):
            worker.save_status(state='waiting_for_storage')
        status=json.loads(worker.status_file.read_text())
        self.assertEqual(status['state'],'waiting_for_storage')
        self.assertEqual(status['storageError'],'unable to open database file')

    def test_corruption_and_permissions_are_not_treated_as_transient_storage(self):
        worker = archive.Archive(self.args,self.db)
        for error in [archive.sqlite3.DatabaseError('database disk image is malformed'),
                      OSError(errno.EACCES,'Permission denied')]:
            with patch.object(worker,'run',side_effect=error), patch.object(worker,'sleep') as sleep:
                with self.assertRaises(type(error)):
                    worker.run_with_recovery()
                sleep.assert_not_called()

    def test_missing_resource_checkpoint_is_reused_and_legacy_xml_is_readable(self):
        with self.db:
            self.db.execute('INSERT INTO responses VALUES (?,?,?,?)',('old','test','2026-01-01',SUBJECT))
        with contextlib.redirect_stdout(io.StringIO()):
            result = catalog.Client(self.db,'old',opener=lambda *a,**k:self.fail('unexpected network')).fetch('test',lambda b:catalog.xml(b,'subject'))
        self.assertEqual(result,SUBJECT)


if __name__ == '__main__':
    unittest.main()
