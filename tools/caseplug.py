"""Map-building plugin: coverage context per fixture case.

Wraps load_sql_fixture_pairs / load_sql_fixtures in test modules so that while
a test iterates a fixture file, coverage is recorded under
"<nodeid>|c:<file>:<index>". Work done after the loop (e.g. check_file's
result comparison) stays under "<nodeid>|run".
"""
import sys
import concurrent.futures as cf
import pickle

_state = {"nodeid": None}


class SyncExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        self._init = (initializer, initargs); self._inited = False
    def submit(self, fn, *args, **kwargs):
        f = cf.Future()
        try:
            if not self._inited:
                self._inited = True
                if self._init[0] is not None:
                    self._init[0](*pickle.loads(pickle.dumps(self._init[1])))
            a, k = pickle.loads(pickle.dumps((args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:
            f.set_exception(e)
        return f
    def shutdown(self, wait=True, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass

cf.ProcessPoolExecutor = SyncExecutor


def _wrap(orig):
    def wrapper(filename, *a, **k):
        import coverage
        cov = coverage.Coverage.current()
        nid = _state["nodeid"]
        try:
            for i, item in enumerate(orig(filename, *a, **k)):
                if cov and nid:
                    cov.switch_context(f"{nid}|c:{filename}:{i}")
                yield item
        finally:
            if cov and nid:
                cov.switch_context(f"{nid}|run")
    wrapper.__wrapped__ = orig
    return wrapper


def pytest_collection_finish(session):
    for name, mod in list(sys.modules.items()):
        if name.startswith("tests.") and name != "tests.helpers" and "executor" not in name:
            for fn in ("load_sql_fixture_pairs", "load_sql_fixtures"):
                if hasattr(mod, fn) and not hasattr(getattr(mod, fn), "__wrapped__"):
                    setattr(mod, fn, _wrap(getattr(mod, fn)))


def pytest_runtest_setup(item):
    _state["nodeid"] = item.nodeid
