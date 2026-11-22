"""Map-building plugin for executor tests: coverage context per query (submit call)."""
import concurrent.futures as cf, pickle, os, sys
_state = {"nodeid": None, "i": 0}

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
            r = fn(*a, **k)
            if cov: cov.switch_context(f"{_state['nodeid']}|run")
            f.set_result(pickle.loads(pickle.dumps(r)))
        except BaseException as e:
            if cov: cov.switch_context(f"{_state['nodeid']}|run")
            f.set_exception(e)
        return f
    def shutdown(self, wait=True, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass

cf.ProcessPoolExecutor = SyncExecutor

def pytest_runtest_setup(item):
    _state["nodeid"] = item.nodeid; _state["i"] = 0
