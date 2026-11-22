"""Turn a patch into candidate test numbers using the coverage map.

Code inside functions: the tests that ran the changed statement.
Code that runs at import time (class bodies, module level, def headers):
the tests that use the class (instantiated it or a subclass, or ran its code)
and also run a line that reads the changed name; when the name feeds other
import-time code (metaclasses, subclasses merging the dict), every user of
the class and of the code it feeds.
"""
from __future__ import annotations

import ast
import os

import patchlib

FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)
DEF = FUNC + (ast.ClassDef,)
GENERIC = {"self", "cls", "klass", "kls", "mcs", "metacls", "this"}
# classes whose instances the map-building plugin records (see tools/mapplug.py)
TRACKED_BASES = {
    "sqlglot.expressions.core:Expr",
    "sqlglot.dialects.dialect:Dialect",
    "sqlglot.parser:Parser",
    "sqlglot.generator:Generator",
    "sqlglot.tokens:Tokenizer",
}


def cls_key_file(key):
    return key.split(":", 1)[0].replace(".", "/") + ".py"


def imports_name(m, f, name, src_root=None):
    cache = m.__dict__.setdefault("_imported", {})
    if f not in cache:
        tree = m.tree(src_root, f) if src_root else m._trees.get(f)
        if tree is None:
            return True
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                names.update(a.asname or a.name for a in n.names)
        cache[f] = names
    return name in cache[f]


