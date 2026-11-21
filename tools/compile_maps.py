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

# lazy imports: a test that uses class C (dialect, parser, ...) imports C's module and its closure
for cname, ts in classes.items():
    mod = cname.split(":")[0]
    if mod not in pyfiles:
        continue
    for d in close(mod) | {mod}:
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

# ---- functions executed at import time (called from module / class bodies, decorators,
#      class-creation hooks), mapped to the module/class bodies that trigger them
def import_time_map():
    defs = {}      # (mod, qualname) -> (rel, node)
    aliases = {}   # mod -> {local name: (target mod, attr or None)}
    for mod, rel in pyfiles.items():
        if not rel.startswith("sqlglot/"):
            continue
        tree = ast.parse(open(os.path.join(repo, rel), encoding="utf-8").read())
        al = aliases.setdefault(mod, {})
        is_pkg = rel.endswith("__init__.py")
        def walk_defs(body, prefix):
            for st in body:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    defs[(mod, prefix + st.name)] = (rel, st)
                    if isinstance(st, ast.ClassDef):
                        walk_defs(st.body, prefix + st.name + ".")
                elif isinstance(st, ast.ImportFrom):
                    if st.level:
                        pkg = mod.split(".") if is_pkg else mod.split(".")[:-1]
                        pkg = pkg[: len(pkg) - (st.level - 1)]
                        base = ".".join(pkg + ([st.module] if st.module else []))
                    else:
                        base = st.module or ""
                    for a in st.names:
                        if base + "." + a.name in pyfiles:
                            al[a.asname or a.name] = (base + "." + a.name, None)
                        else:
                            al[a.asname or a.name] = (base, a.name)
                elif isinstance(st, ast.Import):
                    for a in st.names:
                        al[(a.asname or a.name).split(".")[0]] = (a.name if a.asname else a.name.split(".")[0], None)
                elif isinstance(st, (ast.If, ast.Try)):
                    walk_defs(st.body, prefix)
        walk_defs(tree.body, "")

    def resolve(mod, func, scope_prefix):
        """call target expression -> (mod, qualname) or None"""
        if isinstance(func, ast.Name):
            for cand in (scope_prefix + func.id, func.id):
                if (mod, cand) in defs:
                    return (mod, cand)
            tgt = aliases.get(mod, {}).get(func.id)
            if tgt and tgt[1] and (tgt[0], tgt[1]) in defs:
                return (tgt[0], tgt[1])
        elif isinstance(func, ast.Attribute):
            chain = []
            x = func
            while isinstance(x, ast.Attribute):
                chain.append(x.attr)
                x = x.value
            if isinstance(x, ast.Name):
                chain.append(x.id)
                chain.reverse()
                tgt = aliases.get(mod, {}).get(chain[0])
                if tgt:
                    m2 = tgt[0] if tgt[1] is None else tgt[0]
                    rest = chain[1:] if tgt[1] is None else [tgt[1]] + chain[1:]
                    # walk into submodules
                    while rest and (m2 + "." + rest[0]) in pyfiles:
                        m2 = m2 + "." + rest[0]
                        rest = rest[1:]
                    q = ".".join(rest)
                    if (m2, q) in defs:
                        return (m2, q)
                elif (mod, ".".join(chain)) in defs:
                    return (mod, ".".join(chain))
        return None

    def calls_in(nodes, skip_funcs=True):
        out = []
        stack = list(nodes)
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.Lambda,)):
                continue
            if skip_funcs and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # decorators and defaults run at definition time
                stack.extend(n.decorator_list)
                stack.extend(n.args.defaults)
                stack.extend(d for d in n.args.kw_defaults if d is not None)
                continue
            if isinstance(n, ast.ClassDef):
                stack.extend(n.decorator_list)
                stack.extend(n.bases)
                stack.extend(n.body)
                continue
            if isinstance(n, ast.Call):
                out.append(n.func)
            stack.extend(ast.iter_child_nodes(n))
        return out

    callers = {}  # (mod, qual) -> set of caller scopes "mod:Class.Qual" or "mod:"
    work = []
    for mod, rel in pyfiles.items():
        if not rel.startswith("sqlglot/"):
            continue
        tree = ast.parse(open(os.path.join(repo, rel), encoding="utf-8").read())
        def visit_body(body, prefix, scope):
            for st in body:
                if isinstance(st, ast.ClassDef):
                    cs = f"{mod}:{prefix}{st.name}"
                    for f in calls_in(st.decorator_list + st.bases):
                        work.append((resolve(mod, f, prefix), scope))
                    visit_body(st.body, prefix + st.name + ".", cs)
                    # class-creation hooks of bases: __init_subclass__ / metaclass
                    continue
                for f in calls_in([st]):
                    work.append((resolve(mod, f, prefix), scope))
        visit_body(tree.body, "", f"{mod}:")
    # metaclass hooks & __init_subclass__: run whenever a subclass body executes
    for (mod, q), (rel, node) in defs.items():
        last = q.split(".")[-1]
        owner = ".".join(q.split(".")[:-1])
        is_meta = False
        if owner and (mod, owner) in defs:
            onode = defs[(mod, owner)][1]
            is_meta = any(isinstance(b, ast.Name) and b.id == "type" for b in getattr(onode, "bases", []))
        if last in ("__init_subclass__", "__set_name__", "__class_getitem__") or (is_meta and last in ("__new__", "__init__", "__call__")):
            work.append(((mod, q), f"{mod}:*"))
    seen = set()
    while work:
        tgt, scope = work.pop()
        if tgt is None or (tgt, scope) in seen:
            continue
        seen.add((tgt, scope))
        callers.setdefault(tgt, set()).add(scope)
        rel, node = defs[tgt]
        mod, q = tgt
        if isinstance(node, ast.ClassDef):
            # instantiating a class at import time runs its __init__
            for nm in ("__init__", "__post_init__"):
                if (mod, q + "." + nm) in defs:
                    work.append(((mod, q + "." + nm), scope))
            continue
        prefix = q.rsplit(".", 1)[0] + "." if "." in q else ""
        for f in calls_in(node.body):
            work.append((resolve(mod, f, prefix), scope))
    out = {}
    for (mod, q), scopes in callers.items():
        out.setdefault(pyfiles[mod], {})[q] = sorted(scopes)
    return out


import_time = import_time_map()
print("import-time functions:", sum(len(v) for v in import_time.values()))

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
    "import_time": import_time,
}
with gzip.open(out, "wt", encoding="utf-8") as f:
    json.dump(result, f, separators=(",", ":"))
print("tests", len(test_ids), "sets", len(sets), "files", len(lines), "bytes", os.path.getsize(out))
