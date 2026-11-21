"""Map a patch to candidate tests using the maps built from the pinned commit."""

from __future__ import annotations

import ast
import os
import re

from patchlib import FilePatch
from scopes import Scope

REPO = os.environ.get("SQLGLOT_DIR", "/opt/sqlglot")


class Maps:
    def __init__(self, data: dict, num_of_id: dict[str, int]):
        self.sets = data["sets"]
        # translate map-test indices to tests.json numbers once
        mid = [num_of_id.get(t) for t in data["test_ids"]]
        self._set_cache: dict[int, frozenset] = {}
        self._mid = mid
        self.lines = data["lines"]  # file -> {line: set idx}
        self.classes = data["classes"]  # "module:qualname" -> set idx
        self.opens = data["opens"]  # file -> set idx
        self.scopes = data["scopes"]  # file -> list of tuples
        self.refs = data["refs"]  # name -> list[(file, line)]
        self.file_tests = data["file_tests"]  # file -> set idx (any runtime line)
        self.class_lines = data.get("class_tests", {})  # "file:qualname" -> set idx
        self.import_time = data.get("import_time", {})  # file -> {func qualname: [caller scopes]}

    def tests(self, idx) -> frozenset:
        if idx is None:
            return frozenset()
        r = self._set_cache.get(idx)
        if r is None:
            r = frozenset(n for n in (self._mid[i] for i in self.sets[idx]) if n is not None)
            self._set_cache[idx] = r
        return r

    def line_tests(self, file: str, line: int) -> frozenset:
        return self.tests(self.lines.get(file, {}).get(line))

    def range_tests(self, file: str, start: int, end: int) -> set:
        out: set = set()
        fl = self.lines.get(file, {})
        seen = set()
        for ln in range(start, end + 1):
            idx = fl.get(ln)
            if idx is not None and idx not in seen:
                seen.add(idx)
                out |= self.tests(idx)
        return out

    def scope_list(self, file: str) -> list[Scope] | None:
        raw = self.scopes.get(file)
        if raw is None:
            return None
        out: list[Scope] = []
        for kind, name, start, end, body_start, parent in raw:
            out.append(Scope(kind, name, start, end, body_start, out[parent] if parent >= 0 else None))
        return out


def _ast_equal(a: str, b: str) -> bool | None:
    try:
        return ast.dump(ast.parse(a)) == ast.dump(ast.parse(b))
    except SyntaxError:
        return None


def _innermost(scopes: list[Scope], line: int) -> Scope:
    best = scopes[0]
    for s in scopes[1:]:
        if s.start <= line <= s.end and (s.end - s.start) <= (best.end - best.start):
            best = s
    return best


def _module_of(rel: str) -> str:
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


TEST_NAMES: dict = {}  # test number -> method name (set by the selector)
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Analysis:
    def __init__(self):
        self.candidates: set[int] = set()  # tests that may fail
        self.direct: set[int] = set()  # tests executing a changed line
        self.everything = False  # could not narrow down
        self.notes: list[str] = []
        self.syntax_error = False
        self.broad: set[int] = set()  # tests executing a changed function anywhere
        self.narrow: set[int] = set()  # tests reaching the changed statements (expression-level edits)
        self.class_or_module = False
        self.import_time = False
        self.data_readers: set[int] = set()
        self.test_names: dict = {}


def analyze(fps: list[FilePatch], changes: dict, maps: Maps, all_tests: set[int], test_file_of: dict[int, str]) -> Analysis:
    an = Analysis()
    an.test_names = TEST_NAMES
    for fp in fps:
        path = fp.path
        old_path = fp.old_path
        if old_path and path.endswith(".py") and (path.startswith("sqlglot/") or path.startswith("tests/")):
            _analyze_py(fp, changes, maps, an, all_tests, test_file_of)
        else:
            _analyze_data(fp, maps, an, all_tests)
    an.candidates |= an.narrow
    an.broad |= an.candidates
    return an


def _analyze_data(fp: FilePatch, maps: Maps, an: Analysis, all_tests: set[int]):
    for p in {fp.old_path, fp.new_path} - {None}:
        idx = maps.opens.get(p)
        if idx is not None:
            t = maps.tests(idx)
            an.candidates |= t
            an.direct |= t
            an.data_readers |= {n for n in t if not _tpc_mismatch(p, n, an)}
            an.notes.append(f"data {p}: {len(t)} readers")
        else:
            an.notes.append(f"data {p}: no readers")


