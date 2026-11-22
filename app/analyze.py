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


def _code_only(parts):
    """Lower-cased Python code of the given source fragments without comments and string
    literals (so a comment or SQL text mentioning dialects does not count), tokens joined
    by single spaces; None if a fragment does not tokenize."""
    import io
    import textwrap
    import tokenize

    out = []
    skip = {tokenize.COMMENT, tokenize.STRING, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT}
    for extra in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
        if hasattr(tokenize, extra):
            skip.add(getattr(tokenize, extra))
    for part in parts:
        try:
            for t in tokenize.generate_tokens(io.StringIO(textwrap.dedent(part) + "\n").readline):
                if t.type not in skip and t.string:
                    out.append(t.string)
        except (tokenize.TokenError, IndentationError, SyntaxError):
            return None
    return " ".join(out).lower().replace(" . ", ".").replace(" .", ".").replace(". ", ".")


class Map:
    def __init__(self, data):
        self.lines = {f: {int(l): set(s) for l, s in d.items()} for f, d in data["lines"].items()}
        self.static = {f: set(v) for f, v in data["static"].items()}
        self.opens = data["opens"]
        self.users = {k: set(v) for k, v in data.get("users", {}).items()}
        self.reads = data.get("reads", {})
        self.subq = data.get("subq", {})
        self.env_usage = data.get("env_usage", {})
        self.import_classes = set(data.get("import_classes", ()))
        # TPC-DS queries ordered so the first few execute nearly every line the test runs
        self.e_cover = data.get("e_cover", [])
        # reads inside lambda bodies: they run when the lambda is called, never at import
        self.lambda_reads = {k: {f: set(ls) for f, ls in v.items()} for k, v in data.get("lambda_reads", {}).items()}
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
        self._qtext = None
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
            lam = self.lambda_reads.get(name, {}).get(f, ())
            for l, base in ls:
                if l not in st or (f, l) in skip or l in lam:
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
        code = _code_only(parts)
        if code is None or "dialects" in code or ".classes" in code or "for name in" in code:
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

    def pseudo_text(self, src_root, i):
        """SQL text of TPC-DS query i (pseudo-test 100000+i), lower-cased."""
        if self._qtext is None:
            self._qtext = {}
            try:
                text = open(os.path.join(src_root, "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"), encoding="utf-8").read()
                stmts = "\n".join(l for l in text.splitlines() if l and not l.startswith("--")).split(";")
                for k in range(0, len(stmts) - 1, 2):
                    self._qtext[k // 2] = stmts[k].lower()
            except Exception:  # noqa: BLE001
                pass
        return self._qtext.get(i)

    def exp_users(self, names):
        out = set()
        for n in names:
            for k in self.by_simple.get(n, ()):
                if k.startswith("sqlglot.expressions."):
                    out |= self.users[k]
        return out

    def key_filter(self, src_root, tests, keys, word=False):
        if word:
            import re

            rx = re.compile("|".join(r"(?<![a-z0-9_])" + re.escape(k) + r"(?![a-z0-9_])" for k in keys))

            def has(text):
                return rx.search(text) is not None
        else:
            def has(text):
                return any(k in text for k in keys)

        out = set()
        hit = {}
        for t in tests:
            if t >= 100000:
                q = self.pseudo_text(src_root, t - 100000)
                if q is None or has(q):
                    out.add(t)
                continue
            tt = self.test_text(src_root, t)
            if tt is None:
                out.add(t)
                continue
            text, fixtures = tt
            if has(text):
                out.add(t)
                continue
            for p in fixtures:
                if p not in hit:
                    hit[p] = has(self._fixture(src_root, p))
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

    def generic_static_read(self, name, cls_key=None):
        """Is `name` read at import time through self/cls (a metaclass deriving other data)?
        With cls_key (the class whose attribute changed), reads through an instance
        (`self...`) only count if some instance made at import time is of that class."""
        inst_ok = None
        for f, ls in self.reads.get(name, {}).items():
            st = self.static.get(f, set())
            lam = self.lambda_reads.get(name, {}).get(f, ())
            for l, base in ls:
                if l in st and l not in lam and base.split(".")[0] in GENERIC:
                    if cls_key is not None and base.split(".")[0] in ("self", "this"):
                        if inst_ok is None:
                            inst_ok = any(cls_key in self.mro.get(k, (k,)) for k in self.import_classes)
                        if not inst_ok:
                            continue
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


def escape_keys(node, line, old_lines, new_lines):
    """A one-line NUMERIC_ESCAPES table whose entries keep their keys but change a value:
    only SQL text with that backslash escape (`\\u`, or `\\0`-`\\7` for octal) is read
    differently. Returns the escape strings to look for, or None."""
    import re

    if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.lineno != line:
        return None
    if getattr(node, "end_lineno", line) != line:
        return None
    tgt = node.targets[0] if isinstance(node, ast.Assign) else node.target
    if not isinstance(tgt, ast.Name) or tgt.id != "NUMERIC_ESCAPES":
        return None
    pat = r"[\"']([^\"'\\]+)[\"']\s*:\s*(\([^()]*\))"
    ol = [x for x in old_lines if x.strip().startswith("NUMERIC_ESCAPES")]
    nl = [x for x in new_lines if x.strip().startswith("NUMERIC_ESCAPES")]
    if len(nl) != 1 or len(ol) != 1:
        return None
    o = dict(re.findall(pat, ol[0]))
    if not o:
        return None
    n = dict(re.findall(pat, nl[0]))
    if set(o) != set(n) or o == n:
        return None
    changed = [k for k in o if o[k] != n[k]]
    if any(len(k) != 1 for k in changed):
        return None
    out = set()
    for k in changed:
        if k == "0":
            out |= {"\\" + str(d) for d in range(8)}
        else:
            out.add(("\\" + k).lower())
    return out

_GLOBAL_TEST_TEXT = {}


def global_test_text(src_root):
    """Lower-cased test code outside test classes (module-level constants, helpers) that the
    per-test text does not include."""
    if "t" in _GLOBAL_TEST_TEXT:
        return _GLOBAL_TEST_TEXT["t"]
    import glob

    parts = []
    for f in glob.glob(os.path.join(src_root, "tests", "**", "*.py"), recursive=True):
        try:
            text = open(f, encoding="utf-8").read()
            tree = ast.parse(text)
        except Exception:  # noqa: BLE001
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                continue
            # only string literals: SQL text and schema names, not Python identifiers
            docs = {id(x.value) for x in ast.walk(node) if isinstance(x, ast.Expr)}
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and id(sub) not in docs:
                    parts.append(sub.value)
    _GLOBAL_TEST_TEXT["t"] = "\n".join(parts).lower()
    return _GLOBAL_TEST_TEXT["t"]


def reserved_keys(m, src_root, path, chain, node, line, lines_):
    """An entry of a generator's RESERVED_KEYWORDS set: only identifiers spelled like the
    old or new entry are quoted differently. Returns those words, or None."""
    import re

    if not isinstance(node, (ast.Assign, ast.AnnAssign)) or line <= node.lineno:
        return None
    tgt = node.targets[0] if isinstance(node, ast.Assign) else node.target
    if not isinstance(tgt, ast.Name) or tgt.id != "RESERVED_KEYWORDS":
        return None
    if not isinstance(node.value, (ast.Set, ast.List, ast.Tuple)):
        return None
    classes = [c for c in chain if isinstance(c, ast.ClassDef)]
    if not classes:
        return None
    q = module_name(path) + ":" + ".".join(c.name for c in classes)
    if "sqlglot.generator:Generator" not in m.mro.get(q, ()):
        return None
    text = lines_[line - 1] if line - 1 < len(lines_) else ""
    if not re.fullmatch(r"\s*(\"[A-Za-z0-9_]+\",\s*)+", text):
        return None
    words = {w.lower() for w in re.findall(r"\"([A-Za-z0-9_]+)\"", text)}
    g = global_test_text(src_root)
    if not words or any(re.search(r"(?<![a-z0-9_])" + re.escape(w) + r"(?![a-z0-9_])", g) for w in words):
        return None
    return words


def sql_keyed(m, path, chain):
    """Is this statement in a tokenizer or parser class (whose tables are keyed by SQL text)?"""
    classes = [c for c in chain if isinstance(c, ast.ClassDef)]
    if not classes:
        return False
    q = module_name(path) + ":" + ".".join(c.name for c in classes)
    mro = m.mro.get(q, ())
    return "sqlglot.tokens:Tokenizer" in mro or "sqlglot.parser:Parser" in mro


def exp_keys(node, line, lines_):
    """Expression classes used as dict keys on a changed entry line (`exp.Foo: ...`), for
    tables dispatched by node type (generator TRANSFORMS and friends). None if not that shape."""
    import re

    if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(getattr(node, "value", None), ast.Dict):
        return None
    if line <= node.lineno:
        return None
    text = lines_[line - 1] if line - 1 < len(lines_) else ""
    keys = re.findall(r"\bexp\.([A-Z][A-Za-z0-9_]*)\s*:", text)
    stripped = text.strip()
    # the line must start with the key (an entry), not be a continuation of a value
    if not keys or not re.match(r"exp\.[A-Z]", stripped):
        return None
    return set(keys)


def pygen_effect(m, path, code_text, covered):
    """A change inside code of the Python executor's generator (sqlglot/generators/python.py)
    only alters the generated code it emits. That code runs only for rows that reach it, so
    the TPC-DS queries that matter are the ones whose runs called the ENV functions the code
    emits, not every query that generated it. Returns the narrowed set, or None."""
    import re

    if path != "sqlglot/generators/python.py" or not m.env_usage:
        return None
    qmap = m.env_usage.get("queries", {})
    tmap = m.env_usage.get("tests", {})
    names = {n for n in re.findall(r"\b([A-Z][A-Z0-9_]*)\(", code_text) if n in qmap or n in tmap}
    if not names:
        return None
    pseudo = sorted(c for c in covered if c >= 100000)
    out = {c for c in covered if c < 100000}
    for n in names:
        out |= {100000 + q for q in qmap.get(n, ())}
        out |= set(tmap.get(n, ()))
    # a generation-time error fails every query that generates the code: two of them
    out |= set(pseudo[:2])
    return out


def env_entry_effect(m, path, node, line, old_lines, new_lines, added=False):
    """Tests and TPC-DS queries (pseudo-tests) calling the ENV function on a changed entry
    line of sqlglot/executor/env.py's ENV table; None when the rule does not apply."""
    import re

    if path != "sqlglot/executor/env.py" or not m.env_usage:
        return None
    if not isinstance(node, ast.Assign) or not any(isinstance(t, ast.Name) and t.id == "ENV" for t in node.targets):
        return None
    if line <= node.lineno:
        return None
    keys = []
    for text in (old_lines[line - 1] if line - 1 < len(old_lines) else "", new_lines[line - 1] if line - 1 < len(new_lines) else ""):
        mt = re.match(r'\s*["\']([^"\']+)["\']\s*:', text)
        if not mt:
            return None
        keys.append(mt.group(1))
    callables = set(m.env_usage.get("callables", ()))
    if added:
        # judged by the new line alone: a key nobody calls yet affects no test, and the
        # old-line pass already handles whatever entry this line replaced
        last = keys[-1]
        keys = [last]
        if last not in callables:
            return set()
    elif keys[0] not in callables:
        return None  # the original entry is not a plain function (e.g. the `exp` module)
    keys = set(keys)
    out = set()
    for k in keys:
        out |= set(m.env_usage.get("tests", {}).get(k, ()))
        out |= {100000 + q for q in m.env_usage.get("queries", {}).get(k, ())}
    return out


def enclosing_lambda(node, line):
    """Innermost lambda inside `node` whose span contains `line`."""
    best = None
    for n in ast.walk(node):
        if isinstance(n, ast.Lambda) and n.lineno <= line <= (n.end_lineno or n.lineno):
            if best is None or n.lineno >= best.lineno:
                best = n
    return best


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
        # the change runs at import time: check that every sqlglot module still imports
        self.import_risk = False

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
        if stat or any(m.generic_static_read(n, q) for n in names):
            # the value feeds other import-time code (metaclass, subclass dicts)
            res.notes.append(f"class-attr-derived {q}.{','.join(sorted(names))}")
            out = set(scope) if tracked else set(scope) | readers
            if any(m.generic_static_read(n, q) for n in names):
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
        value_only = patchlib.value_only_change(fd)
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
            lam = enclosing_lambda(node, line)
            if lam is not None and line > lam.lineno:
                # inside a lambda body (a table entry's code): it runs only when called,
                # so the tests that ran those lines are exactly the ones affected
                lines_l = set(range(lam.lineno, (lam.end_lineno or lam.lineno) + 1))
                cov = m.tests_on(path, lines_l)
                lam_text = "\n".join(old_lines[lam.lineno - 1 : (lam.end_lineno or lam.lineno)] + new_lines[lam.lineno - 1 : (lam.end_lineno or lam.lineno)])
                narrowed = pygen_effect(m, path, lam_text, cov)
                if narrowed is not None:
                    res.notes.append(f"pygen {len(cov)}->{len(narrowed)}")
                    cov = narrowed
                res.add(cov, where=(path, lines_l))
                res.notes.append(f"lambda-body {path}:{line}")
                continue
            in_func = any(isinstance(c, FUNC) for c in chain[1:])
            if path.startswith("sqlglot/") and not res.import_risk:
                if in_func:
                    risky = line in m.static.get(path, ())  # function code run during import
                elif not value_only:
                    risky = True  # identifiers changed in import-time code
                else:
                    # a value change: only matters at import if import-time code consumes it
                    classes = [c for c in chain if isinstance(c, ast.ClassDef)]
                    q = module_name(path) + ":" + ".".join(c.name for c in classes) if classes else None
                    names = assigned_names(node)
                    risky = bool(
                        (q and any(q in m.mro.get(k, (k,)) for k in m.import_classes))
                        or any(m.static_reads(n, {(path, node.lineno)}) or m.generic_static_read(n, q) for n in names)
                        or not names
                    )
                res.import_risk = risky
            env_eff0 = None if in_func else env_entry_effect(m, path, node, line, old_lines, new_lines)
            if env_eff0 is not None:
                # an executor ENV entry: only the tests / TPC-DS queries that call it
                res.add(env_eff0)
                res.notes.append(f"env-entry {len(env_eff0)}")
                continue
            if not in_func and isinstance(node, (ast.Assign, ast.AnnAssign)) and hi > lo:
                # one entry of a multi-line import-time table: its own runtime code only
                # (the import-time effect of the whole statement is handled below)
                direct_lines = {line}
            else:
                direct_lines = set(range(lo, hi + 1))
            esc = escape_keys(node, line, old_lines, new_lines) if not in_func and sql_keyed(m, path, chain) else None
            direct = m.tests_on(path, direct_lines)
            if in_func and path == "sqlglot/generators/python.py":
                fn = next((c for c in reversed(chain) if isinstance(c, FUNC)), None)
                if fn is not None:
                    a, b = fn.lineno, (fn.end_lineno or fn.lineno)
                    fn_text = "\n".join(old_lines[a - 1 : b]) + "\n" + "\n".join(new_lines[a - 1 : b + 2])
                    narrowed = pygen_effect(m, path, fn_text, direct)
                    if narrowed is not None:
                        res.notes.append(f"pygen {len(direct)}->{len(narrowed)}")
                        direct = narrowed
            if esc and m.id_of:
                kept = m.key_filter(src_root, direct, esc)
                res.notes.append(f"escape-filter {sorted(esc)[:3]} {len(direct)}->{len(kept)}")
                direct = kept
            res.add(direct, where=(path, direct_lines))
            if (
                path == "sqlglot/dialects/__init__.py"
                or (path == "sqlglot/__init__.py" and not any(isinstance(c, FUNC) for c in chain[1:]))
                or (path == "sqlglot/dialects/dialect.py" and any(isinstance(c, ast.ClassDef) and c.name == "_Dialect" for c in chain))
            ):
                res.no_check = True
                res.notes.append(f"global-state {path}")
            if in_func:
                if m.static.get(path, set()) & set(range(lo, hi + 1)):
                    # function code that runs at import time (metaclass, factory called
                    # from a class body): follow what it assigns and where it is called
                    res.add(import_time_func_effect(m, src_root, path, node, chain, res))
                continue
            eff = static_effect(m, src_root, path, node, chain, res)
            ek_old = exp_keys(node, line, old_lines)
            if ek_old:
                # a node-type dispatch entry only matters to tests that build that node type
                ek = set(ek_old)
                ek |= exp_keys(node, line, new_lines) or set()
                kept = eff & m.exp_users(ek)
                res.notes.append(f"exp-key {sorted(ek)[:3]} {len(eff)}->{len(kept)}")
                eff = kept
            keys = entry_keys(node, line, old_lines, new_lines) if sql_keyed(m, path, chain) else None
            if keys is None and sql_keyed(m, path, chain):
                keys = escape_keys(node, line, old_lines, new_lines)
            word = False
            if keys is None:
                k_old = reserved_keys(m, src_root, path, chain, node, line, old_lines)
                k_new = reserved_keys(m, src_root, path, chain, node, line, new_lines)
                keys = (k_old | k_new) if k_old is not None and k_new is not None else None
                word = True
            if keys and m.id_of:
                kept = m.key_filter(src_root, eff, keys, word=word)
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
            added_lines = patchlib.added_new_lines(fd)
            for line in sorted(added_lines):
                hit = innermost(new_tree, line)
                if hit is None:
                    continue
                node, chain = hit
                if any(isinstance(c, FUNC) for c in chain[1:]) or is_docstring(node):
                    continue
                lo, hi = header_range(node)
                if not (lo <= line <= hi):
                    continue
                lam = enclosing_lambda(node, line)
                if lam is not None and line > lam.lineno and lam.lineno not in added_lines:
                    # the body of a lambda that was already there: the old lambda's tests
                    # (handled with the removed lines) are the ones that run it
                    continue
                env_eff = env_entry_effect(m, path, node, line, new_lines, new_lines, added=True)
                if env_eff is not None:
                    res.add(env_eff)
                    continue
                eff = static_effect(m, src_root, path, node, chain, res, seen={("new", path, node.lineno)})
                ek = exp_keys(node, line, new_lines)
                if ek:
                    eff = eff & m.exp_users(ek)
                keys = entry_keys(node, line, new_lines, new_lines) if sql_keyed(m, path, chain) else None
                if keys is None and sql_keyed(m, path, chain):
                    keys = escape_keys(node, line, old_lines, new_lines)
                word = False
                if keys is None:
                    keys = reserved_keys(m, src_root, path, chain, node, line, new_lines)
                    word = True
                if keys and m.id_of:
                    eff = m.key_filter(src_root, eff, keys, word=word)
                res.add(eff)
                res.add(m.users_named(_words(new_lines[line - 1] if line - 1 < len(new_lines) else "")))
    return res