class Map:
    def __init__(self, data):
        self.lines = {f: {int(l): set(s) for l, s in d.items()} for f, d in data["lines"].items()}
        self.static = {f: set(v) for f, v in data["static"].items()}
        self.opens = data["opens"]
        self.users = {k: set(v) for k, v in data.get("users", {}).items()}
        self.reads = data.get("reads", {})
        self.subq = data.get("subq", {})
        self.mro = data.get("mro", {})
        self.class_names = {k.split(":", 1)[1].split(".")[-1] for k in self.mro}
        self._trees = {}
        self._desc = {}
        self._chain = {}
        self.tests_by_file = {}  # filled by the caller: test file path -> test numbers
        self.always_on_import = set()  # filled by the caller: tests run in a subprocess
        self.id_of = {}  # filled by the caller: test number -> test id
        self.tranges = data.get("tranges", {})
        self._ttext = {}
        self._flines = {}
        self._fixture_text = {}
        self._opened_by = None
        self.by_simple = {}
        for k in self.users:
            self.by_simple.setdefault(k.split(":", 1)[1].split(".")[-1], []).append(k)

    def users_named(self, words):
        """Users of classes whose simple name appears in `words` (case-insensitive)."""
        out = set()
        for w in words:
            for k in self.by_simple.get(w, ()):
                out |= self.users[k]
        return out

    def desc_names(self, key):
        """Simple names of `key` and every class that inherits from it."""
        if key not in self._desc:
            self._desc[key] = {k.split(":", 1)[1].split(".")[-1] for k, mro in self.mro.items() if key in mro}
            self._desc[key].add(key.split(":", 1)[1].split(".")[-1])
        return self._desc[key]

    def tests_on(self, f, lines):
        d = self.lines.get(f, {})
        out = set()
        for l in lines:
            out |= d.get(l, set())
        return out

    def tests_in_range(self, f, lo, hi):
        d = self.lines.get(f, {})
        out = set()
        for l, s in d.items():
            if lo <= l <= hi:
                out |= s
        return out

    def tests_in_file(self, f):
        out = set()
        for s in self.lines.get(f, {}).values():
            out |= s
        return out

    def readers(self, name):
        out = set()
        for f, ls in self.reads.get(name, {}).items():
            out |= self.tests_on(f, [l for l, _ in ls])
        return out

    def readers_in(self, name, files):
        out = set()
        for f, ls in self.reads.get(name, {}).items():
            if f in files:
                out |= self.tests_on(f, [l for l, _ in ls])
        return out

    def static_reads(self, name, skip, cls_key=None, module=None):
        """(file, line) of import-time code that may read the changed `name`.

        cls_key: the class whose attribute changed; reads through another class
        (not this one or a subclass) or through self/cls (same class, already
        covered) are ignored. module: the module whose global changed.
        """
        res = []
        for f, ls in self.reads.get(name, {}).items():
            st = self.static.get(f, set())
            for l, base in ls:
                if l not in st or (f, l) in skip:
                    continue
                if base.split(".")[0] in GENERIC:
                    continue
                if cls_key is not None:
                    if base == "":
                        if f != cls_key_file(cls_key):
                            continue
                    elif not self.may_be_subclass(base, cls_key):
                        continue
                else:
                    if self.match_classes(base):
                        continue
                    if base == "" and module and f != module and not imports_name(self, f, name):
                        continue
                res.append((f, l))
        return res

    def query_subset(self, test_id, res, num):
        """Queries of a per-query-mapped test (TPC-DS) that run the changed lines, or None
        when the change can reach the test some other way (then run or schedule it whole)."""
        sq = self.subq.get(test_id)
        if not sq:
            return None
        keep = set()
        for s, where in zip(res.reasons, res.where):
            if num not in s:
                continue
            if where is None:
                return None
            path, lines = where
            ql = sq["q"].get(path, {})
            outside = set(sq["outside"].get(path, []))
            for l in lines:
                if num not in self.lines.get(path, {}).get(l, ()):
                    continue
                if l in outside:
                    return None
                if str(l) not in ql:
                    return None  # covered by the test but not seen per query: be safe
                keep |= set(ql[str(l)])
        return keep

    def test_text(self, src_root, n):
        """Lower-cased text of test n's method, its class body and the fixtures it opens;
        None when it should never be filtered out."""
        if n in self._ttext:
            return self._ttext[n]
        tid = self.id_of.get(n)
        rng = self.tranges.get(str(n))
        if tid is None or rng is None:
            self._ttext[n] = None
            return None
        f = tid.split("::")[0]
        if f not in self._flines:
            try:
                self._flines[f] = open(os.path.join(src_root, f), encoding="utf-8").read().split("\n")
            except Exception:  # noqa: BLE001
                self._flines[f] = None
        lines = self._flines[f]
        if lines is None:
            self._ttext[n] = None
            return None
        (ms, me), other = rng
        parts = ["\n".join(lines[ms - 1 : me])] + ["\n".join(lines[a - 1 : b]) for a, b in other]
        text = "\n".join(parts).lower()
        if "dialects" in text or ".classes" in text or "for name in" in text:
            self._ttext[n] = None  # iterates over every dialect / mapping: keep it
            return None
        self._ttext[n] = (text, tuple(self._opened().get(n, ())))
        return self._ttext[n]

    def _fixture(self, src_root, p):
        if p not in self._fixture_text:
            try:
                self._fixture_text[p] = open(os.path.join(src_root, p), encoding="utf-8", errors="replace").read().lower()
            except Exception:  # noqa: BLE001
                self._fixture_text[p] = ""
        return self._fixture_text[p]

    def _opened(self):
        if self._opened_by is None:
            self._opened_by = {}
            for p, ts in self.opens.items():
                for t in ts:
                    if t >= 0:
                        self._opened_by.setdefault(t, []).append(p)
        return self._opened_by

    def key_filter(self, src_root, tests, keys):
        out = set()
        hit = {}
        for t in tests:
            tt = self.test_text(src_root, t)
            if tt is None:
                out.add(t)
                continue
            text, fixtures = tt
            if any(k in text for k in keys):
                out.add(t)
                continue
            for p in fixtures:
                if p not in hit:
                    ft = self._fixture(src_root, p)
                    hit[p] = any(k in ft for k in keys)
                if hit[p]:
                    out.add(t)
                    break
        return out

    def case_subset(self, casemap, test_id, res, num, fixture_changes):
        """Fixture cases of a per-case-mapped test that a change can reach, or None when the
        change can reach the test outside its case loop (then run the whole test)."""
        cm = casemap.get(test_id)
        if not cm:
            return None
        names = cm["cases"]
        keep = set()
        for s, where in zip(res.reasons, res.where):
            if num not in s:
                continue
            if where is None:
                return None
            if where[0] == "open":
                fx = where[1]
                rel = fx[len("tests/fixtures/"):] if fx.startswith("tests/fixtures/") else None
                changed = fixture_changes.get(fx)
                if rel is None or changed is None:
                    return None
                keep |= {i for i, n in enumerate(names) if n.rsplit(":", 1)[0] == rel and int(n.rsplit(":", 1)[1]) in changed}
                continue
            path, lines = where
            cl = cm["c"].get(path, {})
            outside = set(cm["outside"].get(path, []))
            for l in lines:
                if num not in self.lines.get(path, {}).get(l, ()):
                    continue
                if l in outside or str(l) not in cl:
                    return None
                keep |= set(cl[str(l)])
        return {names[i] for i in keep}

    def generic_static_read(self, name):
        """Is `name` read at import time through self/cls (a metaclass deriving other data)?"""
        for f, ls in self.reads.get(name, {}).items():
            st = self.static.get(f, set())
            for l, base in ls:
                if l in st and base.split(".")[0] in GENERIC:
                    return True
        return False

    def match_classes(self, chain):
        """Class keys a dotted reference such as `tokens.Tokenizer` or `Postgres.Tokenizer` may name."""
        if chain in self._chain:
            return self._chain[chain]
        parts = chain.split(".")
        strict, loose = [], []
        for k in self.mro:
            mod, qual = k.split(":", 1)
            q = qual.split(".")
            if len(parts) >= len(q) and parts[-len(q):] == q:
                rest = parts[: -len(q)]
                # prefer matches whose prefix looks like the class's module
                if not rest or rest[-1] == mod.split(".")[-1] or ".".join(rest) == mod:
                    strict.append(k)
                elif len(q) == 1 or len(rest) <= 1:
                    loose.append(k)  # package re-export alias such as exp.Select
        out = strict or loose
        self._chain[chain] = out
        return out

    def may_be_subclass(self, chain, cls_key):
        keys = self.match_classes(chain)
        if not keys:
            return True  # unknown reference: be conservative
        return any(cls_key in self.mro.get(k, [k]) for k in keys)

    def tree(self, src_root, f):
        if f not in self._trees:
            try:
                with open(os.path.join(src_root, f), encoding="utf-8") as fh:
                    self._trees[f] = ast.parse(fh.read())
            except Exception:
                self._trees[f] = None
        return self._trees[f]


