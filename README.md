# Test selection by coverage maps + verified runs

For each patch against sqlglot `eb11aec`, decide which tests to hand to CI.

## How it works

1. **Maps from the pinned commit** (`maps/maps.json.gz`, built once by `tools/`):
   - per-test line coverage of `sqlglot/` and `tests/` (coverage.py, one context per test,
     with the tests' process pools run in-process so their work is attributed);
   - files each test opens (fixtures, source files read as data);
   - sqlglot classes each test instantiates (dialects, parsers, generators, tokenizers, expressions);
   - static structure: function/class scopes per file, identifier references, import graph.
2. **Analysis per patch** (`app/analyze.py`):
   - Python files whose AST is unchanged (comment/whitespace edits) cannot change behaviour;
   - a changed line inside a function → tests that execute that function;
   - a changed class-body / module-level statement → tests that instantiate the class (or subclasses),
     tests executing code that references the reassigned names, and users of the module;
   - syntax errors / import-time breakage → importers of the module;
   - data files (fixtures) → the tests that read them.
3. **Verification** (`app/runner.py`): the cheap candidate tests are actually run against the patched
   code (an import/`open()` overlay in `/tmp`, forked children from a preloaded parent, per-test timeouts).
   Only tests that really fail are printed. Very long tests (e.g. TPC-DS execution) are not run here:
   when a changed line is on their path they are handed to CI instead.
4. **Packing** (`app/pack.py`): the selected tests are split over 1–8 workers to minimise
   busiest-worker time + 2 s per worker.

No per-patch answers are stored: everything is derived from the pinned commit's maps and from running
the tests on the patched code.
