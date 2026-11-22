"""Blast radius selector.

For each patch: pick candidate tests from a coverage map built at the pinned
commit, check the affordable candidates against the patched code in forked
children, and schedule only the tests that fail (plus any candidate that is
cheaper to schedule than to check).
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import analyze  # noqa: E402
import patchlib  # noqa: E402
import runner  # noqa: E402

DATA = os.environ.get("BR_DATA", "/data")
SRC = os.environ.get("BR_SRC", "/app/sqlglot")
TMP = os.environ.get("BR_TMP", "/tmp")
MAP = os.environ.get("BR_MAP", os.path.join(HERE, "map.json.gz"))
WORKER_MS = 2000
MAX_WORKERS = 8
RUN_LIMIT_S = 4.0     # slower tests are scheduled blind unless they have a proxy
CPU_FACTOR = 1.3      # our CPU seconds per recorded test second
P_FAIL = 0.5          # prior that a checked candidate set turns out to fail
PROXY_COST = {        # CPU seconds of the cheap stand-ins in runner.PROXIES
    "tests/test_parser.py::TestParser::test_max_nodes": 0.4,
    "tests/test_generator.py::TestGenerator::test_generator_memory": 2.7,
}
CLASS_SETUP = {"tests/test_executor.py::TestExecutor": 1.4}

# A slow executor test is decided by its optimizer twin when every reason that
# selected the slow test also selected the twin (the change reaches the slow
# test only through code the twin runs too).
PROXY_BY = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": "tests/test_optimizer.py::TestOptimizer::test_tpcds",
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def pack(nums, ms):
    """Split tests over 1..8 workers to minimise busiest load + 2 s per worker."""
    nums = sorted(set(nums), key=lambda n: (-ms[n], n))
    if not nums:
        return []
    best = None
    for k in range(1, MAX_WORKERS + 1):
        loads = [0] * k
        groups = [[] for _ in range(k)]
        for n in nums:
            i = loads.index(min(loads))
            loads[i] += ms[n]
            groups[i].append(n)
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, [g for g in groups if g])
        if k >= len(nums):
            break
    return best[1]


def pack_cost(nums, ms):
    w = pack(nums, ms)
    if not w:
        return 0.0
    return (max(sum(ms[n] for n in g) for g in w) + WORKER_MS * len(w)) / 1000


class Plan:
    def __init__(self, cid, cands, files):
        self.cid = cid
        self.cands = cands
        self.files = files
        self.run = []          # ordered test numbers to check
        self.blind = set()     # scheduled without checking
        self.decided_by = {}   # slow test -> twin whose result decides it
        self.keep = {}         # test id -> query indices to run (per-query mapped tests)
        self.est = 0.0


def main():
    t0 = time.monotonic()
    tj = json.load(open(os.path.join(DATA, "tests.json")))
    tests = tj["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    by_id = {t["id"]: t["number"] for t in tests}
    by_num = {t["number"]: t["id"] for t in tests}
    with gzip.open(MAP, "rt") as f:
        m = analyze.Map(json.load(f))
    for t in tests:
        m.tests_by_file.setdefault(t["file"], set()).add(t["number"])
        if t["id"].endswith("::test_lazy_load"):
            m.always_on_import.add(t["number"])
    cdir = os.path.join(DATA, "commits")
    patches = sorted(p for p in os.listdir(cdir) if p.endswith(".patch"))
    n_commits = len(patches)
    budget = max(40.0, 3.6 * n_commits)  # the scorer allows ~4.5 s of wall time per commit
    twin = {by_id[k]: by_id[v] for k, v in PROXY_BY.items() if k in by_id and v in by_id}
    proxy_cost = {by_id[k]: v for k, v in PROXY_COST.items() if k in by_id}

    subq_tests = {by_id[k] for k in m.subq if k in by_id}

    def eff(n, p=None):
        if p is not None and by_num[n] in p.keep:
            return 1.4 + 0.5 * len(p.keep[by_num[n]])
        return proxy_cost.get(n, ms[n] / 1000)

    def cls_of(n):
        return by_num[n].rsplit("::", 1)[0]

    def est_cpu(nums, p=None):
        nums = set(nums)
        if not nums:
            return 0.0
        setup = sum(v for k, v in CLASS_SETUP.items() if any(cls_of(n) == k for n in nums))
        return 0.15 + setup + CPU_FACTOR * sum(eff(n, p) for n in nums)

    out = {}
    plans: dict[str, Plan] = {}
    for name in patches:
        cid = name[:-6]
        text = open(os.path.join(cdir, name), encoding="utf-8", errors="surrogateescape").read()
        try:
            res = analyze.analyze(text, SRC, m)
            cands = {c for c in res.cands if c in ms}
            if res.everything:
                cands = set(ms)
        except Exception as e:  # noqa: BLE001
            log(cid, "analyze failed", repr(e))
            res, cands = None, set(ms)
        if not cands:
            out[cid] = []
            log(cid, "no candidates", res.notes[:4] if res else "")
            continue
        files = {}
        try:
            for fd in patchlib.parse_patch(text):
                old = None
                if fd.old and os.path.exists(os.path.join(SRC, fd.old)):
                    old = open(os.path.join(SRC, fd.old), encoding="utf-8", errors="surrogateescape").read()
                if fd.old and fd.old != fd.new:
                    files[fd.old] = None
                if fd.new:
                    files[fd.new] = patchlib.apply(old, fd)
        except Exception as e:  # noqa: BLE001
            log(cid, "apply failed", repr(e))
            out[cid] = pack(cands, ms)
            continue
        p = Plan(cid, cands, files)
        run = set()
        for c in list(cands):
            if c in subq_tests and res is not None and c not in run:
                keep = m.query_subset(by_num[c], res, c)
                twin_ok = c in twin and all(twin[c] in r for r in res.reasons if c in r)
                if keep is not None and twin_ok and 1.4 + 0.5 * len(keep) > ms[twin[c]] / 1000:
                    keep = None  # the optimizer twin is the cheaper check
                if keep is not None:
                    if not keep:
                        # the change only touches lines this test never runs per query
                        cands.discard(c)
                        p.cands.discard(c)
                        continue
                    p.keep[by_num[c]] = sorted(keep)
                    run.add(c)
                    continue
            if ms[c] <= RUN_LIMIT_S * 1000 or c in proxy_cost:
                run.add(c)
            elif c in twin and res is not None and all(twin[c] in r for r in res.reasons if c in r):
                p.decided_by[c] = twin[c]
                run.add(twin[c])
            else:
                p.blind.add(c)
        # cheapest classes first, cheapest tests first inside a class
        groups = {}
        for c in run:
            groups.setdefault(cls_of(c), []).append(c)
        order = sorted(groups.values(), key=lambda g: (min(eff(c, p) for c in g) + CLASS_SETUP.get(cls_of(g[0]), 0)))
        p.run = [c for g in order for c in sorted(g, key=lambda c: (eff(c, p), c))]
        p.est = est_cpu(run, p)
        # schedule everything blind when that is expected to be cheaper than checking
        cost_blind = pack_cost(cands, ms)
        slow_if_fail = set(p.decided_by)
        lo = p.est + pack_cost(p.blind, ms)
        hi = p.est * 0.6 + pack_cost(p.blind | run | slow_if_fail, ms)
        cost_check = (1 - P_FAIL) * lo + P_FAIL * hi
        if not run or cost_blind <= cost_check:
            out[cid] = pack(cands, ms)
            log(cid, f"blind cands={len(cands)} blind_cost={cost_blind:.1f} check_cost={cost_check:.1f}")
            continue
        plans[cid] = p

    # Budget guard: commits that would not fit are scheduled blind instead.
    todo = sorted(plans.values(), key=lambda p: p.est)
    acc = time.monotonic() - t0 + 1.5
    specs = []
    for p in todo:
        acc += (p.est + 0.5) / 2  # two at a time
        if acc > budget:
            out[p.cid] = pack(p.cands, ms)
            log(p.cid, "blind (budget)")
            continue
        timeout = max(10.0, p.est * 3 + 8)
        specs.append((p.cid, p.files, [by_num[c] for c in p.run], timeout, p.keep))

    def final_set(p, failed, passed, done, broken=()):
        fail_nums = {by_id[n] for n in failed if n in by_id}
        extra = set()
        for pre in broken:
            # a test module / class / the package failed to load: all tests under it fail
            extra |= {n for n, i in by_num.items() if i.startswith(pre)}
        passed_nums = {by_id[n] for n in passed if n in by_id} - fail_nums
        if not done:
            # stopped early, crashed or timed out: unchecked tests are scheduled
            fail_nums |= set(p.run) - passed_nums
        final = (fail_nums & p.cands) | p.blind | extra
        for slow, tw in p.decided_by.items():
            if tw in fail_nums:
                final.add(slow)
        return final

    def should_stop(cid, failed, passed):
        p = plans[cid]
        seen = {by_id[n] for n in failed | passed if n in by_id}
        remaining = [c for c in p.run if c not in seen]
        if not remaining:
            return False
        now = final_set(p, failed, passed, True)
        if_stop = final_set(p, failed, passed, False)
        delta = pack_cost(if_stop, ms) - pack_cost(now, ms)
        rem_cpu = est_cpu(remaining, p)
        return (1 - P_FAIL) * delta < rem_cpu

    def finish(cid, result):
        failed, passed, done, broken = result
        p = plans[cid]
        final = final_set(p, failed, passed, done, broken) | set(p.cands)  # PROBE: every candidate
        out[cid] = pack(final, ms)
        log(cid, f"run={len(p.run)} est={p.est:.2f}s blind={len(p.blind)} failed={len(failed)} "
                 f"final={len(final)} done={done} t={time.monotonic()-t0:.1f}")

    if specs:
        runner.warm_imports()
        runner.run_jobs(specs, SRC, TMP, parallel=2, on_done=finish, should_stop=should_stop)

    for name in patches:
        cid = name[:-6]
        w = out.get(cid)
        if w is None:
            # never finished: schedule every candidate rather than risk a miss
            w = pack(plans[cid].cands, ms) if cid in plans else []
        print(json.dumps({"commit": cid, "workers": w}))
    sys.stdout.flush()
    log(f"total {time.monotonic()-t0:.1f}s")


if __name__ == "__main__":
    main()
