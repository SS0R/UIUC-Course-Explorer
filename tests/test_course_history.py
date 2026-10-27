import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.course_history import prepare_history, prepare_indexes


class CourseHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.manifest = {'schemaVersion':1,'terms':{},'availableTerms':[]}

    def tearDown(self):
        self.temp.cleanup()

    def add(self, year, term, codes, publish=True):
        code = {'fall':'fa','spring':'sp','summer':'su','winter':'wi'}[term]
        key = '%s-%s' % (year,code)
        self.manifest['availableTerms'].append(dict(year=year,term=term))
        if publish:
            courses = [dict(subject=s,number=n,name='Title',sections=[dict(crn='12345')]) for s,n in codes]
            body = json.dumps(dict(schemaVersion=1,year=year,term=term,courses=courses)).encode()
            filename = key+'.json'
            (self.directory/filename).write_bytes(body)
            self.manifest['terms'][key] = dict(file=filename,sha256=hashlib.sha256(body).hexdigest(),year=year,term=term)
        return key

    def history(self):
        filename, body, entry = prepare_history(self.directory,self.manifest)
        self.assertEqual(hashlib.sha256(body).hexdigest(),entry['sha256'])
        return json.loads(body)

    def test_new_offerings_and_returning_courses_across_all_earlier_semesters(self):
        self.add(2004,'fall',[('CS','100'),('MATH','200')])
        self.add(2005,'spring',[('CS','101'),('MATH','200')])
        self.add(2005,'summer',[('CS','100'),('CS','102')])
        h = self.history()
        self.assertEqual(h['firstOffered']['CS 100'],'2004-fa')
        self.assertEqual(h['firstOffered']['CS 101'],'2005-sp')
        self.assertEqual(h['firstOffered']['CS 102'],'2005-su')
        self.assertNotIn('2004-fa',h['eligibleTerms'])
        self.assertEqual(h['eligibleTerms'],['2005-sp','2005-su'])

    def test_missing_prior_term_disables_later_new_course_claims(self):
        self.add(2004,'fall',[('CS','100')])
        self.add(2005,'spring',[('CS','101')],publish=False)
        self.add(2005,'summer',[('CS','102')])
        self.assertEqual(self.history()['eligibleTerms'],[])

    def test_missing_baseline_or_discovery_does_not_mark_every_course_new(self):
        self.add(2027,'spring',[('CS','100')])
        self.assertEqual(self.history()['eligibleTerms'],[])
        self.add(2004,'fall',[('CS','100')])
        self.manifest.pop('availableTerms')
        self.assertEqual(self.history()['eligibleTerms'],[])

    def test_terms_are_chronological_including_winter_and_renamed_courses(self):
        self.add(2015,'spring',[('CS','300')])
        self.add(2014,'fall',[('CS','200')])
        self.add(2004,'fall',[('CS','100')])
        self.add(2015,'winter',[('CS','300')])
        h = self.history()
        self.assertEqual(h['firstOffered']['CS 300'],'2015-wi')
        self.assertEqual(h['eligibleTerms'],['2014-fa','2015-wi','2015-sp'])

    def test_unverified_snapshot_is_rejected(self):
        self.add(2004,'fall',[('CS','100')])
        (self.directory/'2004-fa.json').write_text('corrupt')
        with self.assertRaisesRegex(ValueError,'checksum mismatch'):
            self.history()

    def test_unreleased_term_needs_current_discovery_before_classifying_new(self):
        self.add(2004,'fall',[('CS','100')])
        self.add(2005,'spring',[('CS','101')])
        self.manifest['availableTerms'].pop()
        self.assertEqual(self.history()['eligibleTerms'],[])

    def test_history_output_is_deterministic(self):
        self.add(2004,'fall',[('CS','100')])
        self.add(2005,'spring',[('CS','101')])
        self.assertEqual(prepare_history(self.directory,self.manifest),prepare_history(self.directory,self.manifest))

    def test_offered_semesters_are_derived_locally_in_chronological_order(self):
        self.add(2005,'summer',[('CS','100')])
        self.add(2004,'fall',[('CS','100')])
        self.add(2005,'spring',[('CS','101')])
        indexes=prepare_indexes(self.directory,self.manifest)
        history=json.loads(indexes['courseHistory'][1])
        offerings=json.loads(indexes['courseOfferings'][1])
        self.assertTrue(offerings['coverageComplete'])
        self.assertEqual(offerings['offeredTerms']['CS 100'],['2004-fa','2005-su'])
        self.assertEqual(offerings['offeredTerms']['CS 101'],['2005-sp'])
        self.assertEqual(history['firstOffered']['CS 100'],offerings['offeredTerms']['CS 100'][0])
        self.manifest['availableTerms'].append(dict(year=2005,term='fall'))
        self.assertFalse(json.loads(prepare_indexes(self.directory,self.manifest)['courseOfferings'][1])['coverageComplete'])


if __name__ == '__main__':
    unittest.main()
