"""Map-building plugin: which executor ENV functions each test (and each TPC-DS query of
the executor test) calls."""
import concurrent.futures as cf, pickle, os, json
_state = {"nodeid": None, "q": None}
_by_test, _by_query = {}, {}


class SyncExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        self._init = (initializer, initargs); self._inited = False; self._i = 0
    def submit(self, fn, *args, **kwargs):
        f = cf.Future()
        i = self._i; self._i += 1
        try:
            if not self._inited:
                self._inited = True
                if self._init[0] is not None:
                    self._init[0](*pickle.loads(pickle.dumps(self._init[1])))
            a, k = pickle.loads(pickle.dumps((args, kwargs)))
            _state["q"] = i if _state["nodeid"] and _state["nodeid"].endswith("test_execute_tpcds") else None
            try:
                r = fn(*a, **k)
            finally:
                _state["q"] = None
            f.set_result(pickle.loads(pickle.dumps(r)))
        except BaseException as e:
            f.set_exception(e)
        return f
    def shutdown(self, wait=True, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass

cf.ProcessPoolExecutor = SyncExecutor


def _wrap(name, fn):
    def w(*a, **k):
        if _state["nodeid"]:
            _by_test.setdefault(_state["nodeid"], set()).add(name)
        if _state["q"] is not None:
            _by_query.setdefault(_state["q"], set()).add(name)
        return fn(*a, **k)
    return w


def pytest_collection_finish(session):
    from sqlglot.executor import env
    for k, v in list(env.ENV.items()):
        if callable(v):
            env.ENV[k] = _wrap(k, v)


def pytest_runtest_setup(item):
    _state["nodeid"] = item.nodeid


def pytest_unconfigure(config):
    json.dump({"tests": {k: sorted(v) for k, v in _by_test.items()},
               "queries": {str(k): sorted(v) for k, v in _by_query.items()}}, open(os.environ["ENV_OUT"], "w"))