def _walk(tree):
    """(stmt, chain) for every statement, chain = enclosing nodes from the module down."""
    stack = [(tree, [])]
    while stack:
        node, chain = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                yield child, chain + [node]
                stack.append((child, chain + [node]))
            elif isinstance(child, (ast.excepthandler, ast.match_case)):
                stack.append((child, chain + [node]))


def innermost(tree, line):
    best = None
    for node, chain in _walk(tree):
        if node.lineno <= line <= (node.end_lineno or node.lineno):
            if best is None or len(chain) >= len(best[1]):
                best = (node, chain)
    return best


def header_range(node):
    """Lines that belong to the statement itself rather than its nested body."""
    lo = node.lineno
    if isinstance(node, DEF):
        lo = min([d.lineno for d in node.decorator_list] + [lo])
    body = getattr(node, "body", None)
    if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
        return lo, max(lo, body[0].lineno - 1)
    return lo, node.end_lineno or node.lineno


def module_name(path):
    mod = path[:-3].replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    return mod


def assigned_names(node):
    names = set()
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        tgts = node.targets if isinstance(node, ast.Assign) else [node.target]
        for tg in tgts:
            for n in ast.walk(tg):
                if isinstance(n, ast.Name):
                    names.add(n.id)
                elif isinstance(n, ast.Attribute):
                    names.add(n.attr)
    elif isinstance(node, DEF):
        names.add(node.name)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            names.add((a.asname or a.name).split(".")[0])
    return names


def _words(text):
    """Identifiers spelled inside string literals on this line ("Tableau" in a registry)."""
    import re

    out = set()
    for lit in re.findall(r"\"([^\"\\]*)\"|'([^'\\]*)'", text):
        for s in lit:
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{2,}", s):
                out.add(s)
    return out


def entry_keys(node, line, old_lines, new_lines):
    """For a changed entry inside a class/module-level dict/list/set literal, the string
    literals on that line (lower-cased). None when the filter should not apply."""
    if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
        return None
    if not isinstance(node.value, (ast.Dict, ast.Set, ast.List, ast.Tuple)):
        return None
    if line <= node.lineno:
        return None  # the assignment line itself
    import re

    text = old_lines[line - 1] if line - 1 < len(old_lines) else ""
    lits = [(a or b).strip() for a, b in re.findall(r"\"([^\"\\]*)\"|'([^'\\]*)'", text)]
    # only SQL keywords / function names: upper-case literals of 3+ characters
    if not lits or any(len(k) < 3 or k != k.upper() or not re.search(r"[A-Z]", k) for k in lits):
        return None
    return {k.lower() for k in lits}


