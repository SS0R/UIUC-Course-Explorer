# Updating course data

Run these commands in the repository root, using Python 3.9 or newer. No packages,
API keys, database server, or virtual environment are required. This updates course
and semester schedule data. The archiver preserves full bulk catalog/schedule
responses and session dates. Offered semesters are derived locally from schedules;
finals and individual catalog-detail requests are skipped by default. The site's
historical GPA source stays separate.

## Collect every public semester from 2004

```sh
python3 scripts/archive_catalog.py --all --background
```

This starts an unattended local worker that survives closing the terminal. It stops
when the computer shuts down; sleep pauses progress. Run the same command after a
restart to continue. It does not install a scheduled service, upload files, or deploy.
It always discovers the current public terms rather than assuming four semesters
exist in every year. The initial discovery found 81 public terms from Fall 2004
through Spring 2027; Summer 2027 was listed as not public and was not requested.

Monitor it:

```sh
python3 scripts/archive_catalog.py --status
tail -f .catalog-cache/archive.log
```

Stop it safely:

```sh
python3 scripts/archive_catalog.py --stop
```

The worker checks for the stop request between operations and during retry waits.
A pending HTTP request can take up to the configured timeout. Wait until `--status`
shows `workerRunning: false` before starting another collector. All collectors use
the same lock and never run concurrent API requests.

Resume by repeating the original command; **no `--resume` flag is needed for the
archiver**. To run it in the foreground, omit `--background`.

The stages run in this order across all selected terms:

1. **Schedule:** full department/course/section/meeting responses and the website's
   JSON snapshot. Current default/enrolling terms are prioritized, then historical
   terms in chronological order. The prior Spring 2027 full import is reused.
2. **Catalog:** full department/course catalog responses, including courses not
   offered in the selected semester.
3. **Sessions:** parts of term and session start/end dates.

Finals and individual catalog-detail passes are not part of the default job.
`termsOffered` is calculated from the semester schedules already collected, avoiding
thousands of individual course requests. The optional passes remain available only
through an explicit `--phases` selection; do not enable them unless requested.

At five seconds per request, 10,000 requests take about 14 hours, plus response time
and retries. Completion now requires the three retained stages above. Consult
`completedByPhase`, errors, and each ZIP's `requiredPhases` and `complete` fields.

HTTP 404 from session endpoints (or explicitly requested optional endpoints) is stored as
**checked but unavailable**, with its response body and timestamp. It is distinct
from an HTTP 200 response containing no exam events. Temporary failures are retried;
failures after the retry limit remain errors, and other terms continue. Repeat the
command to retry unfinished/failed stages. Successfully completed stages are skipped.
The archive describes what Illinois currently exposes for historical terms; it cannot
recover historical fields that Illinois has removed or never published.

This machine had approximately 3.5 GB free when the backfill started. Checkpoints
are gzip-compressed inside SQLite, historical CSVs are compressed, and ZIP archives
compress the original response bytes. If free space falls below 512 MB, the worker
waits without making further API requests. `--status` reports `waiting_for_disk`;
it rechecks every 30 seconds and automatically resumes when space recovers. The stop
command remains responsive while waiting. Do not delete `.catalog-cache/` during an
unfinished import, since it holds its checkpoints.
If the disk becomes completely full, status-file updates may temporarily lag; the
worker keeps its waiting state in memory and writes it once space recovers.
Transient SQLite storage errors (such as "unable to open database file" or a full
disk) also trigger automatic recovery: the worker reports `waiting_for_storage`,
rolls back the failed transaction, waits 30 seconds, and resumes checkpoints after
checking disk space. Explicit refreshes keep their resource versions across these
retries. Permission failures and database corruption remain errors requiring inspection.

To archive just one term (using saved responses):

```sh
python3 scripts/archive_catalog.py --year 2027 --term spring
```

To collect fresh versions of the retained schedule, catalog, and session data for one term:

```sh
python3 scripts/archive_catalog.py --year 2027 --term spring --refresh
```

Stop the all-history worker first. `--refresh` creates new versions for that term;
when resuming an interrupted refresh, **omit `--refresh`**, or it will start over.
For a quick website-only refresh, use `update_catalog.py` below. That command updates
schedule snapshots and derived history indexes; it does not refresh the other retained archive responses.

For a deliberate staged run, `--phases schedule catalog sessions` is supported.
`finals` requires a completed `schedule` stage; `catalog-details` requires a completed
`catalog` stage. Completion applies to the requested stages, while each archive
ZIP's `complete` flag requires schedule, catalog, and session stages. Finals and
individual catalog-detail requests are optional and excluded from default completion.