def _analyze_py(fp, changes, maps: Maps, an: Analysis, all_tests, test_file_of):
    rel = fp.old_path
    try:
        with open(os.path.join(REPO, rel), encoding="utf-8") as f:
            old = f.read()
    except FileNotFoundError:
        old = None
    new = changes.get(fp.path)
    if old is not None and new is not None:
        eq = _ast_equal(old, new)
        if eq:
            an.notes.append(f"{rel}: AST unchanged")
            # still: tests that read this source file as data
            _readers(rel, maps, an)
            return
        if eq is None:
            an.syntax_error = True
            an.notes.append(f"{rel}: syntax error")
    scopes = maps.scope_list(rel)
    if scopes is None or old is None:
        an.everything = True
        an.notes.append(f"{rel}: unknown file")
        return
    _readers(rel, maps, an)

    is_test_file = rel.startswith("tests/")
    lines = fp.touched_old_lines()
    trees: dict = {}
    module_level_names: set[str] = set()
    class_level: list[tuple[Scope, int]] = []
    for ln in lines:
        s = _innermost(scopes, ln)
        direct = maps.line_tests(rel, ln)
        an.direct |= direct
        an.candidates |= direct
        if s.kind == "func":
            callers = maps.import_time.get(rel, {}).get(s.name)
            if callers:
                # runs while modules/classes are being created: its effect lands in whatever
                # those class/module bodies build, so their users are at risk
                users = _scope_users(callers, maps)
                an.candidates |= users
                an.import_time = True
                an.notes.append(f"{rel}: import-time {s.name} -> {len(users)} users")
            # whole function: anything executing it may see the change
            func_tests = maps.range_tests(rel, s.body_start, s.end)
            an.broad |= func_tests
            if ln < s.body_start:
                # signature / decorator: callers are the function's users
                an.candidates |= maps.range_tests(rel, s.start, s.end)
                an.narrow |= maps.range_tests(rel, s.start, s.end)
                continue
            rng = None
            if new is not None:
                rng = _statement_range(old, new, s.name, ln, trees)
            if rng is None:
                an.narrow |= func_tests
                an.notes.append(f"{rel}:{ln} func-level {s.name}")
            else:
                an.narrow |= maps.range_tests(rel, rng[0], rng[1])
        elif s.kind == "class":
            class_level.append((s, ln))
            an.class_or_module = True
        else:
            an.class_or_module = True
            module_level_names.add("")
    if an.syntax_error:
        _broad(rel, maps, an, all_tests, test_file_of)
        return

    if class_level or module_level_names:
        # names (re)defined by the changed class/module-level statements
        try:
            touched = set(lines)
            names = _assigned_names(old, touched) | _assigned_names(new, set(fp_new_lines(fp)))
        except SyntaxError:
            names = set()
        an.notes.append(f"{rel}: level names {sorted(names)[:8]}")
        class_users: set = set()
        for s, ln in class_level:
            qual = s.name
            key = f"{_module_of(rel)}:{qual}"
            idx = maps.classes.get(key)
            users = set(maps.tests(idx)) if idx is not None else set()
            # tests running any method of the class
            users |= maps.range_tests(rel, s.start, s.end)
            if idx is None:
                # class never instantiated directly (e.g. mixin / nested holder): fall back to its
                # outer class's users
                outer = s.parent
                while outer is not None and outer.kind == "class":
                    oidx = maps.classes.get(f"{_module_of(rel)}:{outer.name}")
                    if oidx is not None:
                        users |= maps.tests(oidx)
                        break
                    outer = outer.parent
            class_users |= users
            an.notes.append(f"{rel}: class {qual} users {len(users)}")
        an.candidates |= class_users
        ref_tests: set = set()
        for name in names:
            refs = maps.refs.get(name, ())
            if len(refs) > 400:
                continue
            for f2, l2 in refs:
                ref_tests |= maps.line_tests(f2, l2)
        if module_level_names:
            an.candidates |= ref_tests
        else:
            # class attribute: only tests that use the class (or a subclass) can see it
            an.candidates |= ref_tests & class_users
        if module_level_names:
            # module level statement: users of the module
            an.candidates |= set(maps.tests(maps.file_tests.get(rel)))
            if is_test_file or not names:
                _broad(rel, maps, an, all_tests, test_file_of)
    if is_test_file:
        # a test module's own tests are always candidates when it changes at class level
        for s, ln in class_level:
            pass


def fp_new_lines(fp: FilePatch) -> list[int]:
    out = []
    for h in fp.hunks:
        n = h.new_start
        for ln in h.lines:
            c = ln[:1]
            if c == " ":
                n += 1
            elif c == "+":
                out.append(n)
                n += 1
    return out


def _assigned_names(src: str, lines: set[int]) -> set[str]:
    from scopes import assigned_names_at

    return assigned_names_at(src, lines)