def sql_keyed(m, path, chain):
    """Is this statement in a tokenizer or parser class (whose tables are keyed by SQL text)?"""
    classes = [c for c in chain if isinstance(c, ast.ClassDef)]
    if not classes:
        return False
    q = module_name(path) + ":" + ".".join(c.name for c in classes)
    mro = m.mro.get(q, ())
    return "sqlglot.tokens:Tokenizer" in mro or "sqlglot.parser:Parser" in mro


def is_docstring(node):
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


class Result:
    def __init__(self):
        self.cands: set[int] = set()
        self.reasons: list[set[int]] = []
        self.where: list = []  # per reason: (path, lines) when it came from line coverage
        self.notes: list[str] = []
        self.everything = False
        # failures here depend on test order (shared registries): never trust a partial check
        self.no_check = False

    def add(self, s, where=None):
        s = set(s)
        if s:
            self.reasons.append(s)
            self.where.append(where)
            self.cands |= s


def class_scope(m, path, chain_classes):
    """Tests that use this class: instantiated it (or a subclass), or ran its code."""
    cls = chain_classes[-1]
    q = module_name(path) + ":" + ".".join(c.name for c in chain_classes)
    users = m.users.get(q)
    ran = m.tests_in_range(path, cls.lineno, cls.end_lineno or cls.lineno)
    if users is None and any(b in TRACKED_BASES for b in m.mro.get(q, ())):
        users = set()  # instances of this family are recorded: none were ever created
    if users is not None:
        return users | ran, q, True
    return ran, q, False


def _module_users(m, path):
    pre = module_name(path) + ":"
    out = set()
    for k, v in m.users.items():
        if k.startswith(pre):
            out |= v
    return out


def static_effect(m, src_root, path, node, chain, res, depth=0, seen=None):
    """Tests affected by an import-time statement `node` (old tree) in `path`."""
    seen = seen if seen is not None else set()
    key = (path, node.lineno)
    if key in seen or depth > 4:
        return set()
    seen.add(key)
    if is_docstring(node):
        return set()
    classes = [c for c in chain if isinstance(c, ast.ClassDef)]
    names = assigned_names(node)
    if isinstance(node, FUNC):
        # decorator / signature / default change: whoever runs the function
        out = m.tests_in_range(path, node.lineno, node.end_lineno or node.lineno)
        if classes:
            # adding/renaming a method can change dispatch (e.g. *_sql lookups)
            scope, q, _ = class_scope(m, path, classes)
            readers = m.readers(node.name)
            out |= scope & readers
        return out
    if classes:
        scope, q, tracked = class_scope(m, path, classes)
        if isinstance(node, ast.ClassDef):
            res.notes.append(f"class-header {q}")
            return scope
        if not names:
            res.notes.append(f"class-stmt {q}")
            return scope
        stat = []
        for n in names:
            stat += m.static_reads(n, {(path, node.lineno)}, cls_key=q)
        readers = set()
        for n in names:
            readers |= m.readers(n)
        if stat or any(m.generic_static_read(n) for n in names):
            # the value feeds other import-time code (metaclass, subclass dicts)
            res.notes.append(f"class-attr-derived {q}.{','.join(sorted(names))}")
            out = set(scope) if tracked else set(scope) | readers
            if any(m.generic_static_read(n) for n in names):
                # metaclass code copies it onto this class and the class that encloses it
                # (a dialect's nested Tokenizer feeds the dialect's own settings)
                out |= _owner_users(m, q)
            for f, l in stat:
                out |= _static_line(m, src_root, f, l, res, depth, seen, owner=q)
            return out
        res.notes.append(f"class-attr {q}.{','.join(sorted(names))} tracked={tracked}")
        if tracked:
            # instances are recorded: users of the class that also read the name
            return (scope & readers) if readers else scope
        # enums, schema/scope/executor classes: no instance record, so every reader
        return readers | scope
    # module level
    if isinstance(node, ast.ClassDef):
        scope, q, _ = class_scope(m, path, [node])
        res.notes.append(f"class-header {q}")
        return scope
    if not names:
        res.notes.append(f"module-stmt {path}:{node.lineno}")
        return m.tests_in_file(path) | _module_users(m, path) | m.tests_by_file.get(path, set())
    out = set()
    if path.startswith("tests/") and os.path.basename(path).startswith("test_"):
        # module level of a test file: every test in it (import errors fail them all)
        out |= m.tests_by_file.get(path, set())
    if path.startswith("sqlglot/"):
        # tests that import sqlglot in a subprocess (lazy-loading checks) see no coverage
        out |= m.always_on_import
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        # an import binds names in this module only
        for n in names:
            out |= m.readers_in(n, {path})
        out |= m.tests_in_file(path) | _module_users(m, path)
        res.notes.append(f"module-import {path}")
        return out
    for n in names:
        for f, ls in m.reads.get(n, {}).items():
            same_or_imported = f == path or imports_name(m, f, n, src_root)
            lines = []
            for l, base in ls:
                if base == "":
                    if same_or_imported:
                        lines.append(l)
                elif base.split(".")[0] not in GENERIC and not m.match_classes(base):
                    lines.append(l)  # module.attr (sqlglot.pretty, alias.NAME)
            out |= m.tests_on(f, lines)
    for n in names:
        for f, l in m.static_reads(n, {(path, node.lineno)}, module=path):
            out |= _static_line(m, src_root, f, l, res, depth, seen)
    res.notes.append(f"module-attr {path}:{','.join(sorted(names))}")
    return out


