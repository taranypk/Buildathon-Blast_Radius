# Blast radius selector

For each patch in `/data/commits`, decide which sqlglot tests to run and how to
split them over workers. Nothing is looked up by patch: every decision comes
from the patch text, maps built once from sqlglot at the pinned commit
`eb11aec`, and (when it is cheap enough) actually running candidate tests
against the patched code inside the container.

## Pipeline (`app/`)

1. **Candidates** (`analyze.py`)
   - Comment/blank-line-only edits (same token stream) select nothing; so do
     `x is None` <-> `x == None` swaps.
   - A changed statement inside a function selects the tests that executed it
     (line coverage, `map.json.gz`).
   - Import-time code (class bodies, module level, decorators, metaclass code)
     selects the tests that use the class (recorded instances of it or its
     subclasses) and read the changed name; values that feed other import-time
     code (subclass dict merges, metaclasses) select every user of the class
     and of the class that encloses it. Entries of tokenizer/parser keyword
     tables are narrowed to tests whose SQL mentions the keyword.
   - Fixture edits select the tests that read the file.
   - Renames, syntax errors, deleted modules and edits to the dialect
     registry / package globals fall back to broad, unchecked selections.
2. **Check** (`runner.py`): the patch is applied to a symlinked copy of the
   source tree in `/tmp`; a forked child imports the patched sqlglot fresh and
   runs the candidate tests with `unittest` (process pools run in-process but
   still pickle). Big fixture-driven tests run only the fixture cases the change
   can reach (`casemap.json.gz`); the TPC-DS executor test runs only the queries
   it can reach; two very slow tests use scaled-down stand-ins.
3. **Schedule** (`main.py`): per commit, checking is chosen only when its
   expected CPU is below what scheduling the candidates unchecked would cost;
   the tests that fail (plus anything left unchecked) are split over 1-8
   workers to minimise busiest worker + 2 s per worker.

## Maps (`tools/`, built from the pinned commit)

- `mapplug.py`: pytest plugin used while running the full suite under
  `coverage --cov-context=test`; records files each test opens and classes it
  instantiates, runs process-pool work in-process, clears registry/schema
  caches before each test so cached work is attributed to every test.
- `qplug.py`, `caseplug.py`: per-query (TPC-DS) and per-fixture-case coverage.
- `mro_dump.py`: class hierarchy of every sqlglot class.
- `build_map3.py`: combines the coverage data into `app/map.json.gz`.

`vendor/sqlglot-eb11aec.tar.gz` is `git archive` of sqlglot at
eb11aec4be8cb2122afe713a4816662c3f0fb2fa (sqlglot/, tests/, pyproject.toml,
setup.py, sqlglotc/).
