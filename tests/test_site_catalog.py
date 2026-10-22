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
async function fetch(url, options) { calls.push({ url: String(url), options }); return reply; }
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
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'check.js'
            path.write_text(syntax + '\n' + harness + '\n' + loader)
            result = subprocess.run([ENGINE, str(path)], text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Catalog loader checks passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