def _owner_users(m, key):
    """Users of the classes that enclose a nested class (Dialect for Dialect.Tokenizer)."""
    mod, qual = key.split(":", 1)
    parts = qual.split(".")
    out = set()
    for i in range(1, len(parts)):
        out |= m.users.get(mod + ":" + ".".join(parts[:i]), set())
    return out


def import_time_func_effect(m, src_root, path, node, chain, res):
    fn = [c for c in chain if isinstance(c, FUNC)][-1]
    out = m.tests_in_range(path, fn.lineno, fn.end_lineno or fn.lineno)
    names = assigned_names(node)
    attr_names = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)}
    if not attr_names:
        # a condition/loop line: everything the function assigns onto objects
        attr_names = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)}
    for n in attr_names:
        out |= m.readers(n)
    # import-time call sites of this function (class bodies calling a factory)
    seen = set()
    for f, l in m.static_reads(fn.name, set()):
        out |= _static_line(m, src_root, f, l, res, 0, seen)
    res.notes.append(f"import-time-func {path}:{fn.name} attrs={len(attr_names)} locals={len(names)}")
    return out


def _static_line(m, src_root, f, line, res, depth, seen, owner=None):
    tree = m.tree(src_root, f)
    if tree is None:
        return m.tests_in_file(f)
    hit = innermost(tree, line)
    if hit is None:
        return set()
    node, chain = hit
    fns = [c for c in chain[1:] if isinstance(c, FUNC)]
    if fns:
        # import-time code inside a function (metaclass, decorator, factory)
        if owner is not None:
            # it derives values for the class being built (or its enclosing class)
            return _owner_users(m, owner)
        cls = [c for c in chain if isinstance(c, ast.ClassDef)]
        fn = fns[-1]
        out = m.tests_in_range(f, fn.lineno, fn.end_lineno or fn.lineno)
        if cls:
            scope, q, _ = class_scope(m, f, cls)
            out |= scope
        else:
            out |= _module_users(m, f)
        return out
    return static_effect(m, src_root, f, node, chain, res, depth + 1, seen)