def _readers(rel: str, maps: Maps, an: Analysis):
    idx = maps.opens.get(rel)
    if idx is not None:
        an.candidates |= maps.tests(idx)


def _broad(rel: str, maps: Maps, an: Analysis, all_tests, test_file_of):
    """Import-time breakage: anything that imports the module may fail."""
    if rel.startswith("tests/"):
        mod = _module_of(rel)
        an.candidates |= {n for n in all_tests if test_file_of[n] == rel}
        # test modules importing this one (e.g. tests.dialects.test_dialect.Validator)
        imp = maps.opens.get("__importers__:" + mod)
        if imp is not None:
            an.candidates |= maps.tests(imp)
        return
    an.candidates |= set(maps.tests(maps.file_tests.get(rel)))
    imp = maps.opens.get("__importers__:" + _module_of(rel))
    if imp is not None:
        an.candidates |= maps.tests(imp)
    else:
        an.everything = True


def _find_func(tree, qualname):
    parts = qualname.split(".")
    body = tree.body
    node = None
    for p in parts:
        nxt = None
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and st.name == p:
                nxt = st
        if nxt is None:
            # nested function inside a function body: search recursively
            for st in ast.walk(node) if node is not None else ():
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and st.name == p and st is not node:
                    nxt = st
                    break
        if nxt is None:
            return None
        node = nxt
        body = nxt.body
    return node


def _func_signature(fn):
    stores, flags = set(), set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            stores.add(n.id)
        elif isinstance(n, ast.arg):
            stores.add(n.arg)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            flags.update(("g:" + x) for x in n.names)
        elif isinstance(n, (ast.Yield, ast.YieldFrom)):
            flags.add("yield")
        elif isinstance(n, (ast.Await,)):
            flags.add("await")
        elif isinstance(n, (ast.Return,)) and n.value is not None:
            flags.add("retval")
    return stores, flags


def _statement_range(old_src, new_src, qualname, line, trees):
    """Line range of the innermost statement (header for compound ones) containing `line`,
    if the edit to the function is expression-level only; else None."""
    try:
        if "old" not in trees:
            trees["old"] = ast.parse(old_src)
        if "new" not in trees:
            trees["new"] = ast.parse(new_src)
    except SyntaxError:
        return None
    fo = _find_func(trees["old"], qualname)
    fn = _find_func(trees["new"], qualname)
    if fo is None or fn is None:
        return None
    if _func_signature(fo) != _func_signature(fn):
        return None
    # same number of statements overall: an inserted/removed statement is not expression-level
    if sum(isinstance(x, ast.stmt) for x in ast.walk(fo)) != sum(isinstance(x, ast.stmt) for x in ast.walk(fn)):
        return None
    best = None
    for st in ast.walk(fo):
        if not isinstance(st, ast.stmt) or st is fo:
            continue
        end = st.end_lineno or st.lineno
        if st.lineno <= line <= end:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            body = getattr(st, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
                hdr_end = body[0].lineno - 1
                if isinstance(st, ast.If) or isinstance(st, (ast.For, ast.While, ast.With, ast.AsyncFor, ast.AsyncWith)):
                    if line > hdr_end:
                        # inside the body: a deeper statement will match; also else-branches
                        continue
                    rng = (st.lineno, max(st.lineno, hdr_end))
                else:
                    continue  # try/except: the deeper statement decides
            else:
                rng = (st.lineno, end)
            if best is None or (rng[1] - rng[0]) <= (best[1] - best[0]):
                best = rng
    if best is None:
        return None
    # if/elif chain: an elif header is an If nested in orelse; its range is its own header.
    return best


def _scope_users(scopes, maps: Maps) -> set:
    out: set = set()
    for sc in scopes:
        mod, _, q = sc.partition(":")
        if q == "*" or q == "":
            idx = maps.opens.get("__importers__:" + mod)
            if idx is not None:
                out |= maps.tests(idx)
            rel = mod.replace(".", "/") + ".py"
            out |= maps.tests(maps.file_tests.get(rel))
        else:
            idx = maps.classes.get(f"{mod}:{q}")
            if idx is not None:
                out |= maps.tests(idx)
            else:
                idx = maps.opens.get("__importers__:" + mod)
                if idx is not None:
                    out |= maps.tests(idx)
    return out


def _tpc_mismatch(path: str, n: int, an: Analysis) -> bool:
    """tpc-h data/queries are not read by the tpc-ds tests and vice versa (they share a setUpClass)."""
    name = an.test_names.get(n, "")
    if "tpc-h" in path and "tpcds" in name:
        return True
    if "tpc-ds" in path and "tpch" in name:
        return True
    return False
