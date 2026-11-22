"""Map-building plugin for the TPC-DS executor test: per-query coverage context
and per-query record of instantiated sqlglot classes."""
import concurrent.futures as cf, pickle, os, sys, json
_state = {"nodeid": None, "i": 0, "cur": None}
_used = {}


class SyncExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        self._init = (initializer, initargs); self._inited = False
    def submit(self, fn, *args, **kwargs):
        import coverage
        cov = coverage.Coverage.current()
        f = cf.Future()
        i = _state["i"]; _state["i"] += 1
        try:
            if not self._inited:
                self._inited = True
                if self._init[0] is not None:
                    self._init[0](*pickle.loads(pickle.dumps(self._init[1])))
            a, k = pickle.loads(pickle.dumps((args, kwargs)))
            if cov: cov.switch_context(f"{_state['nodeid']}|q{i}")
            _state["cur"] = _used.setdefault(i, set())
            try:
                r = fn(*a, **k)
            finally:
                _state["cur"] = None
                if cov: cov.switch_context(f"{_state['nodeid']}|run")
            f.set_result(pickle.loads(pickle.dumps(r)))
        except BaseException as e:
            f.set_exception(e)
        return f
    def shutdown(self, wait=True, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass

cf.ProcessPoolExecutor = SyncExecutor


def _wrap(cls):
    orig = cls.__init__
    def __init__(self, *a, **k):
        orig(self, *a, **k)
        s = _state["cur"]
        if s is not None:
            s.add(type(self))
            d = getattr(self, "dialect", None)
            if d is not None:
                s.add(type(d))
    cls.__init__ = __init__


def pytest_collection_finish(session):
    import importlib
    for mod, name in [("sqlglot.expressions.core", "Expr"), ("sqlglot.dialects.dialect", "Dialect"),
                      ("sqlglot.parser", "Parser"), ("sqlglot.generator", "Generator"), ("sqlglot.tokens", "Tokenizer")]:
        _wrap(getattr(importlib.import_module(mod), name))


def pytest_runtest_setup(item):
    _state["nodeid"] = item.nodeid; _state["i"] = 0


def pytest_unconfigure(config):
    key = lambda c: f"{c.__module__}:{c.__qualname__}"
    out = {str(i): sorted({key(b) for c in s for b in c.__mro__ if b.__module__.startswith("sqlglot")}) for i, s in _used.items()}
    json.dump(out, open(os.environ["QUSERS_OUT"], "w"))