def analyze(patch_text, src_root, m: Map, n_tests=None) -> Result:
    res = Result()
    for fd in patchlib.parse_patch(patch_text):
        path = fd.path
        old_text = None
        if fd.old:
            p = os.path.join(src_root, fd.old)
            if os.path.exists(p):
                old_text = open(p, encoding="utf-8", errors="surrogateescape").read()
        for p in {fd.old, fd.new} - {None}:
            openers = m.opens.get(p, ())
            res.add((t for t in openers if t >= 0), where=("open", p))
            if -1 in openers:
                res.notes.append(f"collect-opens {p}")
        if not path.endswith(".py"):
            if not (path.startswith("sqlglot/") or path.startswith("tests/")):
                res.notes.append(f"other file {path}")
            continue
        if fd.old is None or old_text is None:
            res.notes.append(f"new py {path}")
            # tests that scan this directory's files (read them as text)
            d = os.path.dirname(path) + "/"
            res.add(t for p, ts in m.opens.items() if p.startswith(d) and "/" not in p[len(d):] for t in ts if t >= 0)
            if os.path.basename(path) == "conftest.py":
                res.everything = True
            continue
        if fd.new is not None and fd.new != fd.old:
            # renamed/moved module: anything importing the old name breaks
            res.notes.append(f"renamed {fd.old} -> {fd.new}")
            res.everything = True
            continue
        new_text = patchlib.apply(old_text, fd) if fd.new else None
        if new_text is not None and patchlib.same_ast(old_text, new_text):
            res.notes.append(f"same-ast {path}")
            continue
        if new_text is not None and patchlib.same_ast_modulo_none_compare(old_text, new_text):
            res.notes.append(f"none-compare-only {path}")
            continue
        tree = m.tree(src_root, fd.old) if fd.old == path else None
        if tree is None:
            try:
                tree = ast.parse(old_text)
            except SyntaxError:
                res.everything = True
                continue
        if new_text is None:
            res.notes.append(f"deleted {path}")
            res.everything = True
            continue
        try:
            compile(new_text, path, "exec", dont_inherit=True)
        except (SyntaxError, ValueError):
            res.notes.append(f"syntax-error {path}")
            res.everything = True
            continue
        removed, border = patchlib.changed_old_lines(fd)
        old_lines = old_text.split("\n")
        new_lines = new_text.split("\n")
        for line in sorted(removed | border):
            hit = innermost(tree, line)
            if hit is None:
                continue
            node, chain = hit
            lo, hi = header_range(node)
            in_header = lo <= line <= hi
            if not in_header:
                # a blank/comment line inside a compound statement's body
                res.add(m.tests_on(path, [line]), where=(path, {line}))
                continue
            if is_docstring(node):
                continue
            res.add(m.tests_on(path, range(lo, hi + 1)), where=(path, set(range(lo, hi + 1))))
            if (
                path == "sqlglot/dialects/__init__.py"
                or (path == "sqlglot/__init__.py" and not any(isinstance(c, FUNC) for c in chain[1:]))
                or (path == "sqlglot/dialects/dialect.py" and any(isinstance(c, ast.ClassDef) and c.name == "_Dialect" for c in chain))
            ):
                res.no_check = True
                res.notes.append(f"global-state {path}")
            in_func = any(isinstance(c, FUNC) for c in chain[1:])
            if in_func:
                if m.static.get(path, set()) & set(range(lo, hi + 1)):
                    # function code that runs at import time (metaclass, factory called
                    # from a class body): follow what it assigns and where it is called
                    res.add(import_time_func_effect(m, src_root, path, node, chain, res))
                continue
            eff = static_effect(m, src_root, path, node, chain, res)
            keys = entry_keys(node, line, old_lines, new_lines) if sql_keyed(m, path, chain) else None
            if keys and m.id_of:
                kept = m.key_filter(src_root, eff, keys)
                res.notes.append(f"key-filter {sorted(keys)[:3]} {len(eff)}->{len(kept)}")
                eff = kept
            res.add(eff)
            # import-time data naming a class (registries such as DIALECTS = ["Tableau", ...]):
            # every test that uses that class, since lookups are cached after first use
            res.add(m.users_named(_words(old_lines[line - 1] if line - 1 < len(old_lines) else "")))
        # added import-time statements: judge them by the names they define
        try:
            new_tree = ast.parse(new_text)
        except SyntaxError:
            new_tree = None
        if new_tree is not None:
            for line in sorted(patchlib.added_new_lines(fd)):
                hit = innermost(new_tree, line)
                if hit is None:
                    continue
                node, chain = hit
                if any(isinstance(c, FUNC) for c in chain[1:]) or is_docstring(node):
                    continue
                lo, hi = header_range(node)
                if not (lo <= line <= hi):
                    continue
                eff = static_effect(m, src_root, path, node, chain, res, seen={("new", path, node.lineno)})
                keys = entry_keys(node, line, new_lines, new_lines) if sql_keyed(m, path, chain) else None
                if keys and m.id_of:
                    eff = m.key_filter(src_root, eff, keys)
                res.add(eff)
                res.add(m.users_named(_words(new_lines[line - 1] if line - 1 < len(new_lines) else "")))
    return res
