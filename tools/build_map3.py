"""coverage data + opens + class usage -> app/map.json.gz keyed by test number.

- lines:  file -> line -> tests that ran it (setup/teardown lines count for the whole class)
- static: file -> lines run at import time
- opens:  file -> tests that opened it (-1 = opened while collecting)
- users:  "module:Qual.Name" -> tests that instantiated that class or a subclass
- reads:  name -> file -> lines that read that name (attribute or variable),
          for names assigned at class or module level somewhere
"""
import ast, collections, glob, gzip, json, os, sys
from coverage import CoverageData

root, tests_json, out, covdir = sys.argv[1:5]
tests = json.load(open(tests_json))["tests"]
num = {t["id"]: t["number"] for t in tests}
by_cls = collections.defaultdict(set)
for t in tests:
    by_cls[t["id"].rsplit("::", 1)[0]].add(t["number"])

lines = collections.defaultdict(lambda: collections.defaultdict(set))
static = collections.defaultdict(set)
for cf in glob.glob(os.path.join(covdir, "*", ".coverage")):
    d = CoverageData(cf); d.read()
    for f in d.measured_files():
        rel = os.path.relpath(f, root)
        for ln, ctxs in d.contexts_by_lineno(f).items():
            for c in ctxs:
                if c == "":
                    static[rel].add(ln); continue
                tid, _, phase = c.rpartition("|")
                if tid not in num:
                    continue
                if phase == "run":
                    lines[rel][ln].add(num[tid])
                else:
                    lines[rel][ln] |= by_cls[tid.rsplit("::", 1)[0]]

# the alphabetically first test of a class also runs the class's setUpClass, so a file
# it opens may be needed by every test in the class
first_of_class = {}
for t in sorted(num):
    first_of_class.setdefault(t.rsplit("::", 1)[0], t)
opens = collections.defaultdict(set)
for f in glob.glob(os.path.join(covdir, "*", "opens-*.json")):
    for t, ps in json.load(open(f)).items():
        for p in ps:
            if t in num:
                opens[p].add(num[t])
                cls = t.rsplit("::", 1)[0]
                if first_of_class.get(cls) == t:
                    opens[p] |= by_cls[cls]
            elif t == "<collect>": opens[p].add(-1)

users = collections.defaultdict(set)
for f in glob.glob(os.path.join(covdir, "*", "classes-*.json")):
    data = json.load(open(f))
    mro = data["mro"]
    for t, keys in data["by_test"].items():
        if t not in num:
            continue
        for k in keys:
            for b in mro.get(k, [k]):
                users[b].add(num[t])

# names assigned at class/module level anywhere, and where they are read
assigned = set()
trees = {}
for base in ("sqlglot", "tests"):
    for p in glob.glob(os.path.join(root, base, "**", "*.py"), recursive=True):
        rel = os.path.relpath(p, root)
        try:
            trees[rel] = ast.parse(open(p, encoding="utf-8").read())
        except SyntaxError:
            continue
for rel, tree in trees.items():
    stack = [tree]
    while stack:
        node = stack.pop()
        for ch in getattr(node, "body", []):
            if isinstance(ch, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                tgts = ch.targets if isinstance(ch, ast.Assign) else [ch.target]
                for tg in tgts:
                    for n in ast.walk(tg):
                        if isinstance(n, ast.Name):
                            assigned.add(n.id)
            elif isinstance(ch, ast.ClassDef):
                assigned.add(ch.name)
                stack.append(ch)
            elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assigned.add(ch.name)
            elif isinstance(ch, (ast.If, ast.Try)):
                stack.append(ch)
reads = collections.defaultdict(lambda: collections.defaultdict(list))


def base_ident(v):
    """Dotted path of an attribute's object: a.b.C -> "a.b.C", self -> "self", other -> "?"."""
    parts = []
    while isinstance(v, ast.Attribute):
        parts.append(v.attr)
        v = v.value
    if isinstance(v, ast.Name):
        parts.append(v.id)
        return ".".join(reversed(parts))
    return "?"


for rel, tree in trees.items():
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load) and n.attr in assigned:
            reads[n.attr][rel].append([n.lineno, base_ident(n.value)])
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in assigned:
            reads[n.id][rel].append([n.lineno, ""])
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("getattr", "hasattr")
              and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str)
              and n.args[1].value in assigned):
            reads[n.args[1].value][rel].append([n.lineno, base_ident(n.args[0])])

mro = json.load(open(os.path.join(covdir, "mro.json")))

# test method / class-body line ranges, so the selector can read a test's text cheaply
tranges = {}
for t in tests:
    f, cname, meth = t["id"].split("::")
    tree = trees.get(f)
    if tree is None:
        continue
    for c in tree.body:
        if isinstance(c, ast.ClassDef) and c.name == cname:
            meth_rng = None
            other = []
            for ch in c.body:
                if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if ch.name == meth:
                        meth_rng = [ch.lineno, ch.end_lineno]
                else:
                    other.append([ch.lineno, ch.end_lineno])
            if meth_rng:
                tranges[str(t["number"])] = [meth_rng, other]
subq = json.load(open("/home/claude/br/covq/subq_map.json")) if os.path.exists("/home/claude/br/covq/subq_map.json") else {}
res = {
    "tranges": tranges,
    "subq": subq,
    "lines": {f: {str(l): sorted(s) for l, s in dd.items()} for f, dd in lines.items()},
    "static": {f: sorted(s) for f, s in static.items()},
    "opens": {p: sorted(s) for p, s in opens.items()},
    "users": {k: sorted(s) for k, s in users.items()},
    "reads": {k: {f: sorted(set(map(tuple, ls))) for f, ls in v.items()} for k, v in reads.items()},
    "mro": mro,
}
with gzip.open(out, "wt") as fh:
    json.dump(res, fh, separators=(",", ":"))
print("files", len(res["lines"]), "opens", len(opens), "classes", len(users), "names", len(reads))
