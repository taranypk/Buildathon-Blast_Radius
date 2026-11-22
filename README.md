# Blast radius selector

For each patch in `/data/commits`, decide which sqlglot tests to run.

How it works:

1. **Coverage map** (`app/map.json.gz`): built once from sqlglot at the pinned
   commit. For every test it records the source lines it executes and the
   files it opens (fixtures). Built with process pools run in-process so the
   big optimizer/executor tests are measured correctly.
2. **Candidates** (`app/analyze.py`): parse the diff; comment-only edits (same
   AST) select nothing; changed lines inside functions select the tests that
   run those lines; class/module-level (import-time) changes select the tests
   that use that class or module; fixture edits select the tests that read them.
3. **Check** (`app/runner.py`): apply the patch to a symlinked copy of the
   source in `/tmp`, fork, and run the candidate tests with unittest. Very slow
   tests use cheaper stand-ins or are scheduled without checking.
4. **Schedule** (`app/main.py`): only the tests that fail, split over 1-8
   workers to minimise busiest load + 2 s per worker.

## Build

`vendor/sqlglot-eb11aec.tar.gz` is `git archive` of sqlglot at commit
eb11aec4be8cb2122afe713a4816662c3f0fb2fa (sqlglot/, tests/, pyproject.toml, setup.py, sqlglotc/).