## Reading the full archive

`data/archive/YEAR-CODE.zip` preserves the original response bytes for a term, not
just fields the website understands. A ZIP is rewritten at each completed stage
and every 100 requests during course-by-course stages. Saved SQLite checkpoints are
more current between ZIP exports. Each ZIP contains:

- `manifest.json`: every request URL/path, HTTP status, fetch timestamp, checksum,
  byte count, archived filename, completed phases, and errors.
- `responses/`: successful original XML, including course descriptions, gen ed
  categories, restrictions, fees, notes, dates, meeting details, instructor attributes,
  and any additional fields returned by the retained bulk APIs.
- `unavailable/`: response bodies for checked HTTP 404 resources.

`data/archive/manifest.json` summarizes per-term coverage and ZIP checksums.
`data/archive/discovery.json` preserves parsed term availability for both modules.
`data/archive/discovery.zip` preserves the exact XML discovery responses and their
request metadata. They also remain in SQLite's `responses` table under
`archive-discovery-*` run IDs. `data/archive/reference/` contains the official API
specification and XML schema used to determine coverage.

If a cache is lost or this repo is freshly cloned, the worker verifies existing
ZIP checksums and restores their responses and stage completion into SQLite.
Completed responses do not need to be downloaded again. Keep the ZIPs, their
coverage manifest, and the website snapshots together. An old resumed archive
collection does not overwrite a website snapshot updated after it began.

## Offered semesters without extra API requests

`data/catalog/manifest.json` points to a separate `courseOfferings` JSON asset. Its
`offeredTerms` map lists each course code's semesters chronologically, computed
from the published schedule snapshots. It includes public upcoming terms already
listed in the schedules, and does not predict unannounced future offerings.

```sh
python3 - <<'PYOFFERINGS'
import json
from pathlib import Path
root = Path('data/catalog')
manifest = json.loads((root / 'manifest.json').read_text())
offerings = json.loads((root / manifest['courseOfferings']['file']).read_text())
print(offerings['offeredTerms'].get('CS 225', []))
PYOFFERINGS
```

`coverageComplete` indicates whether all discovered public schedules are represented.
History begins in Fall 2004. Rebuild offline with `python3 scripts/course_history.py`
after stopping any active updater; schedule publishing rebuilds both indexes
implicitly. The website's first-offering filter still uses the separate small
`courseHistory` asset, so the full offered-semester index adds no page-load download.
The website does not yet display the full course-offering timeline.

To remove previously collected finals from SQLite, term ZIPs, and request logs,
stop the worker and run `python3 scripts/prune_finals.py`. This does not download
anything and preserves the other responses. After cleanup, completion reflects the
three retained stages. Finals should not be downloaded again by the default command.

For historical CSV exports, use `gzip.open('data/catalog/2004-fa.csv.gz', 'rt')`
with Python's `csv` module, or decompress the file with your preferred archive tool.
Only the selected semester's compact JSON is fetched by website visitors; archive
ZIPs and other semesters are not downloaded during page loading.

## Refresh a semester

```sh
python3 scripts/update_catalog.py --year 2027 --term spring
```

Supported terms: `spring`, `summer`, `fall`, `winter`. Use the year assigned to the
term in Course Explorer. A new term must be available in Illinois's API first.

This collects the term's subject list, then downloads one bulk XML response per
department using `?mode=cascade`. These responses include course descriptions,
credit hours, prerequisites within the descriptions, gen ed attributes, sections,
instructors, and meeting times/locations. This is the semester's **offered courses**,
not every course in the university's academic catalog. Blank instructor fields
stay blank until Illinois assigns instructors; run the updater again to pick them up.
Some legitimate offerings, such as special studies courses, have no description in
the API. They remain in the dataset with a blank description.

The updater validates every department before replacing the site's snapshot. Each
ordinary invocation starts a fresh run and rechecks existing records, including
instructor assignments. It prints department progress and a final change summary. Stop the all-history
worker first; these commands share its updater lock.

Allow at least about 16 minutes for a term with 188 departments at the default
pacing, plus response time. Throttling can make it take much longer. The updater
never fetches departments concurrently.

## Stop and resume

Press Ctrl+C to stop. Successful responses are saved immediately in the local
SQLite database. Resume with the **same year, term, and scope**:

```sh
python3 scripts/update_catalog.py --year 2027 --term spring --resume
```

