"""Exercise the site's actual catalog loader with mocked HTTP and select elements."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
JSC = Path('/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc')
ENGINE = shutil.which('node') or (str(JSC) if JSC.exists() else None)


@unittest.skipUnless(ENGINE, 'Install Node.js to run the optional website loader check')
class SiteCatalogTests(unittest.TestCase):
    def test_planner_average_excludes_missing_gpas(self):
        html = (ROOT / 'index.html').read_text()
        start = html.index('  function findCombos(')
        end = html.index('  function runPlanner()', start)
        harness = r'''
function assert(ok, message) { if (!ok) throw new Error(message); }
let courseMap = {};
let courseKeys = [];
function combos(gpas) {
  courseMap = {};
  courseKeys = gpas.map((gpa, i) => {
    const key = `TEST ${i}`;
    courseMap[key] = {_gpa:gpa, _genedLabels:[`Category ${i}`]};
    return key;
  });
  return findCombos(new Map(gpas.map((_, i) => [`Category ${i}`, 1])));
}
const mixed = combos([3.94, 3.92, 3.84, null]);
assert(mixed.length === 1 && mixed[0].courses.length === 4,
  'A course without GPA must still satisfy its category');
assert(mixed[0].avgGpa.toFixed(2) === '3.90',
  'Average only the three known GPAs from the reported example');
assert(combos([null, undefined])[0].avgGpa === null,
  'An entirely ungraded combination must have no average');
assert(combos([0, 4, null])[0].avgGpa === 2,
  'A recorded zero GPA must count in the average');
courseMap = {
  unknown: {_gpa:null, _genedLabels:['A']},
  zero: {_gpa:0, _genedLabels:['A']},
  known: {_gpa:3.5, _genedLabels:['A']}
};
courseKeys = Object.keys(courseMap);
assert(findCombos(new Map([['A', 1]])).map(c => c.avgGpa).join(',') === '3.5,0,',
  'Sort known averages ahead of combinations with no GPA');
(typeof print === 'function' ? print : console.log)('Planner GPA checks passed');
'''
        self.run_javascript(html[start:end] + '\n' + harness, 'Planner GPA checks passed')

    def test_syntax_and_catalog_loading(self):
        html = (ROOT / 'index.html').read_text()
        scripts = re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', html, re.S)
        syntax = '\n'.join('new Function(%s);' % json.dumps(script) for script in scripts)
        self.assertNotIn('course-catalog/data', html)
        start = html.index('  function availableCatalogTerms()')
        end = html.index('  async function onSemesterChange()', start)
        loader = html[start:end]
        harness = r'''
const log = typeof print === 'function' ? print : console.log;
const warn = [];
const console = { warn: (...args) => warn.push(args) };
function assert(ok, message) { if (!ok) throw new Error(message); }
// The JS shell lacks browser URL/DOM globals. Only HTTP and menu boundaries are mocked.
class URL {
  constructor(path, base) { this.href = String(base) + path; }
  toString() { return this.href; }
}
function select(values) {
  return { value: values[0], options: values.map(value => ({ value })),
    set innerHTML(value) { this.options = []; },
    appendChild(option) { this.options.push(option); },
    insertBefore(option, before) {
      const index = before ? this.options.indexOf(before) : this.options.length;
      this.options.splice(index, 0, option);
    }
  };
}
const document = { createElement: () => ({}) };
const CATALOG_BASE = 'https://example.test/UIUC-Course-Explorer/data/catalog/';
const CURRENT_YEAR = 2026;
const semNameMap = { sp: 'spring', su: 'summer', fa: 'fall', wi: 'winter' };
let catalogManifest = { schemaVersion: 1, terms: {} };
let courseHistoryPromise = null;
let yearSelect = select(['2026', '2025']);
let semSelect = select(['fa', 'sp', 'su', 'wi']);
const menus = [];
function buildCustomSelect(s) { menus.push(s); }
function syncCustomSelect() {}
const messages = [];
function setStatus(message) { messages.push(message); }
let loads = 0;
async function loadData() { loads++; return true; }
let calls = [];
let reply;
let historyReply;
async function fetch(url, options) {
  calls.push({ url: String(url), options });
  return String(url).endsWith('course-history-test.json') ? historyReply : reply;
}
const entry = { year: 2027, term: 'spring', file: '2027-sp-abc.json' };
async function check() {
  reply = { ok: true, json: async () => ({ schemaVersion: 1, terms: { '2027-sp': entry,
    '2028-fa': {year: 2028, term: 'fall', file: '2028-fa-abc.json'} }, availableTerms: [
      {year:2004,term:'fall'}, {year:2027,term:'spring'}, {year:2028,term:'fall'}] }) };
  await initializeCatalog();
  assert(yearSelect.value === 2028, 'Newest imported year should be selected');
  assert(semSelect.value === 'fa', 'Newest imported semester should be selected');
  assert(yearSelect.options.map(o => Number(o.value)).join(',') === '2028,2027,2004',
    'Discovered years, including 2004, should be available in descending order');
  assert(menus[0] === yearSelect && loads === 1, 'Rebuild the existing menu and load data');
  assert(calls[0].url === CATALOG_BASE + 'manifest.json', 'Support deployment below a subpath');
  assert(calls[0].options.cache === 'no-cache', 'Revalidate the manifest');

  yearSelect.value = '2027'; semSelect.value = 'sp'; calls = [];
  reply = { ok: true, json: async () => ({schemaVersion: 1, year: 2027, term: 'spring', courses: [{name:'Example'}]}) };
  const local = await fetchCatalog('signal');
  assert(local.courses.length === 1 && local.entry === entry, 'Load the selected local snapshot');
  assert(calls[0].url === CATALOG_BASE + entry.file && calls[0].options.signal === 'signal',
    'Use snapshot URL and pass through cancellation');

  entry.sha256 = 'snapshot-hash';
  catalogManifest.courseHistory = {file:'course-history-test.json'};
  const history = {schemaVersion:1,eligibleTerms:['2027-sp'],sourceSnapshots:{'2027-sp':'snapshot-hash'},
    firstOffered:{'CS 101':'2027-sp','CS 100':'2004-fa'}};
  historyReply = {ok:true,json:async () => history};
  reply = {ok:true,json:async () => ({schemaVersion:1,year:2027,term:'spring',courses:[
    {subject:'CS',number:'101'}, {subject:'CS',number:'100'}]})};
  const withHistory = await fetchCatalog('signal');
  assert(withHistory.historyAvailable && withHistory.courses[0]._isNewThisSemester &&
    !withHistory.courses[1]._isNewThisSemester, 'Only first offerings should be marked new');
  await fetchCatalog('signal');
  assert(calls.filter(call => call.url.endsWith('course-history-test.json')).length === 1,
    'Course history should be downloaded only once across semester loads');
  history.sourceSnapshots['2027-sp'] = 'outdated-hash';
  assert(!(await fetchCatalog('signal')).historyAvailable, 'Stale history must disable new-course claims');
  history.sourceSnapshots['2027-sp'] = 'snapshot-hash';
  history.eligibleTerms = [];
  assert(!(await fetchCatalog('signal')).historyAvailable, 'Incomplete earlier coverage must disable the filter');
  courseHistoryPromise = null;
  historyReply = {ok:false,status:404};
  assert(!(await fetchCatalog('signal')).historyAvailable, 'History failure must preserve normal course browsing');
  delete catalogManifest.courseHistory;
  warn.length = 0;

  reply = {ok:false, status:503};
  let failed = false;
  try { await fetchCatalog('signal'); } catch (error) { failed = true; }
  assert(failed, 'A broken local snapshot must report an error');
  reply = {ok:true, json:async () => ({schemaVersion:1,year:2026,term:'spring',courses:[{}]})};
  failed = false;
  try { await fetchCatalog('signal'); } catch (error) { failed = true; }
  assert(failed, 'Reject a snapshot from the wrong year');

  yearSelect.value = '2004';
  syncAvailableSemesters();
  assert(semSelect.value === 'fa', 'Fall is the only available semester for 2004');
  assert(semSelect.options.find(o => o.value === 'sp').disabled, 'Do not invent Spring 2004');
  const before = calls.length;
  let pending = false;
  try { await fetchCatalog('signal'); } catch (error) { pending = error.catalogPending; }
  assert(pending && calls.length === before, 'Pending semesters must not request an external catalog');

  yearSelect.value = '2027'; semSelect.value = 'sp';
  reply = {ok:true, json:async () => ({schemaVersion:1,year:2027,term:'spring',courses:[]})};
  assert((await fetchCatalog('signal')).courses.length === 0, 'An imported empty term is valid');
  reply = {ok:false,status:404};
  await initializeCatalog();
  assert(loads === 1 && warn.length === 1 && messages.length === 1,
    'A missing manifest must show an error without loading external data');
  log('Catalog loader checks passed');
}
check().catch(error => {
  log(String(error.stack || error));
  if (typeof quit === 'function') quit(1);
  else process.exit(1);
});
'''
        # Bind the logger before our local console mock for Node and JavaScriptCore.
        harness = harness.replace("const log = typeof print === 'function' ? print : console.log;",
                                  "const log = typeof print === 'function' ? print : globalThis.console.log;")
        self.run_javascript(syntax + '\n' + harness + '\n' + loader,'Catalog loader checks passed')

    def run_javascript(self, source, marker):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'check.js'
            path.write_text(source)
            result = subprocess.run([ENGINE, str(path)], text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(marker, result.stdout)

    def test_course_navigation_clears_grade_distribution(self):
        html = (ROOT / 'index.html').read_text()
        panel = html[html.index('  function updateBackBtn()'):html.index('  const LECTURE_TYPES')]
        chart = html[html.index('  function toggleGradeSheet('):html.index('  function dotClass(')]
        harness = r'''
const log = typeof print === 'function' ? print : globalThis.console.log;
function assert(ok, message) { if (!ok) throw new Error(message); }
const elements = {};
const document = {
  body: {style: {}},
  getElementById(id) {
    return elements[id] || (elements[id] = {
      innerHTML: '', style: {}, classList: {add() {}, remove() {}}
    });
  }
};
let activePanelCourse = null, openGpaRow = null, panelHistory = [], statusFetchVersion = 0;
const yearSelect = {value: '2026'}, semSelect = {value: 'fa'};
const semNameMap = {fa: 'fall'}, GENED_FILTERS = [], GRADE_COLS = ['A', 'B'];
const courseMap = {
  'CS 225': {subject: 'CS', number: '225', name: 'Data Structures', _gpa: 3.46, _pctA: 0.61},
  'CS 173': {subject: 'CS', number: '173', name: 'Discrete Structures', _gpa: 3.16, _pctA: 0.35}
};
function escapeHtml(value) { return value; }
function linkifyDesc(value) { return value; }
function gpaColorClass() { return 'b-green'; }
function buildInstrTable() {}
function renderSections() {}
function lookupGpaForCourse(course) {
  return {gpa: course._gpa, students: 10, dist: {A: 7, B: 3}};
}
const container = document.getElementById('grade-sheet-container');
openPanel('CS 225');
toggleGradeSheet('CS 225 SOLOMON', 'SOLOMON');
assert(container.innerHTML.includes('CS 225'), 'Open the original instructor distribution');
openPanel('CS 173');
assert(activePanelCourse === 'CS 173' && container.innerHTML === '' && openGpaRow === null,
  'Following a course link must clear the old distribution and selection');
toggleGradeSheet('CS 173 EVANS', 'EVANS');
assert(container.innerHTML.includes('CS 173') && !container.innerHTML.includes('CS 225'),
  'The destination instructor must show the destination distribution');
goBack();
assert(activePanelCourse === 'CS 225' && container.innerHTML === '' && openGpaRow === null,
  'Back navigation must also clear the previous distribution');
toggleGradeSheet('CS 225 SOLOMON', 'SOLOMON');
assert(container.innerHTML.includes('CS 225'), 'The original distribution can be reopened');
toggleGradeSheet('CS 225 SOLOMON', 'SOLOMON');
assert(container.innerHTML === '' && openGpaRow === null, 'Clicking again closes the chart');
toggleGradeSheet('CS 225 SOLOMON', 'SOLOMON');
openPanel('CS 999');
assert(container.innerHTML === '' && openGpaRow === null,
  'Courses not offered this semester must also clear the chart');
log('Course navigation grade distribution checks passed');
'''
        self.run_javascript(panel + chart + harness,
                            'Course navigation grade distribution checks passed')

    def test_new_course_chip_toggles_and_combines_with_existing_filters(self):
        html = (ROOT/'index.html').read_text()
        chips = html[html.index('  function toggleOther(val)'):html.index('  function toggleTerm(val)')]
        filtering = html[html.index('  function applyFilter()'):html.index('  function gpaColorClass(gpa)')]
        harness = r'''
const log = typeof print === 'function' ? print : globalThis.console.log;
function assert(ok,message) { if (!ok) throw new Error(message); }
const options = {children:[],appendChild(node) {this.children.push(node);}};
const group = {querySelector:() => options};
const document = {
  getElementById:id => id === 'search-input' ? {value:''} : group,
  createElement:() => ({dataset:{},attributes:{},classList:{add(){},remove(){}},
    setAttribute(key,value) {this.attributes[key]=value;}}),
  querySelector:selector => options.children.find(node => selector.includes(`"${node.dataset.other}"`))
};
let newCourseHistoryAvailable = true;
const activeOther = new Set(), activeLevels = new Set(), activeCredits = new Set(),
  activeGeneds = new Set(), activeTerm = new Set(), activeFormats = new Set();
let filtered = [], filteredInstructors = [], filteredDescriptions = [];
const courseKeys = ['CS 101','CS 102','CS 100'];
const courseMap = {
  'CS 101':{_isNewThisSemester:true,_hasPrereq:false},
  'CS 102':{_isNewThisSemester:true,_hasPrereq:true},
  'CS 100':{_isNewThisSemester:false,_hasPrereq:false}
};
function intCreditsSelected(){return [];}
function sortKeys(keys){return keys;}
function renderGrid(){}
function updateChipAllState(){}
function setFilterOptionCount(){}
function toggleCredit(){}
buildOtherChips();
let chip = options.children.find(node => node.dataset.other === 'new_this_semester');
assert(chip.textContent === 'New This Semester' && chip.attributes['aria-disabled'] === 'false',
  'Other should contain an enabled New This Semester chip');
chip.onclick();
assert(activeOther.has('new_this_semester') && filtered.join() === 'CS 101,CS 102',
  'Selecting the chip should include new courses only');
toggleOther('no_prereq');
assert(filtered.join() === 'CS 101','New courses must combine with existing filters');
chip.onclick();
assert(filtered.join() === 'CS 101,CS 100','Toggling off should preserve the other selected filter');
activeOther.clear();
newCourseHistoryAvailable = false;
options.children = [];
buildOtherChips();
chip = options.children.find(node => node.dataset.other === 'new_this_semester');
assert(chip.attributes['aria-disabled'] === 'true' && chip.className.includes('chip-disabled'),
  'Incomplete historical coverage should disable the chip');
chip.onclick();
assert(!activeOther.has('new_this_semester'),'A disabled chip must not enable the filter');
log('New-course filter checks passed');
'''
        self.run_javascript(chips + filtering + harness,'New-course filter checks passed')


if __name__ == '__main__':
    unittest.main()
