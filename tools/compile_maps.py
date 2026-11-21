"""Combine raw coverage / opens / class-usage data into repo/maps/maps.json.gz.

Run inside the dev image (needs coverage):  python compile_maps.py RAW_DIR SQLGLOT_DIR OUT
"""

import ast
import glob
import gzip
import json
import os
import sys

import coverage

raw, repo, out = sys.argv[1:4]
cls_dir = sys.argv[4] if len(sys.argv) > 4 else raw
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "repo", "app"))
from scopes import build_scopes  # noqa: E402

test_ids: list[str] = []
tid_index: dict[str, int] = {}


def tix(t):
    i = tid_index.get(t)
    if i is None:
        i = tid_index[t] = len(test_ids)
        test_ids.append(t)
    return i


sets: list[tuple] = []
set_index: dict[tuple, int] = {}


def six(s):
    key = tuple(sorted(s))
    i = set_index.get(key)
    if i is None:
        i = set_index[key] = len(sets)
        sets.append(key)
    return i


lines: dict[str, dict[int, int]] = {}
file_tests: dict[str, set] = {}
for f in sorted(glob.glob(os.path.join(raw, "cov[0-9]*"))):
    data = coverage.CoverageData(basename=f)
    data.read()
    for mf in data.measured_files():
        rel = os.path.relpath(mf, "/src")
        if rel.startswith(".."):
            continue
        ctx = data.contexts_by_lineno(mf)
        fl = lines.setdefault(rel, {})
        acc = file_tests.setdefault(rel, set())
        for ln, cs in ctx.items():
            ts = {tix(c) for c in cs if c}
            if not ts:
                continue
            prev = fl.get(ln)
            fl[ln] = (prev or set()) | ts
            acc |= ts

opens: dict[str, set] = {}
times = {}
for f in sorted(glob.glob(os.path.join(raw, "opens*.json"))):
    d = json.load(open(f))
    for t, files in d["opens"].items():
        for p in files:
            opens.setdefault(p, set()).add(tix(t))
    times.update(d["times"])

classes: dict[str, set] = {}
for f in sorted(glob.glob(os.path.join(cls_dir, "classes*.json"))):
    d = json.load(open(f))
    for t, names in d.items():
        i = tix(t)
        for n in names:
            classes.setdefault(n, set()).add(i)

# static import graph: module -> imported sqlglot/tests modules
def mod_of(rel):
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


pyfiles = {}
for base in ("sqlglot", "tests"):
    for dp, dn, fn in os.walk(os.path.join(repo, base)):
        for n in fn:
            if n.endswith(".py"):
                rel = os.path.relpath(os.path.join(dp, n), repo)
                pyfiles[mod_of(rel)] = rel

imports: dict[str, set] = {}
for mod, rel in pyfiles.items():
    src = open(os.path.join(repo, rel), encoding="utf-8").read()
    tree = ast.parse(src)
    deps = set()
    is_pkg = rel.endswith("__init__.py")
    for node in ast.walk(tree):
        # only top-level / module-time imports matter for import breakage, but be generous:
        # skip imports nested in functions (lazy)
        pass
    def top_imports(body):
        for st in body:
            if isinstance(st, ast.Import):
                for a in st.names:
                    yield a.name
            elif isinstance(st, ast.ImportFrom):
                if st.level:
                    pkg = mod.split(".") if is_pkg else mod.split(".")[:-1]
                    pkg = pkg[: len(pkg) - (st.level - 1)]
                    basem = ".".join(pkg + ([st.module] if st.module else []))
                else:
                    basem = st.module or ""
                yield basem
                for a in st.names:
                    yield basem + "." + a.name
            elif isinstance(st, (ast.If, ast.Try)):
                yield from top_imports(st.body)
                yield from top_imports(getattr(st, "orelse", []))
                for h in getattr(st, "handlers", []):
                    yield from top_imports(h.body)
            elif isinstance(st, ast.ClassDef):
                yield from top_imports(st.body)
    for name in top_imports(tree.body):
        parts = name.split(".")
        for k in range(1, len(parts) + 1):
            m = ".".join(parts[:k])
            if m in pyfiles:
                deps.add(m)
    imports[mod] = deps

# transitive closure
closure = {}
def close(m, stack=()):
    if m in closure:
        return closure[m]
    seen = set()
    todo = [m]
    while todo:
        x = todo.pop()
        for d in imports.get(x, ()):
            if d not in seen:
                seen.add(d)
                todo.append(d)
    closure[m] = seen
    return seen

# tests in each test file
tests_by_file: dict[str, set] = {}
for t in list(test_ids):
    tests_by_file.setdefault(t.split("::")[0], set()).add(tid_index[t])

for mod, rel in pyfiles.items():
    if not rel.startswith("tests/"):
        continue
    deps = close(mod)
    ts = tests_by_file.get(rel, set())
    if not ts:
        continue
    for d in deps | {mod}:
        opens.setdefault("__importers__:" + d, set()).update(ts)

# scopes + identifier references in sqlglot sources
scopes = {}
refs: dict[str, list] = {}
for mod, rel in pyfiles.items():
    src = open(os.path.join(repo, rel), encoding="utf-8").read()
    sc = build_scopes(src)
    pos = {id(s): i for i, s in enumerate(sc)}
    scopes[rel] = [(s.kind, s.name, s.start, s.end, s.body_start, pos[id(s.parent)] if s.parent else -1) for s in sc]
    if not rel.startswith("sqlglot/"):
        continue
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            refs.setdefault(node.id, []).append((rel, node.lineno))
        elif isinstance(node, ast.Attribute):
            refs.setdefault(node.attr, []).append((rel, node.lineno))
for k in refs:
    refs[k] = sorted(set(refs[k]))

result = {
    "test_ids": test_ids,
    "sets": sets,
    "lines": {f: {ln: six(ts) for ln, ts in fl.items()} for f, fl in lines.items()},
    "file_tests": {f: six(ts) for f, ts in file_tests.items() if ts},
    "opens": {p: six(ts) for p, ts in opens.items()},
    "classes": {c: six(ts) for c, ts in classes.items()},
    "scopes": scopes,
    "refs": refs,
    "times": times,
}
with gzip.open(out, "wt", encoding="utf-8") as f:
    json.dump(result, f, separators=(",", ":"))
print("tests", len(test_ids), "sets", len(sets), "files", len(lines), "bytes", os.path.getsize(out))
