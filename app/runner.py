"""Run candidate tests against a patched copy of sqlglot in forked children.

The parent imports the heavy third-party libraries once. Each commit gets a
fresh fork with a symlinked copy of the source tree where only the patched
files are real, so the patched sqlglot is imported clean. Tests are run with
unittest directly (they are all unittest.TestCase classes), which is much
lighter than starting pytest for every commit.
"""
from __future__ import annotations

import json
import os
import select
import shutil
import signal
import sys
import time


def warm_imports():
    import concurrent.futures  # noqa: F401
    import pickle  # noqa: F401
    import unittest  # noqa: F401
    import unittest.mock  # noqa: F401
    import inspect  # noqa: F401
    import textwrap  # noqa: F401
    import tracemalloc  # noqa: F401
    for mod in ("numpy", "pandas", "pandas.testing", "duckdb", "dateutil", "pytz", "decimal", "datetime"):
        try:
            __import__(mod)
        except Exception:
            pass


def build_overlay(src_root: str, dst: str, files: dict[str, str | None]) -> None:
    """Mirror src_root at dst with symlinks; `files` maps path -> new text (None = deleted)."""
    real_dirs = set()
    for p in files:
        parts = p.split("/")
        for i in range(1, len(parts)):
            real_dirs.add("/".join(parts[:i]))
    os.makedirs(dst)

    def fill(rel: str):
        sdir = os.path.join(src_root, rel) if rel else src_root
        ddir = os.path.join(dst, rel) if rel else dst
        names = set(os.listdir(sdir)) if os.path.isdir(sdir) else set()
        for p in list(files) + list(real_dirs):
            head, _, tail = p.rpartition("/")
            if head == rel and tail:
                names.add(tail)
        for name in names:
            r = f"{rel}/{name}" if rel else name
            if r in files:
                if files[r] is not None:
                    with open(os.path.join(ddir, name), "w", encoding="utf-8", errors="surrogateescape") as f:
                        f.write(files[r])
            elif r in real_dirs:
                os.makedirs(os.path.join(ddir, name), exist_ok=True)
                fill(r)
            else:
                os.symlink(os.path.join(sdir, name), os.path.join(ddir, name))

    fill("")