`--resume` reuses validated responses from the newest **unpublished** matching run.
It is for continuing an interrupted import, not getting fresh instructor data.
It also works after validation/publication fails. Completed published runs cannot
be resumed. Omit `--resume` to start a fresh collection.

If an interrupted run is old, start a new run instead of resuming stale responses.
The command prints its original start date when resuming. The snapshot records the
source fetch dates; the site's full-refresh date uses the earliest fetch in that
full run, so resuming an old collection does not imply that every record was just
checked.

## Refresh one department or course

After a full snapshot exists for a semester:

```sh
python3 scripts/update_catalog.py --year 2027 --term spring --subject CS
python3 scripts/update_catalog.py --year 2027 --term spring --subject CS --course 225
```

A course refresh still makes one department bulk request, then selects the requested
course. It merges changes into the existing snapshot without removing other courses
or departments. It preserves the full-refresh date and advances the latest-update
timestamp, visible in the freshness tooltip. A course missing from an otherwise valid
department response counts as a removal, subject to the safeguards below.

For a small test before a full semester exists, collect without publishing:

```sh
python3 scripts/update_catalog.py --year 2027 --term spring --subject CS --course 225 --no-publish
```

`--no-publish` saves and validates the responses without changing public data.
For a full-term preview, omit `--subject` and `--course`. You can publish a completed
full-term preview without another download by repeating its command with `--resume`
and without `--no-publish`. Scoped publication always needs an existing full snapshot;
it will not create a misleading partial semester.

## Rate limits and failures

There is no verified fixed request limit built into this tool. Its defaults are
deliberately conservative:

- One request at a time, with five seconds between responses and the next request.
- Up to six retries for throttling, temporary server/network errors, or malformed
  responses, with waits starting at 30 seconds and doubling, plus a little jitter.
- `Retry-After` is respected, including HTTP-date values, even when it asks for a
  longer wait. HTTP 429 also increases the pacing for subsequent requests.
- A 90-second request timeout. Successful, validated XML is checkpointed in SQLite.
- An exclusive updater lock prevents two processes from using the same cache at once.

If throttled, let the command wait. For a slower run or resume:

```sh
python3 scripts/update_catalog.py --year 2027 --term spring --resume --delay 15
```

`--delay` is seconds and cannot be below two. Don't run multiple copies with separate
cache directories to evade throttling. A 404 stops immediately; verify the requested
term exists. An HTML error page or unexpected XML is never accepted as course data.

A failed or interrupted collection leaves the published manifest untouched. A drop
of more than 15% in courses **or sections** within the requested scope blocks
publication. This catches suspiciously incomplete responses. Inspect the API and
saved XML before accepting a legitimate reduction:

```sh
python3 scripts/update_catalog.py --year 2027 --term spring --resume --allow-removals
```

Use the same `--subject`/`--course` if the original run was scoped. Do not automatically
add this flag to bypass a failed check. The safeguard cannot detect every upstream
data problem; review the change summary and spot-check important courses.

## Files and publication

| Path | Purpose | Commit? |
| --- | --- | --- |
| `scripts/update_catalog.py` | Collection, validation, export | Yes |
| `.catalog-cache/catalog.sqlite3` | Run history, compressed XML checkpoints, latest exported snapshots, archive stage state | No; ignored |
| `.catalog-cache/updater-lock.sqlite3` | Cross-process lock; released automatically on exit | No; ignored |
| `data/catalog/manifest.json` | Terms available locally, snapshot filenames, counts, checksums, timestamps | Yes |
| `data/catalog/YEAR-TERM-HASH.json` | Compact website snapshot; course metadata stored once | Yes |
| `data/catalog/YEAR-TERM.csv` | Website-only updater CSV export, one row per section meeting | Yes |
| `data/catalog/YEAR-TERM.csv.gz` | Compressed historical CSV export from the archiver | Yes |
| `data/archive/` | Lossless per-term ZIPs for retained responses, coverage manifests, discovery, API reference | Yes |
| `data/catalog/course-offerings-*.json` | Derived offered-semester lists; loaded only when needed | Yes |

Term filename codes are `sp`, `su`, `fa`, and `wi`. JSON filenames include a content
hash. The updater writes the new JSON first and switches the manifest last, so a
failed collection cannot direct the site at a partial snapshot. Previous JSON files
are retained for already-open pages and rollback. CSV is an auxiliary export, replaced
before the manifest; the website reads JSON, not CSV.

