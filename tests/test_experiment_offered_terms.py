"""Exercise offered-term history and navigation in the experimental UI."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
JSC = Path('/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc')
ENGINE = shutil.which('node') or (str(JSC) if JSC.exists() else None)


@unittest.skipUnless(ENGINE, 'A JavaScript engine is required')
class OfferedTermsTests(unittest.TestCase):
    def run_checks(self, checks):
        html = (ROOT / 'index.html').read_text()
        start = html.index('  // OFFERED TERMS')
        end = html.index('  const LECTURE_TYPES', start)
        helpers = html[start:end]
        harness = r'''
const log = typeof print === 'function' ? print : globalThis.console.log;
function assert(ok, message) { if (!ok) throw new Error(message); }
function escapeHtml(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
class URL { constructor(path, base) { this.href = String(base) + path; } }
const CATALOG_BASE = 'https://example.test/data/catalog/';
const elements = {};
const document = {getElementById(id) {
  return elements[id] || (elements[id] = {innerHTML:'',textContent:'',hidden:true,
    classList:{values:new Set(),add(value){this.values.add(value);},remove(value){this.values.delete(value);},contains(value){return this.values.has(value);}},
    setAttribute(name,value){this[name]=value;},scrollIntoView(){}});
}};
const yearSelect = {value:'2027'}, semSelect = {value:'sp'};
let courseMap = {'CS 225':{}};
let activePanelCourse = 'CS 225';
let catalogManifest = {courseOfferings:{file:'offerings.json'},terms:{
  '2027-sp':{file:'new.json',sha256:'new'},
  '2026-fa':{file:'fall.json',sha256:'fall'},
  '2026-su':{file:'summer.json',sha256:'summer'},
  '2026-sp':{file:'spring.json',sha256:'spring'},
  '2026-wi':{file:'winter.json',sha256:'winter'}
}};
const history = {schemaVersion:1,coverageStart:'2004-fa',offeredTerms:{
  'CS 225':['2026-fa','2027-sp','2026-sp','2026-su','2026-wi','2026-fa']
},sourceSnapshots:{'2027-sp':'new','2026-fa':'fall','2026-su':'summer','2026-sp':'spring','2026-wi':'winter'}};
let requests=0;
async function fetch() {requests++;return {ok:true,json:async()=>history};}
function closePrereqTree(){}
function syncCustomSelect(){}
let opened=[];
function openPanel(key){opened.push(key);activePanelCourse=key;}
function setStatus(message){elements.error=message;}
let onSemesterChange;
'''
        source = helpers + harness + '\nasync function run() {\n' + checks + r'''
}
run().then(() => log('Offered-term checks passed')).catch(error => log(error.stack || String(error)));
'''
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / 'offered-terms.js'
            script.write_text(source)
            result = subprocess.run([ENGINE, str(script)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Offered-term checks passed', result.stdout)

    def test_terms_sorted_deduplicated_and_verified(self):
        self.run_checks(r'''
assert(offeredTermLabel('2026-fa')==='Fall 2026', 'Term labels');
assert(offeredTermsForCourse('CS 225',history).join(',')==='2027-sp,2026-fa,2026-su,2026-sp,2026-wi', 'Newest-first ordering and no duplicates');
history.offeredTerms['CS 225'].push('2099-fa','garbage');
history.sourceSnapshots['2026-su']='stale';
const terms=offeredTermsForCourse('CS 225',history);
assert(!terms.includes('2099-fa')&&!terms.includes('garbage')&&!terms.includes('2026-su'), 'Unavailable, malformed and stale terms excluded');
history.offeredTerms['CS 225']=[];
assert(offeredTermsForCourse('CS 225',history).join(',')==='2027-sp', 'Current snapshot is authoritative even when index lags');
''')

    def test_history_lazy_loaded_and_shared(self):
        self.run_checks(r'''
assert(requests===0, 'No history download just to open a course');
await Promise.all([getCourseOfferings(),getCourseOfferings()]);
await getCourseOfferings();
assert(requests===1, 'History download cached across requests and courses');
''')

    def test_display_toggle_and_current_term(self):
        self.run_checks(r'''
await toggleOfferedTerms();
assert(!elements['panel-offered-terms'].hidden, 'Button opens history');
assert(elements['detail-panel'].classList.contains('show-offered-terms'), 'Offered terms replaces course details');
assert(elements['panel-offered-btn']['aria-expanded']==='true', 'Expanded state exposed');
const list=elements['offered-terms-list'].innerHTML;
assert(list.includes('Fall 2026') && list.includes("openCourseInTerm('2026-fa')"), 'Terms navigate on click');
assert(list.includes('aria-current="true"') && list.includes('Current'), 'Current term identified');
assert(elements['offered-terms-status'].textContent.includes('Fall 2004'), 'History coverage stated');
await toggleOfferedTerms();
assert(elements['panel-offered-terms'].hidden && elements['panel-offered-btn']['aria-expanded']==='false', 'Second click collapses');
assert(!elements['detail-panel'].classList.contains('show-offered-terms'), 'Closing history restores course details');
''')

    def test_failure_retry_and_stale_response(self):
        self.run_checks(r'''
fetch=async()=>{requests++;return {ok:false,status:503};};
await loadOfferedTerms();
assert(elements['offered-terms-list'].innerHTML.includes('Try again'), 'Failure offers retry');
fetch=async()=>{requests++;return {ok:true,json:async()=>history};};
await loadOfferedTerms();
assert(requests===2 && elements['offered-terms-list'].innerHTML.includes('Fall 2026'), 'Failed history request can recover');
courseOfferingsPromise=null;
let finish;
fetch=()=>new Promise(resolve=>{finish=resolve;});
const pending=loadOfferedTerms();
resetOfferedTerms();
finish({ok:true,json:async()=>history});
await pending;
assert(elements['offered-terms-list'].innerHTML==='' && elements['offered-terms-status'].textContent==='', 'Closing panel discards stale response');
''')

    def test_navigation_loads_target_then_opens_course(self):
        self.run_checks(r'''
onSemesterChange=async()=>{
  assert(yearSelect.value==='2026' && semSelect.value==='fa', 'Catalog loader receives selected term');
  courseTermNavigation++;activePanelCourse=null;courseMap={'CS 225':{name:'Historical title'}};
  return true;
};
assert(await openCourseInTerm('2026-fa'), 'Term navigation succeeds');
assert(opened.join(',')==='CS 225' && courseMap['CS 225'].name==='Historical title', 'Reopens original course using historical snapshot');
let loads=0;onSemesterChange=async()=>{loads++;return true;};
assert(await openCourseInTerm('2026-fa') && loads===0, 'Current term does not reload catalog');
assert(!(await openCourseInTerm('2099-fa')) && loads===0, 'Unavailable terms cannot trigger loads');
''')

    def test_failed_and_superseded_navigation_does_not_reopen(self):
        self.run_checks(r'''
onSemesterChange=async()=>{activePanelCourse=null;courseTermNavigation++;return false;};
assert(!(await openCourseInTerm('2026-fa')) && opened.length===0, 'Failed catalog load cannot reopen stale course');
activePanelCourse='CS 225';yearSelect.value='2027';semSelect.value='sp';
let finish;
onSemesterChange=()=>{activePanelCourse=null;courseTermNavigation++;return new Promise(resolve=>{finish=resolve;});};
const pending=openCourseInTerm('2026-fa');
courseTermNavigation++; // User navigates elsewhere while the snapshot is loading.
finish(true);
assert(!(await pending) && opened.length===0, 'Superseded request cannot reopen a course');
''')

    def test_committed_offerings_reference_available_snapshots(self):
        manifest = json.loads((ROOT / 'data/catalog/manifest.json').read_text())
        history = json.loads((ROOT / 'data/catalog' / manifest['courseOfferings']['file']).read_text())
        self.assertEqual(history['schemaVersion'], 1)
        self.assertIn('CS 225', history['offeredTerms'])
        for term in history['offeredTerms']['CS 225']:
            self.assertIn(term, manifest['terms'])
            self.assertTrue((ROOT / 'data/catalog' / manifest['terms'][term]['file']).exists())


if __name__ == '__main__':
    unittest.main()