class SyncExecutor:
    """In-process stand-in for ProcessPoolExecutor that still pickles both ways."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        self._init = (initializer, initargs)
        self._inited = False

    def submit(self, fn, *args, **kwargs):
        import concurrent.futures as cf
        import pickle

        f = cf.Future()
        try:
            if not self._inited:
                self._inited = True
                if self._init[0] is not None:
                    self._init[0](*pickle.loads(pickle.dumps(self._init[1])))
            a, k = pickle.loads(pickle.dumps((args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def map(self, fn, *its, **kw):
        return [self.submit(fn, *a).result() for a in zip(*its)]

    def shutdown(self, wait=True, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.shutdown()


# Cheaper stand-ins for very slow tests: same code paths, smaller inputs.
# Each is a list of (marker, old, new): `old` is replaced by `new` in the part
# of the method source after `marker` ("" = whole source). If any replacement
# does not apply (the patch changed the test), the real test runs instead.
PROXIES = {
    "tests/test_parser.py::TestParser::test_max_nodes": [
        ("", "width = 10000", "width = 100"),
        ("width = 100", "max_nodes=1_000_000", "max_nodes=10_000"),
        ("", "max_nodes=50_000", "max_nodes=5_000"),
        ("", '"(50000)"', '"(5000)"'),
    ],
    "tests/test_generator.py::TestGenerator::test_generator_memory": [
        ("", "range(1000)", "range(100)"),
    ],
}


def _make_proxy(cls, meth, repl):
    import inspect
    import textwrap

    fn = getattr(cls, meth)
    fn = getattr(fn, "__func__", fn)
    src = textwrap.dedent(inspect.getsource(fn))
    for marker, old, new in repl:
        if marker:
            i = src.find(marker)
            if i < 0:
                return None
            head, tail = src[: i + len(marker)], src[i + len(marker):]
            if old not in tail:
                return None
            src = head + tail.replace(old, new, 1)
        else:
            if old not in src:
                return None
            src = src.replace(old, new, 1)
    ns: dict = {}
    g = sys.modules[cls.__module__].__dict__
    exec(compile(src, f"<proxy {meth}>", "exec"), g, ns)
    return ns.get(meth)


def _child(overlay: str, nodeids: list[str], wfd: int):
    out = os.fdopen(wfd, "w", buffering=1)

    def emit(kind, nid):
        out.write(json.dumps([kind, nid]) + "\n")

    try:
        import concurrent.futures as cf
        import importlib
        import unittest

        cf.ProcessPoolExecutor = SyncExecutor
        # never write bytecode: a cached .pyc of a patched file must not leak
        # into another commit (same size and mtime second would reuse it)
        sys.dont_write_bytecode = True
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        os.chdir(overlay)
        sys.path.insert(0, overlay)
        os.environ["PYTHONPATH"] = overlay
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)

        # group by class, keep file order
        by_class: dict[tuple[str, str], list[str]] = {}
        for nid in nodeids:
            path, cls, meth = nid.split("::")
            by_class.setdefault((path, cls), []).append(meth)

        try:
            import sqlglot  # noqa: F401
            import sqlglot.dialects  # noqa: F401
        except BaseException:  # noqa: BLE001
            emit("A", "")  # the package itself no longer imports: everything fails
            emit("D", "")
            return

        id_of = {}
        suite = unittest.TestSuite()
        for (path, cls_name), meths in by_class.items():
            modname = path[:-3].replace("/", ".")
            try:
                mod = importlib.import_module(modname)
                cls = getattr(mod, cls_name)
            except BaseException:  # noqa: BLE001
                emit("M", path)  # the test module does not import: all its tests fail
                for mth in meths:
                    emit("F", f"{path}::{cls_name}::{mth}")
                continue
            for mth in meths:
                nid = f"{path}::{cls_name}::{mth}"
                if nid in PROXIES:
                    try:
                        proxy = _make_proxy(cls, mth, PROXIES[nid])
                    except Exception:  # noqa: BLE001
                        proxy = None
                    if proxy is not None:
                        setattr(cls, mth, proxy)
                        emit("X", nid)
                try:
                    tc = cls(mth)
                except BaseException:  # noqa: BLE001
                    emit("F", nid)
                    continue
                id_of[id(tc)] = nid
                suite.addTest(tc)

        class Rec(unittest.TestResult):
            def __init__(self):
                super().__init__()
                self.bad = set()

            def _mark(self, test):
                nid = id_of.get(id(test))
                if nid is None:
                    # class/module fixture failure (_ErrorHolder): every test of that class fails
                    desc = str(getattr(test, "description", test))
                    for v in set(id_of.values()):
                        path_, cls_name, _ = v.split("::")
                        if cls_name in desc:
                            emit("K", f"{path_}::{cls_name}")
                            if v not in self.bad:
                                self.bad.add(v)
                                emit("F", v)
                    return
                if nid not in self.bad:
                    self.bad.add(nid)
                    emit("F", nid)

            def addError(self, test, err):
                self._mark(test)

            def addFailure(self, test, err):
                self._mark(test)

            def addSubTest(self, test, subtest, err):
                if err is not None:
                    self._mark(test)

            def addUnexpectedSuccess(self, test):
                self._mark(test)

            def stopTest(self, test):
                nid = id_of.get(id(test))
                if nid is not None and nid not in self.bad:
                    emit("P", nid)

        suite.run(Rec())
        emit("D", "")
    except BaseException as e:  # noqa: BLE001
        try:
            emit("E", repr(e))
        except Exception:
            pass
    finally:
        try:
            out.flush()
        except Exception:
            pass
        os._exit(0)


class Job:
    def __init__(self, key, src_root, tmp_root, files, nodeids, timeout):
        self.key = key
        self.overlay = os.path.join(tmp_root, f"w-{key}")
        shutil.rmtree(self.overlay, ignore_errors=True)
        build_overlay(src_root, self.overlay, files)
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            try:
                os.setpgid(0, 0)
            except Exception:
                pass
            _child(self.overlay, nodeids, w)
        os.close(w)
        self.pid, self.fd = pid, r
        self.deadline = time.monotonic() + timeout
        self.buf = b""
        self.failed, self.passed, self.proxied = set(), set(), set()
        self.broken = set()  # prefixes of test ids that all fail ("" = everything)
        self.done = False
        self.closed = False
        self.stopped = False

    def feed(self):
        chunk = os.read(self.fd, 65536)
        if not chunk:
            self.close()
            return
        self.buf += chunk
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            try:
                kind, nid = json.loads(line)
            except Exception:
                continue
            if kind == "F":
                self.failed.add(nid)
                self.passed.discard(nid)
            elif kind == "P":
                self.passed.add(nid)
            elif kind == "X":
                self.proxied.add(nid)
            elif kind in ("M", "K", "A"):
                self.broken.add(nid if kind != "A" else "")
            elif kind == "D":
                self.done = True

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except Exception:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except Exception:
                pass
        self.cpu = 0.0
        try:
            _, _, ru = os.wait4(self.pid, 0)
            self.cpu = ru.ru_utime + ru.ru_stime
        except Exception:
            pass
        os.close(self.fd)
        shutil.rmtree(self.overlay, ignore_errors=True)


def run_jobs(specs, src_root, tmp_root, parallel=2, on_done=None, should_stop=None):
    """specs: list of (key, files, nodeids, timeout). Runs up to `parallel` at once."""
    pending = list(specs)
    running: list[Job] = []
    results = {}
    while pending or running:
        while pending and len(running) < parallel:
            key, files, nodeids, timeout = pending.pop(0)
            try:
                running.append(Job(key, src_root, tmp_root, files, nodeids, timeout))
            except Exception as e:  # noqa: BLE001
                print(f"job {key} failed to start: {e!r}", file=sys.stderr)
                results[key] = (set(), set(), False, set())
        now = time.monotonic()
        wait = max(0.0, min(j.deadline for j in running) - now) if running else 0
        ready, _, _ = select.select([j.fd for j in running], [], [], wait)
        for j in list(running):
            if j.fd in ready:
                n_before = len(j.failed)
                j.feed()
                if should_stop and len(j.failed) > n_before and not j.closed and not j.done:
                    if should_stop(j.key, j.failed, j.passed):
                        j.stopped = True
                        j.close()
            if j.closed or time.monotonic() >= j.deadline:
                j.close()
                running.remove(j)
                results[j.key] = (j.failed, j.passed, j.done, j.broken)
                print(f"job {j.key} cpu={j.cpu:.2f}", file=sys.stderr)
                if on_done:
                    on_done(j.key, results[j.key])
    return results
