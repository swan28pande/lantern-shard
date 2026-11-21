"""Run selected sqlglot tests against a patched overlay, in forked children.

The parent process preloads third-party modules (and optionally sqlglot plus
the test modules) once.  For every commit a child is forked: it writes the
patched files into /tmp, redirects imports and open() of patched files there,
purges stale modules, runs the requested unittest methods and reports each
test's outcome through a pipe.  A test that runs past its time limit is
treated as failing (the labels count pytest timeouts as failures) and the
remaining tests continue in a fresh child.
"""

from __future__ import annotations

import importlib
import os
import resource
import select
import signal
import sys
import time

REPO = os.environ.get("SQLGLOT_DIR", "/opt/sqlglot")
TMP = os.environ.get("BR_TMP", "/tmp/br")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- child side


def setup_overlay(cid: str, changes: dict) -> None:
    """changes: repo-relative path -> new content (None = deleted)."""
    import json

    import brhook

    root = os.path.join(TMP, cid)
    files, deleted = {}, []
    for rel, content in changes.items():
        if content is None:
            deleted.append(rel)
            continue
        dst = os.path.join(root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(content)
        files[rel] = dst
    manifest = {"files": files, "deleted": deleted}
    mpath = os.path.join(root, "manifest.json")
    os.makedirs(root, exist_ok=True)
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    os.environ["BR_OVERLAY"] = mpath
    brhook.install(manifest)


def _sync_executor_class():
    import concurrent.futures as cf
    import pickle

    class SyncExecutor(cf.Executor):
        """Runs submitted work in-process (no spawned interpreters), keeping pickling semantics."""

        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
            self._init = (initializer, initargs)
            self._inited = False

        def submit(self, fn, *args, **kwargs):
            f = cf.Future()
            try:
                if not self._inited and self._init[0] is not None:
                    ini, iargs = self._init
                    ini(*pickle.loads(pickle.dumps(iargs)))
                self._inited = True
                args, kwargs = pickle.loads(pickle.dumps((args, kwargs)))
                f.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
            except BaseException as e:
                f.set_exception(e)
            return f

        def shutdown(self, wait=True, *, cancel_futures=False):
            pass

    return SyncExecutor


def child_run(cid: str, changes: dict, test_ids: list[tuple[int, str]], wfd: int, abort_on_fail=frozenset(), probe_only=frozenset()) -> None:
    """Runs in the forked child. Writes 'S n', 'P n', 'F n' lines to wfd."""
    import unittest

    out = os.fdopen(wfd, "w", buffering=1)
    prof = os.environ.get("BR_PROFILE")
    t0 = time.process_time()
    os.chdir(REPO)
    try:
        setup_overlay(cid, changes)
    except Exception as e:  # overlay failure -> report every test failed
        log("overlay failure", cid, repr(e))
        for n, _ in test_ids:
            out.write(f"F {n}\n")
        out.flush()
        os._exit(0)

    # group by (module, class)
    plan: list[tuple[str, str, list[tuple[int, str]]]] = []
    index: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for n, tid in test_ids:
        parts = tid.split("::")
        file = parts[0]
        modname = file[:-3].replace("/", ".")
        cls = parts[1] if len(parts) > 2 else ""
        meth = parts[-1]
        key = (modname, cls)
        if key not in index:
            index[key] = []
            plan.append((modname, cls, index[key]))
        index[key].append((n, meth))

    sync_exec = _sync_executor_class()
    current = {"n": None}

    class Result(unittest.TestResult):
        def __init__(self):
            super().__init__()
            self.bad: set[str] = set()
            self.class_error = False

        def _mark(self, test):
            mid = getattr(test, "_testMethodName", None)
            if mid is None:
                # _ErrorHolder for setUpClass/tearDownClass/module failures
                self.class_error = True
            else:
                self.bad.add(mid)
            n = current["n"]
            if n is not None and n in abort_on_fail:
                # the verdict is in: stop paying for the rest of this test
                out.write(f"F {n}\n")
                out.flush()
                os._exit(0)

        def addError(self, test, err):
            self._mark(test)

        def addFailure(self, test, err):
            self._mark(test)

        def addSubTest(self, test, subtest, err):
            if err is not None:
                self._mark(test)

        def addUnexpectedSuccess(self, test):
            self._mark(test)

    for modname, cls, items in plan:
        out.write("C 0\n")
        try:
            mod = importlib.import_module(modname)
            if "ProcessPoolExecutor" in vars(mod):
                mod.ProcessPoolExecutor = sync_exec
            klass = getattr(mod, cls) if cls else None
            if prof:
                log(f"[{cid}] t={time.process_time() - t0:.3f} imported {modname}")
            if klass is not None:
                klass.setUpClass()
            if prof:
                log(f"[{cid}] t={time.process_time() - t0:.3f} setUpClass {cls}")
        except BaseException as e:  # import / class setup failure: all of these fail
            log(f"[{cid}] setup {modname}.{cls} failed: {type(e).__name__}: {e}")
            for n, _ in items:
                out.write(f"F {n}\n")
            continue
        for n, meth in items:
            if n in probe_only:
                # setup succeeded and this test is not otherwise at risk
                out.write(f"P {n}\n")
                continue
            out.write(f"S {n}\n")
            current["n"] = n
            ru0 = resource.getrusage(resource.RUSAGE_SELF)
            ok = True
            try:
                if klass is None:
                    getattr(mod, meth)()
                else:
                    res = Result()
                    klass(meth).run(res)
                    if res.bad or res.class_error:
                        ok = False
            except BaseException as e:
                log(f"[{cid}] {n} raised {type(e).__name__}: {e}")
                ok = False
            ru1 = resource.getrusage(resource.RUSAGE_SELF)
            used = (ru1.ru_utime - ru0.ru_utime) + (ru1.ru_stime - ru0.ru_stime)
            current["n"] = None
            out.write(f"{'P' if ok else 'F'} {n} {used:.4f}\n")
        out.write("C 0\n")
        if klass is not None:
            try:
                klass.tearDownClass()
                klass.doClassCleanups()
            except BaseException as e:
                log(f"[{cid}] teardown {modname}.{cls}: {type(e).__name__}: {e}")
    if prof:
        log(f"[{cid}] t={time.process_time() - t0:.3f} done")
    out.flush()
    out.close()
    os._exit(0)


# --------------------------------------------------------------------------- parent side


class Job:
    def __init__(self, cid, changes, tests, limits):
        self.cid = cid
        self.changes = changes
        self.pending = list(tests)  # [(num, id)]
        self.limits = limits  # num -> seconds
        self.failed: set[int] = set()
        self.passed: set[int] = set()
        self.pid = None
        self.fd = None
        self.buf = b""
        self.current = None
        self.started = 0.0
        self.spawn_count = 0
        self.cpu: dict[int, float] = {}
        self.abort_on_fail: frozenset = frozenset()
        self.probe_only: frozenset = frozenset()
        self.child_cpu = 0.0

    def done(self):
        return not self.pending and self.pid is None


def _spawn(job: Job):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            # keep stdout clean: the child must never print to the real stdout
            os.dup2(2, 1)
            child_run(job.cid, job.changes, job.pending, w, job.abort_on_fail, job.probe_only)
        except BaseException as e:
            try:
                log("child crash", repr(e))
            finally:
                os._exit(1)
        os._exit(0)
    os.close(w)
    job.pid, job.fd, job.buf = pid, r, b""
    job.current = None
    job.started = time.monotonic()
    job.spawn_count += 1


def _reap(job: Job, kill=False):
    if job.pid is None:
        return
    if kill:
        try:
            os.kill(job.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        _, _, ru = os.wait4(job.pid, 0)
        job.child_cpu += ru.ru_utime + ru.ru_stime
    except ChildProcessError:
        pass
    try:
        os.close(job.fd)
    except OSError:
        pass
    job.pid = job.fd = None


def run_jobs(jobs: list[Job], parallel: int = 2, setup_grace: float = 40.0) -> None:
    queue = list(jobs)
    active: list[Job] = []
    while queue or active:
        while queue and len(active) < parallel:
            j = queue.pop(0)
            if not j.pending:
                continue
            _spawn(j)
            active.append(j)
        if not active:
            break
        now = time.monotonic()
        # compute nearest deadline
        timeout = 0.5
        for j in active:
            lim = j.limits.get(j.current, 5.0) if j.current is not None else setup_grace
            timeout = min(timeout, max(0.01, j.started + lim - now))
        rl, _, _ = select.select([j.fd for j in active], [], [], timeout)
        now = time.monotonic()
        for j in list(active):
            if j.fd in rl:
                data = os.read(j.fd, 65536)
                if not data:
                    # child exited
                    _reap(j)
                    if j.current is not None:
                        # died mid-test (crash / segfault): count as failing
                        j.failed.add(j.current)
                        j.pending = [t for t in j.pending if t[0] != j.current]
                        j.current = None
                    if j.pending and j.spawn_count < 50:
                        # child ended without finishing (crash between tests): retry rest
                        _spawn(j)
                    else:
                        for n, _ in j.pending:
                            j.failed.add(n)
                        j.pending = []
                        active.remove(j)
                    continue
                j.buf += data
                *lines, j.buf = j.buf.split(b"\n")
                for line in lines:
                    if not line:
                        continue
                    parts = line.split()
                    kind, num = parts[0], int(parts[1])
                    if len(parts) > 2:
                        j.cpu[num] = float(parts[2])
                    if kind == b"S":
                        j.current = num
                        j.started = time.monotonic()
                    elif kind == b"C":
                        j.current = None
                        j.started = time.monotonic()
                    else:
                        (j.failed if kind == b"F" else j.passed).add(num)
                        j.pending = [t for t in j.pending if t[0] != num]
                        j.current = None
                        j.started = time.monotonic()
            else:
                lim = j.limits.get(j.current, 5.0) if j.current is not None else setup_grace
                if now - j.started > lim:
                    log(f"[{j.cid}] timeout on {j.current} after {now - j.started:.1f}s")
                    _reap(j, kill=True)
                    if j.current is not None:
                        j.failed.add(j.current)
                        j.pending = [t for t in j.pending if t[0] != j.current]
                    else:
                        for n, _ in j.pending:
                            j.failed.add(n)
                        j.pending = []
                    j.current = None
                    if j.pending:
                        _spawn(j)
                    else:
                        active.remove(j)


def preload(full: bool = True, test_modules=()) -> None:
    """Import heavy third-party modules (and the pristine sqlglot + tests) once."""
    t0 = time.process_time()
    import unittest  # noqa: F401
    import unittest.mock  # noqa: F401
    import concurrent.futures  # noqa: F401
    import multiprocessing  # noqa: F401
    import sqlite3  # noqa: F401
    import json  # noqa: F401
    import decimal  # noqa: F401
    import datetime  # noqa: F401
    import tempfile  # noqa: F401

    for m in ("duckdb", "pandas", "pandas.testing", "numpy"):
        try:
            importlib.import_module(m)
        except Exception as e:
            log("preload failed", m, e)
    t1 = time.process_time()
    if full:
        if REPO not in sys.path:
            sys.path.insert(0, REPO)
        os.chdir(REPO)
        import sqlglot  # noqa: F401

        for m in sorted(test_modules):
            try:
                importlib.import_module(m)
            except BaseException as e:
                log("preload failed", m, repr(e))
    log(f"preload third-party {t1 - t0:.2f}s, sqlglot {time.process_time() - t1:.2f}s")
