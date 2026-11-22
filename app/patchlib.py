"""Parse unified diffs, apply them, and find which old-file lines a change touches."""
from __future__ import annotations

import ast
import re

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FileDiff:
    def __init__(self):
        self.old = None  # path or None for new file
        self.new = None  # path or None for deleted file
        self.hunks = []  # list of (old_start, [(tag, text)])
        self.binary = False

    @property
    def path(self):
        return self.new or self.old


def _strip(p):
    p = p.split("\t")[0].strip()
    if p == "/dev/null":
        return None
    if p.startswith("a/") or p.startswith("b/"):
        return p[2:]
    return p


def parse_patch(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur = None
    hunk = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FileDiff()
            files.append(cur)
            hunk = None
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if m:
                cur.old, cur.new = m.group(1), m.group(2)
        elif cur is not None and hunk is None and line.startswith("new file mode"):
            cur.old = None
        elif cur is not None and hunk is None and line.startswith("deleted file mode"):
            cur.new = None
        elif cur is not None and hunk is None and line.startswith("rename from "):
            cur.old = line[len("rename from "):]
        elif cur is not None and hunk is None and line.startswith("rename to "):
            cur.new = line[len("rename to "):]
        elif cur is not None and line.startswith("Binary files"):
            cur.binary = True
        elif line.startswith("--- ") and (hunk is None or not _in_hunk(hunk)):
            if cur is None:
                cur = FileDiff()
                files.append(cur)
            cur.old = _strip(line[4:])
            hunk = None
        elif line.startswith("+++ ") and (hunk is None or not _in_hunk(hunk)):
            cur.new = _strip(line[4:])
        elif line.startswith("@@"):
            m = HUNK.match(line)
            if m and cur is not None:
                ol = int(m.group(2)) if m.group(2) is not None else 1
                nl = int(m.group(4)) if m.group(4) is not None else 1
                hunk = [int(m.group(1)), [], ol, nl, int(m.group(3))]
                cur.hunks.append(hunk)
        elif hunk is not None and _in_hunk(hunk):
            if line.startswith("\\"):
                # "\ No newline at end of file" applies to the previous line
                if hunk[1]:
                    tag, txt = hunk[1][-1]
                    hunk[1][-1] = (tag, txt, "nonl")
            elif line[:1] in (" ", "+", "-") or line == "":
                tag = line[:1] or " "
                hunk[1].append((tag, line[1:]))
                if tag in (" ", "-"):
                    hunk[2] -= 1
                if tag in (" ", "+"):
                    hunk[3] -= 1
        i += 1
    return files


def _in_hunk(h):
    return h[2] > 0 or h[3] > 0


def apply(old_text: str | None, fd: FileDiff) -> str:
    """Apply the hunks of fd to old_text. Returns the new text."""
    old = (old_text or "").split("\n")
    # old_text ends with "\n" normally -> last element "" ; keep line semantics
    trailing_nl = old_text is not None and old_text.endswith("\n")
    if trailing_nl:
        old = old[:-1]
    if old_text is None or old_text == "":
        old = []
    out: list[str] = []
    pos = 0  # 0-based index into old
    new_nonl = False
    for h in fd.hunks:
        start = h[0] - 1 if h[0] > 0 else 0
        if not any(t in (" ", "-") for t, *_ in h[1]):
            # pure insertion: @@ -N,0 means insert after line N
            start = h[0]
        out.extend(old[pos:start])
        pos = start
        new_nonl = False
        for item in h[1]:
            tag, txt = item[0], item[1]
            if tag == " ":
                out.append(old[pos] if pos < len(old) else txt)
                pos += 1
                new_nonl = len(item) > 2
            elif tag == "-":
                pos += 1
            else:
                out.append(txt)
                new_nonl = len(item) > 2
    out.extend(old[pos:])
    if pos < len(old):
        new_nonl = not trailing_nl
    text = "\n".join(out)
    if out and not new_nonl:
        text += "\n"
    return text


def changed_old_lines(fd: FileDiff) -> tuple[set[int], set[int]]:
    """Old-file line numbers removed/modified, and old lines bordering pure insertions."""
    removed: set[int] = set()
    border: set[int] = set()
    for h in fd.hunks:
        ln = h[0] if any(t in (" ", "-") for t, *_ in h[1]) else h[0] + 1
        prev_kind = None
        replaced = False  # the current +block replaces removed lines
        for item in h[1]:
            tag = item[0]
            if tag == " ":
                if prev_kind == "+" and not replaced:
                    border.add(ln)
                ln += 1
                replaced = False
            elif tag == "-":
                removed.add(ln)
                ln += 1
                replaced = True
            else:
                if prev_kind != "+" and not replaced:
                    # a pure insertion: the statements on either side locate it
                    border.add(ln - 1)
                    border.add(ln)
            prev_kind = tag
    return removed, border


def _tokens(text):
    import io
    import tokenize

    out = []
    try:
        for t in tokenize.generate_tokens(io.StringIO(text).readline):
            if t.type in (tokenize.COMMENT, tokenize.NL):
                continue
            if t.type == tokenize.NEWLINE:
                out.append((t.type, ""))
            else:
                out.append((t.type, t.string))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return out


def _middle(a, b):
    """Strip the common prefix and suffix of two token lists."""
    i = 0
    n = min(len(a), len(b))
    while i < n and a[i] == b[i]:
        i += 1
    j = 0
    while j < n - i and a[len(a) - 1 - j] == b[len(b) - 1 - j]:
        j += 1
    return a[i : len(a) - j], b[i : len(b) - j], a[max(0, i - 1) : i] + a[len(a) - j : len(a) - j + 1]


def same_ast(a: str, b: str) -> bool | None:
    """Same code apart from comments and blank lines (token streams equal)."""
    ta, tb = _tokens(a), _tokens(b)
    if ta is None or tb is None:
        try:
            return ast.dump(ast.parse(a)) == ast.dump(ast.parse(b))
        except SyntaxError:
            return None
    return ta == tb


class _NoneCompare(ast.NodeTransformer):
    """`x is None` and `x == None` behave the same for every object compared with None
    here, so normalise both (and `is not` / `!=`) before comparing two files."""

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1 and any(
            isinstance(c, ast.Constant) and c.value is None for c in [node.left, *node.comparators]
        ):
            op = node.ops[0]
            if isinstance(op, ast.Eq):
                node.ops = [ast.Is()]
            elif isinstance(op, ast.NotEq):
                node.ops = [ast.IsNot()]
        return node


SAFE_ENUMS = {
    "AliasPrecedence", "DType", "ErrorLevel", "IsPredicatePrecedence", "NormalizationStrategy",
    "PropertiesLocation", "ScopeType", "TokenType", "TrieResult", "UsingColumnOrder",
}


def same_ast_modulo_none_compare(a: str, b: str) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if ta is not None and tb is not None:
        ma, mb, around = _middle(ta, tb)
        sa, sb = [t[1] for t in ma], [t[1] for t in mb]
        pairs = {(("is",), ("==",)), (("==",), ("is",)), (("is", "not"), ("!=",)), (("!=",), ("is", "not"))}
        if (tuple(sa), tuple(sb)) in pairs:
            # the operator sits right next to the differing span: is a None on either side?
            if any(t[1] == "None" for t in around):
                return True
            # ... or a member of one of sqlglot's enums (identity and equality agree for
            # enum members; Dialects is left out, its values are often given as strings)
            k = 0
            while k < min(len(ta), len(tb)) and ta[k] == tb[k]:
                k += 1
            after = [t[1] for t in ta[k + len(ma) : k + len(ma) + 4]]
            before = [t[1] for t in ta[max(0, k - 4) : k]]
            if len(after) >= 3 and after[0] in SAFE_ENUMS and after[1] == "." and after[2].isupper() \
                    and (len(after) < 4 or after[3] not in (".", "(", "[")):
                return True
            if len(before) >= 3 and before[-3] in SAFE_ENUMS and before[-2] == "." and before[-1].isupper() \
                    and (len(before) < 4 or before[-4] != "."):
                return True
            return False
        if len(ma) + len(mb) < 40:
            return False  # a small, different change
    try:
        ta, tb = _NoneCompare().visit(ast.parse(a)), _NoneCompare().visit(ast.parse(b))
        return ast.dump(ta) == ast.dump(tb)
    except SyntaxError:
        return False


def added_new_lines(fd: FileDiff) -> set[int]:
    """New-file line numbers of added lines."""
    added: set[int] = set()
    for h in fd.hunks:
        ln = h[4] if len(h) > 4 else 0
        if not any(t in (" ", "+") for t, *_ in h[1]):
            continue
        if ln == 0:
            ln = 1
        for item in h[1]:
            if item[0] == "+":
                added.add(ln)
                ln += 1
            elif item[0] == " ":
                ln += 1
    return added


_VALUE_NAMES = {"True", "False", "None", "and", "or", "not", "is", "in", "if", "else"}


def value_only_change(fd: FileDiff) -> bool:
    """Do the removed and added lines differ only in literals and operators (no identifier
    added, removed or renamed)? Such a change cannot raise NameError/AttributeError."""
    import io
    import tokenize
    from collections import Counter

    def toks(lines):
        out = Counter()
        try:
            for t in tokenize.generate_tokens(io.StringIO("\n".join(l.strip() for l in lines) + "\n").readline):
                if t.type == tokenize.NAME and t.string not in _VALUE_NAMES:
                    out[t.string] += 1
        except (tokenize.TokenError, IndentationError, SyntaxError):
            return None
        return out

    for h in fd.hunks:
        rem = [item[1] for item in h[1] if item[0] == "-"]
        add = [item[1] for item in h[1] if item[0] == "+"]
        a, b = toks(rem), toks(add)
        if a is None or b is None or a != b:
            return False
    return True
