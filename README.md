# UIUC Course Explorer

A fast, client-side course browser for the University of Illinois Urbana-Champaign. Search courses by subject, number, or keyword, filter by level, gen ed, credit hours, and more. Includes historical GPA data pulled from real grade distributions.

**Live site:** [rosskimme.com/UIUC-Course-Explorer](https://rosskimme.com/UIUC-Course-Explorer/)

Course and section data can be collected directly from Illinois's public API:

```sh
python3 scripts/update_catalog.py --year 2027 --term spring
```

See [Updating course data](docs/UPDATING_COURSE_DATA.md) for setup, rate limits,
resuming interrupted runs, targeted instructor refreshes, and publishing snapshots.
The historical GPA dataset is maintained separately.

To archive every public semester from 2004, including full API fields and final exams:

```sh
python3 scripts/archive_catalog.py --all --background
python3 scripts/archive_catalog.py --status
```

This long-running job saves progress and resumes when the start command is repeated.
The site uses our own semester snapshots; no external course-catalog repository is required.
