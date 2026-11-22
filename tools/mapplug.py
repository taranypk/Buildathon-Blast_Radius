"""pytest plugin used only when building the map.

- Runs ProcessPoolExecutor work in-process (pickling args/results both ways) so
  coverage sees it and attributes it to the right test.
- Clears known caches before each test so cached work is attributed to every
  test that depends on it.
- Records files opened during each test.
"""
import concurrent.futures as cf
import json, os, pickle, sys

class SyncExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        self._init = (initializer, initargs)
        self._inited = False
    def _ensure(self):
        if not self._inited:
            self._inited = True
            if self._init[0] is not None:
                a = pickle.loads(pickle.dumps(self._init[1]))
                self._init[0](*a)
    def submit(self, fn, *args, **kwargs):
        f = cf.Future()
        try:
            self._ensure()
            a, k = pickle.loads(pickle.dumps((args, kwargs)))
            r = fn(*a, **k)
            f.set_result(pickle.loads(pickle.dumps(r)))
        except BaseException as e:
            f.set_exception(e)
        return f
    def map(self, fn, *its, **kw):
        return [self.submit(fn, *a).result() for a in zip(*its)]
    def shutdown(self, wait=True, **kw):
        pass
    def __enter__(self): return self
    def __exit__(self, *a): self.shutdown()

cf.ProcessPoolExecutor = SyncExecutor

ROOT = os.environ.get("BR_ROOT", os.getcwd())
_cur = {"t": "<collect>"}
_rec = {}
def _hook(ev, args):
    if ev == "open" and args and isinstance(args[0], str):
        p = os.path.abspath(args[0])
        if p.startswith(ROOT) and "__pycache__" not in p and not p.endswith((".pyc",)):
            _rec.setdefault(_cur["t"], set()).add(os.path.relpath(p, ROOT))
sys.addaudithook(_hook)

def pytest_runtest_setup(item):
    _cur["t"] = item.nodeid
    g = sys.modules.get("sqlglot.generator")
    if g is not None and hasattr(g, "_DISPATCH_CACHE"):
        g._DISPATCH_CACHE.clear()
    cls = getattr(item, "cls", None)
    if cls is not None and isinstance(getattr(cls, "cache", None), dict):
        cls.cache.clear()

def pytest_sessionfinish(session):
    with open(os.path.join(os.environ["BR_OUT"], f"opens-{os.getpid()}.json"), "w") as f:
        json.dump({k: sorted(v) for k, v in _rec.items()}, f)

# ---- class usage: which sqlglot classes (and dialects) each test instantiates
_used = {"cur": None, "by_test": {}, "mro": {}}


def _key(c):
    return f"{c.__module__}:{c.__qualname__}"


def _note(obj):
    s = _used["cur"]
    if s is None:
        return
    for o in (obj, getattr(obj, "dialect", None)):
        if o is None:
            continue
        c = type(o)
        if c not in s:
            s.add(c)


def _wrap(cls):
    orig = cls.__init__

    def __init__(self, *a, **k):
        orig(self, *a, **k)
        _note(self)

    __init__.__wrapped__ = orig
    cls.__init__ = __init__


def pytest_collection_finish(session):
    import importlib
    targets = [("sqlglot.expressions.core", "Expr"), ("sqlglot.dialects.dialect", "Dialect"),
               ("sqlglot.parser", "Parser"), ("sqlglot.generator", "Generator"), ("sqlglot.tokens", "Tokenizer")]
    for mod, name in targets:
        try:
            _wrap(getattr(importlib.import_module(mod), name))
        except Exception as e:
            print("mapplug: cannot wrap", mod, name, e, file=sys.stderr)


def pytest_runtest_logstart(nodeid, location):
    _used["cur"] = _used["by_test"].setdefault(nodeid, set())


def pytest_runtest_logfinish(nodeid, location):
    _used["cur"] = None


def pytest_unconfigure(config):
    out = os.environ.get("BR_OUT")
    if not out:
        return
    by_test = {}
    for t, s in _used["by_test"].items():
        keys = []
        for c in s:
            k = _key(c)
            keys.append(k)
            if k not in _used["mro"]:
                _used["mro"][k] = [_key(b) for b in c.__mro__ if b.__module__.startswith("sqlglot")]
        by_test[t] = sorted(keys)
    with open(os.path.join(out, f"classes-{os.getpid()}.json"), "w") as f:
        json.dump({"by_test": by_test, "mro": _used["mro"]}, f)


# ---- cache clearing so lazily-cached work is attributed to every test that needs it
import weakref as _weakref
_schemas = _weakref.WeakSet()


def _install_cache_hooks():
    try:
        import sqlglot.schema as sch
        orig = sch.MappingSchema.__init__

        def __init__(self, *a, **k):
            orig(self, *a, **k)
            _schemas.add(self)

        sch.MappingSchema.__init__ = __init__
    except Exception as e:
        print("mapplug: schema hook failed", e, file=sys.stderr)


def _clear_caches():
    try:
        import sqlglot.dialects as dpkg
        from sqlglot.dialects.dialect import _Dialect
        std = set(dpkg.DIALECT_MODULE_NAMES)
        for k in list(_Dialect._classes):
            if k in std:
                del _Dialect._classes[k]
        for name in list(dpkg.MODULE_BY_DIALECT):
            dpkg.__dict__.pop(name, None)
    except Exception as e:
        print("mapplug: dialect cache clear failed", e, file=sys.stderr)
    for s in list(_schemas):
        for k, v in list(vars(s).items()):
            if k.endswith("_cache") and isinstance(v, dict):
                v.clear()


_prev_collection_finish = pytest_collection_finish


def pytest_collection_finish(session):  # noqa: F811
    _prev_collection_finish(session)
    _install_cache_hooks()


def pytest_runtest_call(item):
    _clear_caches()
