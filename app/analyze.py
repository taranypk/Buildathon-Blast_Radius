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
GENERIC = {"self", "cls", "klass", "kls", "mcs", "metacls", "this"}


def cls_key_file(key):
    return key.split(":", 1)[0].replace(".", "/") + ".py"


def imports_name(m, f, name, src_root=None):
    tree = m.tree(src_root, f) if src_root else m._trees.get(f)
    if tree is None:
        return True
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and any((a.asname or a.name) == name for a in n.names):
            return True
    return False
DEF = FUNC + (ast.ClassDef,)


class Map:
    def __init__(self, data):
        self.lines = {f: {int(l): set(s) for l, s in d.items()} for f, d in data["lines"].items()}
        self.static = {f: set(v) for f, v in data["static"].items()}
        self.opens = data["opens"]
        self.users = {k: set(v) for k, v in data.get("users", {}).items()}
        self.reads = data.get("reads", {})
        self.mro = data.get("mro", {})
        self.class_names = {k.split(":", 1)[1].split(".")[-1] for k in self.mro}
        self._trees = {}
        self._desc = {}
        self.tests_by_file = {}  # filled by the caller: test file path -> test numbers
        self.always_on_import = set()  # filled by the caller: tests run in a subprocess
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
        desc = self.desc_names(cls_key) if cls_key else None
        for f, ls in self.reads.get(name, {}).items():
            st = self.static.get(f, set())
            for l, base in ls:
                if l not in st or (f, l) in skip:
                    continue
                if base in GENERIC:
                    continue
                if cls_key is not None:
                    if base == "":
                        if f != cls_key_file(cls_key):
                            continue
                    elif base in self.class_names and base not in desc:
                        continue
                else:
                    if base in self.class_names:
                        continue
                    if base == "" and module and f != module and not imports_name(self, f, name):
                        continue
                res.append((f, l))
        return res

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
        self.notes: list[str] = []
        self.everything = False

    def add(self, s):
        s = set(s)
        if s:
            self.reasons.append(s)
            self.cands |= s


def class_scope(m, path, chain_classes):
    """Tests that use this class: instantiated it (or a subclass), or ran its code."""
    cls = chain_classes[-1]
    q = module_name(path) + ":" + ".".join(c.name for c in chain_classes)
    users = m.users.get(q)
    ran = m.tests_in_range(path, cls.lineno, cls.end_lineno or cls.lineno)
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
        if stat:
            # the value feeds other import-time code (metaclass, subclass dicts)
            res.notes.append(f"class-attr-derived {q}.{','.join(sorted(names))}")
            out = set(scope) if tracked else set(scope) | readers
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
        files = {f for f in m.reads.get(n, {}) if f == path or imports_name(m, f, n, src_root)}
        out |= m.readers_in(n, files)
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
            res.add(t for t in openers if t >= 0)
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
                res.add(m.tests_on(path, [line]))
                continue
            if is_docstring(node):
                continue
            res.add(m.tests_on(path, range(lo, hi + 1)))
            in_func = any(isinstance(c, FUNC) for c in chain[1:])
            if in_func:
                continue
            res.add(static_effect(m, src_root, path, node, chain, res))
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
                res.add(static_effect(m, src_root, path, node, chain, res, seen={("new", path, node.lineno)}))
                res.add(m.users_named(_words(new_lines[line - 1] if line - 1 < len(new_lines) else "")))
    return res
