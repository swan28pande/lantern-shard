"""pytest plugin (map building only): records which sqlglot classes each test instantiates."""
import json, os

_cur = {"id": None}
_used = {}
_seen_types = {}


def _names(cls):
    r = _seen_types.get(cls)
    if r is None:
        r = tuple(
            f"{c.__module__}:{c.__qualname__}"
            for c in cls.__mro__
            if getattr(c, "__module__", "").startswith("sqlglot")
        )
        _seen_types[cls] = r
    return r


def _rec(cls):
    tid = _cur["id"]
    if tid is not None:
        s = _used.get(tid)
        if s is None:
            s = _used[tid] = set()
        s.add(cls)


def _wrap(klass):
    orig = klass.__init__

    def __init__(self, *a, **k):
        _rec(type(self))
        return orig(self, *a, **k)

    __init__.__wrapped__ = orig
    klass.__init__ = __init__


def pytest_configure(config):
    from sqlglot.expressions.core import Expr
    from sqlglot.dialects.dialect import Dialect, _Dialect
    from sqlglot.tokens import Tokenizer
    from sqlglot.parser import Parser
    from sqlglot.generator import Generator
    from sqlglot.schema import Schema

    for k in (Expr, Dialect, Tokenizer, Parser, Generator):
        _wrap(k)
    # class lookups of dialects (Dialect["x"], Dialect.get) count as use too
    raw = _Dialect.__dict__["get"]
    if isinstance(raw, classmethod):
        fn = raw.__func__

        def get(cls, key, default=None):
            r = fn(cls, key, default)
            if isinstance(r, type):
                _rec(r)
            return r

        _Dialect.get = classmethod(get)
    else:
        def get(self, key, default=None):
            r = raw(self, key, default)
            if isinstance(r, type):
                _rec(r)
            return r

        _Dialect.get = get


def pytest_runtest_protocol(item, nextitem):
    _cur["id"] = item.nodeid


def pytest_sessionfinish(session):
    out = os.environ["MAP_OUT"]
    res = {}
    for tid, classes in _used.items():
        names = set()
        for c in classes:
            names.update(_names(c))
        res[tid] = sorted(names)
    with open(out, "w") as f:
        json.dump(res, f)