The local database needs no manual editing. For inspection, Python's `sqlite3`
module or a SQLite browser can query `runs`, `responses`, and `snapshots`. `responses`
contains `run_id`, `path`, `fetched_at`, and the `body`. New bodies are gzip-compressed;
`response_bytes(body)` in `scripts/update_catalog.py` reads both compressed and older
plain XML. `missing_responses` records optional HTTP 404 responses; `archive_terms`
tracks stage completion and resource versions. Keep the cache if you
want to resume; deleting it loses collection progress but not the committed snapshots.
`--cache-dir PATH` and `--output PATH` support isolated testing. Use the same cache
directory when resuming. Published files are the merge baseline, so targeted refreshes
also work in a fresh clone after a full snapshot has been committed.

After a successful update:

Before committing or deploying generated files, stop the background worker and wait
for `workerRunning: false`. This keeps the staged manifest and its referenced files
consistent. You can restart the worker after committing; it resumes saved work.

```sh
git diff --stat
python3 -m unittest discover -s tests -v
python3 -m http.server 8000
```

Open `http://localhost:8000/`, select the term, and spot-check a description,
instructors, and meeting times against Course Explorer. Preview through HTTP;
opening `index.html` with a `file://` URL does not support these fetches.
The website loader test uses Node.js when installed, or macOS's built-in
JavaScriptCore shell; it is skipped if neither is available. The updater tests
require only Python and make no network requests.

Review and commit the changed code/docs and generated public files, then use your
normal deployment process. Running the updater changes local files; it does not
push Git commits or deploy the website. Deploy `index.html` and `data/catalog/`
together, preserve their relative paths, and enable your host's normal compression.
The local SQLite database is not needed by the deployed site.

The browser opens the newest locally imported term. The selector includes all public
years/terms in discovery, back to 2004, and disables semesters absent from the API's
listing for a year. A semester still being imported shows a pending message. There
is no dependency or fallback to the previous course-catalog repository. The browser
downloads only the selected semester's compact JSON alongside the existing GPA request;
it never runs the collector. A missing manifest or broken local snapshot shows an
error. Reload to see terms completed by a local background import, and deploy the
updated files to make them available to site visitors. The manifest is requested with
cache revalidation; hashed JSON URLs allow caching across visits. Live enrollment
status requests remain separate and can change after a snapshot.

If you clean up old hashed JSON files, keep those referenced by the current manifest
and allow time for already-open clients before deleting older versions. You can roll
back a term by restoring its manifest entry and referenced JSON from Git.

## New This Semester filter

The Other filters include **New This Semester**. A course matches when its subject
and course number first appear in that semester's schedule across our complete
earlier schedule history. Returning courses and title changes under the same code
do not count as new. Renumbered courses are distinct course codes.

The history begins in Fall 2004; it cannot establish whether courses existed before
then. Fall 2004 is treated as a baseline and its new-course filter is disabled.
Missing earlier public semesters, unavailable history, or a history index that does
not match the selected snapshot also disable the filter rather than guess.

`update_catalog.py` rebuilds the hashed first-offering and offered-semester indexes
whenever it publishes a schedule snapshot. The manifest's `courseHistory` entry
points to the small `course-history-*.json` asset used by this filter.
The website fetches this index once per page load, alongside the selected semester;
it does not download historical semester files to calculate the filter.

To rebuild the index from existing snapshots without API requests, stop the archive
worker, wait for its lock to be released, then run:

```sh
python3 scripts/course_history.py
```

Restart a previously running archive with `python3 scripts/archive_catalog.py --all --background` afterward. Keep term discovery current when importing newly released
semesters; the filter requires known, complete coverage of earlier public terms.

## Guidance for agents

1. Read this document and inspect `git status` before making changes; preserve unrelated edits.
2. Check `python3 scripts/archive_catalog.py --status`. Do not launch another
   collector while it is running. Use its stop command when a separate refresh is
   needed, and wait for it to release the lock. Run the appropriate full or scoped command. Start a fresh run for current data;
   use `--resume` only for continuing a known unfinished run.
3. Monitor progress. On throttling, wait or increase `--delay`. Do not parallelize requests.
4. If validation fails, inspect the failure and cached responses. Keep the last good
   snapshot. Do not bypass removal checks without confirming the removals.
5. Run the tests, review the change summary and manifest, and spot-check output.
6. Report whether the update is local or deployed. Do not claim a refresh completed
   if only part of the term was collected.

Illinois's public API documentation entry:
<https://answers.uillinois.edu/illinois/page.php?id=88215>.
