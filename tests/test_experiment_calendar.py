"""Check weekly calendar behavior in the experimental UI using its actual JavaScript."""
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


@unittest.skipUnless(ENGINE, 'A JavaScript engine is required')
class ExperimentalCalendarTests(unittest.TestCase):
    def run_calendar(self, checks):
        html = (ROOT / 'index.html').read_text()
        start = html.index('  // WEEKLY SECTION CALENDAR')
        end = html.index('  function renderSections(c)', start)
        prelude = r'''
function assert(ok, message) { if (!ok) throw new Error(message); }
function escapeHtml(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function meeting(overrides = {}) {
  return Object.assign({crn:'100', section:'A', typeCode:'LEC', typeName:'Lecture',
    days:'MWF', startTime:'09:00AM', endTime:'09:50AM', isTBA:false,
    instructors:['Test Instructor'], building:'Main Hall', room:'101', partOfTerm:'1'}, overrides);
}
'''
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'calendar.js'
            path.write_text(prelude + html[start:end] + '\n' + checks)
            result = subprocess.run([ENGINE, str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_time_and_day_parsing(self):
        self.run_calendar(r'''
assert(calendarTimeMinutes('12:00AM') === 0, 'Midnight');
assert(calendarTimeMinutes('12:00PM') === 720, 'Noon');
assert(calendarTimeMinutes(' 1:30 pm ') === 810, 'Spaced afternoon time');
assert(calendarTimeMinutes('18:45') === 1125, '24-hour input');
['ARRANGED', '', '25:00', '9:90AM', '00:30PM'].forEach(value =>
  assert(calendarTimeMinutes(value) === null, 'Invalid time: ' + value));
assert(calendarDays('TR').join('') === 'TR', 'Tuesday and Thursday are distinct');
assert(calendarDays('MTWRFSU').length === 7, 'All seven days');
assert(calendarDays('Monday, Thursday').join('') === 'MR', 'Named days');
assert(calendarDays('n.a.').length === 0, 'Unknown days are unscheduled');
assert(calendarTimeLabel(720) === '12:00 PM', 'Noon label');
''')

    def test_overlap_layout_and_abutting_meetings(self):
        self.run_calendar(r'''
const rows = layoutCalendarDay([
  {start:540,end:600,index:0}, {start:550,end:580,index:1},
  {start:575,end:640,index:2}, {start:660,end:710,index:3},
  {start:710,end:760,index:4}
]);
rows.forEach((a, i) => rows.slice(i + 1).forEach(b => {
  if (a.start < b.visualEnd && b.start < a.visualEnd)
    assert(a.lane !== b.lane, 'Overlapping blocks must not cover each other');
}));
assert(rows[0].lanes === 3, 'Connected overlaps use three lanes');
assert(rows[3].lanes === 1 && rows[4].lanes === 1, 'Independent and abutting groups regain full width');
const short = layoutCalendarDay([{start:540,end:545,index:0},{start:550,end:560,index:1}]);
assert(short[0].lane !== short[1].lane, 'Minimum block height must not cover short meetings');
''')

    def test_repeated_meetings_and_all_sections(self):
        self.run_calendar(r'''
const course = {sections:[meeting(), meeting({days:'R', startTime:'01:00PM',endTime:'01:50PM'}),
  meeting({crn:'200',section:'B',days:'T'})]};
const model = buildCalendarModel(course);
assert(model.byDay.M.length === 1 && model.byDay.W.length === 1 && model.byDay.F.length === 1, 'MWF repeated');
assert(model.byDay.R.length === 1 && model.byDay.T.length === 1, 'Every section and every meeting remains in the calendar');
assert(model.start === 540 && model.end === 840, 'Grid covers earliest and latest meetings');
''')

    def test_weekends_overnight_and_unscheduled(self):
        self.run_calendar(r'''
const course = {sections:[meeting({days:'S'}), meeting({days:'U',startTime:'11:30PM',endTime:'12:30AM'}),
  meeting({isTBA:true,startTime:'ARRANGED'}), meeting({days:'n.a.'}), meeting({endTime:''})]};
const model = buildCalendarModel(course);
assert(model.days.join('') === 'MTWRFSU', 'Weekend columns included when needed');
assert(model.byDay.M[0].start === 0 && model.byDay.M[0].end === 30, 'Overnight wraps to Monday');
assert(model.byDay.U[0].end === 1440, 'Sunday ends at midnight');
assert(model.unscheduled.length === 3, 'TBA, unknown days, and missing end times remain visible');
assert(model.start === 0 && model.end === 1440, 'Overnight range stays in bounds');
assert(!buildCalendarModel({sections:[]}).hasEvents, 'Empty courses handled');
''')

    def test_rendering_and_meeting_interactions(self):
        self.run_calendar(r'''
const course = {sections:[meeting({section:'A<script>',building:'Main & Annex'}),
  meeting({days:'R',startTime:'01:00PM',endTime:'01:50PM'})]};
const elements = {};
const document = {getElementById: id => elements[id] || (elements[id] = {innerHTML:'',hidden:false,scrollIntoView(){}})};
const courseMap = {TEST:course};
let activePanelCourse = 'TEST';
renderCourseCalendar(course);
const html = elements['view-cal'].innerHTML;
assert(!html.includes('<select') && !html.includes('calendar-section'), 'No section dropdown');
assert(html.includes('A&lt;script&gt;') && !html.includes('A<script>'), 'Section labels are escaped');
assert(html.includes('onclick="showCalendarMeeting(0)"'), 'Blocks open meeting details');
assert(html.includes('tabindex="0"') && html.includes('aria-label='), 'Scrollable calendar has accessible labels');
showCalendarMeeting(0);
assert(!elements['calendar-meeting-details'].hidden, 'Selecting a meeting reveals details');
assert(elements['calendar-meeting-details'].innerHTML.includes('Main &amp; Annex'), 'Meeting details escaped');
assert(elements['calendar-meeting-details'].innerHTML.includes('Copy CRN'), 'Calendar detail includes copy button');
assert(calendarGridHtml({sections:[meeting({isTBA:true})]}).includes('Copy CRN'), 'Arranged meetings include copy button');
assert(calendarGridHtml({sections:[meeting({isTBA:true})]}).includes('calendar-unscheduled'), 'Arranged-only course retains details');
assert(calendarGridHtml({sections:[]}).includes('No sections found.'), 'Empty calendar message');
''')

    def test_copy_crn_clipboard_and_fallback(self):
        output = self.run_calendar(r'''
const clipboardValues = [];
let clipboardFails = false, fallbackSucceeds = true, removed = 0, restored = 0, fallbackValue;
const navigator = {clipboard: {writeText: async value => {
  if (clipboardFails) throw new Error('Denied');
  clipboardValues.push(value);
}}};
const document = {
  activeElement: {focus(){restored++;}},
  createElement(){return {style:{},focus(){},select(){fallbackValue=this.value;},remove(){removed++;}};},
  body:{appendChild(){}},
  execCommand(command){assert(command==='copy','Copy command');return fallbackSucceeds;}
};
const timers = [];
function setTimeout(fn){timers.push(fn);}
function button(crn){return {dataset:{crn},disabled:false,textContent:'Copy CRN',isConnected:true,setAttribute(name,value){this[name]=value;}};}
async function check(){
  assert(sectionCrnButton(meeting()).includes('data-crn="100"'), 'Copy button has exact CRN');
  assert(sectionCrnButton(meeting({crn:''})) === '', 'No copy button without a CRN');
  assert(sectionCrnButton(meeting({crn:'x" onclick="bad'})) === '', 'Malformed CRNs rejected');
  const native = button('00123');
  await copySectionCrn(native);
  assert(clipboardValues[0] === '00123', 'Copies only CRN and preserves leading zeros');
  assert(native.textContent === 'Copied!' && !native.disabled, 'Success feedback');
  timers.shift()();
  assert(native.textContent === 'Copy CRN', 'Button resets after feedback');
  clipboardFails = true;
  const local = button('45678');
  await copySectionCrn(local);
  assert(local.textContent==='Copied!' && fallbackValue==='45678', 'Local preview fallback copies exact CRN');
  assert(removed===1 && restored===1, 'Temporary field cleaned up and focus restored');
  fallbackSucceeds = false;
  const blocked = button('99999');
  await copySectionCrn(blocked);
  assert(blocked.textContent==='Copy failed' && !blocked.disabled, 'Clipboard rejection is not reported as success');
  assert(blocked.title.includes('99999'), 'Manual copy value available on failure');
  assert(removed===2 && restored===2, 'Failure also restores focus');
  clipboardFails = false;
  const feedback = {textContent:''};
  const row = {dataset:{crn:'54321'},isConnected:true,innerHTML:'Section time and room',
    querySelector(){return feedback;},setAttribute(name,value){this[name]=value;}};
  await copySectionRow(row);
  assert(clipboardValues[clipboardValues.length-1]==='54321', 'Clicking row copies its CRN');
  assert(feedback.textContent.includes('Copied!') && row.innerHTML==='Section time and room', 'Feedback preserves section content');
  const oldReset=timers.pop();
  await copySectionRow(row);
  oldReset();
  assert(feedback.textContent.includes('Copied!'), 'Earlier feedback timer does not clear a later click');
  timers.pop()();
  assert(feedback.textContent==='' && row['aria-busy']==='false', 'Row resets feedback and busy state');
  clipboardFails=true;
  await copySectionRow(row);
  assert(feedback.textContent.includes('Copy failed') && row.title.includes('54321'), 'Failed row copy offers manual CRN');
}
check().then(() => print('Clipboard checks passed')).catch(error => print('Clipboard checks failed: ' + error));
''')
        self.assertIn('Clipboard checks passed', output)

    def test_list_view_rows_copy_crns(self):
        html = (ROOT / 'index.html').read_text()
        start = html.index('  function renderSections(c)')
        end = html.index('  function switchView(v)', start)
        self.run_calendar(html[start:end] + r'''
const elements={};
const document={getElementById:id=>elements[id]||(elements[id]={innerHTML:''})};
function loadSectionStatuses(){}
function switchView(){}
function dotClass(){return 'sdot-LEC';}
renderSections({sections:[meeting(),meeting({crn:'200',isTBA:true,startTime:'ARRANGED'})]});
const list=elements['view-list'].innerHTML;
assert(!list.includes('copy-crn-btn'), 'No separate copy buttons in list view');
assert((list.match(/onclick="copySectionRow\(this\)"/g)||[]).length===2, 'Regular and TBA rows copy on click');
assert((list.match(/tabindex="0"/g)||[]).length===2 && list.includes("event.key === 'Enter'"), 'Rows support keyboard activation');
assert(list.includes('CRN 100') && list.includes('CRN 200'), 'CRNs visible for manual copying');
''')

    def test_rmp_links_use_school_filter_and_expand_name_order(self):
        html = (ROOT / 'index.html').read_text()
        start = html.index('  function rmpSearchUrl(')
        end = html.index('  function toggleGradeSheet(', start)
        self.run_calendar(html[start:end] + r'''
const base='https://www.ratemyprofessors.com/search/professors/1112';
assert(rmpSearchUrl('Kang, S')===base+'?q=Kang', 'Reported example searches surname within UIUC');
assert(rmpSearchUrl('Smith, J. P.')===base+'?q=Smith', 'Initials are not searched as given names');
assert(rmpSearchUrl('Solomon, Morgan Q')===base+'?q=Morgan%20Solomon', 'Full given name precedes surname');
assert(rmpSearchUrl('Van Der Meer, A')===base+'?q=Van%20Der%20Meer', 'Compound surnames preserved');
assert(rmpSearchUrl('Grace Hopper')===base+'?q=Grace%20Hopper', 'Already ordered names preserved');
assert(rmpSearchUrl('')===base, 'Missing name falls back to school search');
assert(decodeURIComponent(rmpSearchUrl("O'Neil & García, S").split('?q=')[1])==="O'Neil & García", 'Special characters safely encoded');
const elements={};
const document={getElementById:id=>elements[id]||(elements[id]={innerHTML:''})};
const LECTURE_TYPES=new Set(['LEC']);
function extractLastName(name){return name.split(',')[0].toLowerCase();}
function lookupGpaForCourse(){return null;}
buildInstrTable({subject:'TEST',number:'100',sections:[meeting({instructors:['Kang, S']})]});
assert(elements['instr-tbody'].innerHTML.includes(base+'?q=Kang'), 'Rendered instructor link uses corrected URL');
assert(elements['instr-tbody'].innerHTML.includes('Search RMP'), 'Link clearly describes search behavior');
''')

    def test_all_inline_scripts_parse(self):
        html = (ROOT / 'index.html').read_text()
        scripts = re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', html, re.S)
        self.run_calendar('\n'.join('new Function(%s);' % json.dumps(script) for script in scripts))


if __name__ == '__main__':
    unittest.main()
