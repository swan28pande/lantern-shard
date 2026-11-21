"""pytest plugin used only when building maps: switches coverage context per test,
records files opened per test and per-test wall time."""
import json, os, sys, time
import coverage

_cur = {"id": None}
_opens = {}
_times = {}
ROOT = os.getcwd()

def _audit(event, args):
    if event == "open" and _cur["id"]:
        p = args[0]
        if isinstance(p, (str, bytes)):
            p = os.fsdecode(p)
            if not os.path.isabs(p):
                p = os.path.join(os.getcwd(), p)
            p = os.path.normpath(p)
            if p.startswith(ROOT + "/") and not p.endswith((".pyc",)):
                _opens.setdefault(_cur["id"], set()).add(os.path.relpath(p, ROOT))

sys.addaudithook(_audit)

def pytest_runtest_protocol(item, nextitem):
    cov = coverage.Coverage.current()
    _cur["id"] = item.nodeid
    if cov:
        cov.switch_context(item.nodeid)
    item._t0 = time.perf_counter()

def pytest_runtest_logfinish(nodeid, location):
    pass

def pytest_runtest_teardown(item, nextitem):
    pass

def pytest_runtest_makereport(item, call):
    if call.when == "teardown":
        _times[item.nodeid] = time.perf_counter() - item._t0

def pytest_sessionfinish(session):
    out = os.environ["MAP_OUT"]
    with open(out, "w") as f:
        json.dump({"opens": {k: sorted(v) for k, v in _opens.items()}, "times": _times}, f)


# ---- run process pools in-process so per-test coverage sees the work they do
import concurrent.futures as _cf
import pickle as _pickle


class _SyncExecutor(_cf.Executor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        self._init = (initializer, initargs)
        self._inited = False

    def submit(self, fn, *args, **kwargs):
        f = _cf.Future()
        try:
            if not self._inited and self._init[0] is not None:
                ini, iargs = self._init
                ini(*_pickle.loads(_pickle.dumps(iargs)))
            self._inited = True
            args, kwargs = _pickle.loads(_pickle.dumps((args, kwargs)))
            f.set_result(_pickle.loads(_pickle.dumps(fn(*args, **kwargs))))
        except BaseException as e:
            f.set_exception(e)
        return f

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


def pytest_collection_modifyitems(session, config, items):
    for mod in list(sys.modules.values()):
        name = getattr(mod, "__name__", "")
        if name.startswith("tests.") and getattr(mod, "ProcessPoolExecutor", None) is _cf.ProcessPoolExecutor:
            mod.ProcessPoolExecutor = _SyncExecutor
