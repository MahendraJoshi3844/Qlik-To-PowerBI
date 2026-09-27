"""Split and parse a Qlik load script into statements.

The load script is where a Qlik app's data model is actually built - there is
no separate model definition to read. So the model is recovered by parsing the
script: which tables are loaded, from where, with which fields, and how later
statements (JOIN, CONCATENATE, DROP, RENAME, QUALIFY) change them.

Statement kinds produced: `load` (a LOAD/SELECT chain, including preceding
loads), `set`, `let`, `connect`, `qualify`, `unqualify`, `drop_tables`,
`drop_fields`, `rename_field`, `rename_table`, `store`, `section`, `control`,
`binary`, `include`, `ignored` and `unknown`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_CONTROL = re.compile(
    r"^(if|elseif|else|end\s*if|for|next|do|loop|sub|end\s*sub|call|switch|case|default|end\s*switch|exit\s+(for|do|sub))\b",
    re.I,
)


@dataclass
class FieldSpec:
    expr: str  # the source expression text ('*' for star)
    alias: str  # output field name
    is_star: bool = False

    @property
    def is_plain(self) -> bool:
        """A bare field reference (possibly renamed), not a computation."""
        return bool(_PLAIN.fullmatch(self.expr.strip()))


_PLAIN = re.compile(r"\[[^\]]+\]|\"[^\"]+\"|`[^`]+`|[A-Za-z_%#$@][\w%#$@.]*")


@dataclass
class Source:
    kind: str  # file | resident | inline | autogenerate | sql | none
    path: str = ""
    format: dict[str, str] = field(default_factory=dict)
    table: str = ""  # resident table
    header: list[str] = field(default_factory=list)  # inline header
    rows: list[list[str]] = field(default_factory=list)  # inline rows
    sql: str = ""
    count: str = ""  # autogenerate


@dataclass
class LoadStep:
    kind: str  # load | select
    fields: list[FieldSpec] = field(default_factory=list)
    source: Source = field(default_factory=lambda: Source("none"))
    distinct: bool = False
    where: str = ""
    while_: str = ""
    group_by: list[str] = field(default_factory=list)
    order_by: str = ""
    text: str = ""


@dataclass
class Prefix:
    kind: str  # mapping | concatenate | noconcatenate | join | keep | crosstable | generic | hierarchy | intervalmatch | first | buffer | sample | other
    how: str = ""  # left/right/inner/outer for join/keep
    target: str = ""  # (Table) argument
    args: list[str] = field(default_factory=list)


@dataclass
class Statement:
    kind: str
    text: str
    line: int
    tab: str = ""
    label: str = ""
    prefixes: list[Prefix] = field(default_factory=list)
    steps: list[LoadStep] = field(default_factory=list)  # outermost (first written) first
    name: str = ""  # variable / connection / table name
    value: str = ""
    names: list[str] = field(default_factory=list)
    extra: str = ""


# --- splitting ------------------------------------------------------------------


def split_statements(script: str) -> list[tuple[str, int, str]]:
    """(statement text without ';', 1-based line, tab name). Comments removed."""
    out: list[tuple[str, int, str]] = []
    i, n = 0, len(script)
    buf: list[str] = []
    line = 1
    start_line = 1
    tab = ""

    def flush():
        nonlocal buf
        text = "".join(buf).strip()
        if text:
            out.append((text, start_line, tab))
        buf = []

    while i < n:
        ch = script[i]
        if not "".join(buf).strip():
            start_line = line
            # A control statement ends at the end of its line.
            rest = script[i:]
            stripped = rest.lstrip(" \t")
            m = _CONTROL.match(stripped)
            if m and (not stripped.lower().startswith("call") or True):
                eol = script.find("\n", i)
                semi = script.find(";", i)
                end = n if eol < 0 else eol
                if 0 <= semi < end:
                    end = semi
                out.append((script[i:end].strip(), line, tab))
                line += script.count("\n", i, end)
                i = end + 1 if end < n and script[end] in ";\n" else end
                if end < n and script[end] == "\n":
                    line += 1
                continue
        if ch == "\n":
            line += 1
            buf.append(ch)
            i += 1
            continue
        if script.startswith("///$tab", i):
            j = script.find("\n", i)
            j = n if j < 0 else j
            tab = script[i + 7 : j].strip()
            i = j
            continue
        if script.startswith("//", i) and not (i > 0 and script[i - 1] == ":"):
            j = script.find("\n", i)
            i = n if j < 0 else j
            continue
        if script.startswith("/*", i):
            j = script.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += script.count("\n", i, j)
            i = j
            continue
        if not "".join(buf).strip() and re.match(r"rem\b", script[i:], re.I):
            j = script.find(";", i)
            j = n if j < 0 else j + 1
            line += script.count("\n", i, j)
            i = j
            continue
        if ch in "'\"`":
            j = script.find(ch, i + 1)
            j = n - 1 if j < 0 else j
            buf.append(script[i : j + 1])
            line += script.count("\n", i, j + 1)
            i = j + 1
            continue
        if ch == "[":
            j = script.find("]", i + 1)
            j = n - 1 if j < 0 else j
            buf.append(script[i : j + 1])
            line += script.count("\n", i, j + 1)
            i = j + 1
            continue
        if ch == ";":
            flush()
            i += 1
            continue
        buf.append(ch)
        i += 1
    flush()
    return out


# --- top-level scanning helpers ---------------------------------------------------


def _scan(text: str):
    """Yield (index, char, depth) for characters outside quotes/brackets."""
    depth, i, n = 0, 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"`":
            j = text.find(ch, i + 1)
            i = n if j < 0 else j + 1
            continue
        if ch == "[":
            j = text.find("]", i + 1)
            i = n if j < 0 else j + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        yield i, ch, depth
        i += 1


def split_top(text: str, sep: str = ",") -> list[str]:
    parts, last = [], 0
    for i, ch, depth in _scan(text):
        if ch == sep and depth == 0:
            parts.append(text[last:i])
            last = i + 1
    parts.append(text[last:])
    return [p.strip() for p in parts if p.strip()]


def find_keyword(text: str, words: tuple[str, ...], start: int = 0) -> tuple[int, str]:
    """First top-level occurrence of any keyword (word-bounded). (-1, '') if none."""
    low = text.lower()
    for i, ch, depth in _scan(text):
        if i < start or depth != 0:
            continue
        if i > 0 and (low[i - 1].isalnum() or low[i - 1] in "_.#$"):
            continue
        for w in words:
            if low.startswith(w, i):
                end = i + len(w)
                if end >= len(low) or not (low[end].isalnum() or low[end] in "_#$"):
                    return i, w
    return -1, ""


def unquote(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and ((name[0], name[-1]) in (("[", "]"), ('"', '"'), ("`", "`"), ("'", "'"))):
        return name[1:-1]
    return name


# --- statement parsing -------------------------------------------------------------


_LABEL = re.compile(r"^\s*(\[[^\]]+\]|\"[^\"]+\"|[A-Za-z_%#$@][\w %#$@.-]*?)\s*:(?!:|//)\s*", re.S)
_PREFIX = re.compile(
    r"^\s*(mapping|noconcatenate|concatenate|(?:left|right|inner|outer)\s+join|join|(?:left|right|inner)\s+keep|keep|"
    r"crosstable|generic|hierarchy|hierarchybelongsto|intervalmatch|first|buffer|sample|add|replace|merge|semantic|unless|when)\b",
    re.I,
)


def _parse_format(spec: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in split_top(spec):
        low = part.lower().strip()
        if low in ("txt", "qvd", "ooxml", "biff", "xml", "html", "fix", "dif", "kml", "qvx", "parquet", "json"):
            out["type"] = low
        elif low.startswith("delimiter is"):
            out["delimiter"] = unquote(part.split(None, 2)[2])
        elif low.startswith("table is"):
            out["table"] = unquote(part.split(None, 2)[2])
        elif "embedded labels" in low:
            out["labels"] = "embedded"
        elif "no labels" in low:
            out["labels"] = "none"
        elif low.startswith("header is"):
            out["header"] = part.split(None, 2)[2]
        elif low in ("utf8", "unicode", "ansi", "oem", "mac") or low.startswith("codepage"):
            out["encoding"] = low
        elif low in ("msq", "no quotes"):
            out["quotes"] = low
        else:
            out.setdefault("other", "")
            out["other"] = (out["other"] + ", " + part).strip(", ")
    return out


def _parse_inline(body: str) -> tuple[list[str], list[list[str]]]:
    lines = [ln for ln in body.strip().splitlines() if ln.strip()]
    if not lines:
        return [], []
    header = [unquote(h) for h in split_top(lines[0])]
    rows = [[unquote(v) for v in _split_csv(ln)] for ln in lines[1:]]
    return header, rows


def _split_csv(line: str) -> list[str]:
    parts, buf, q = [], [], None
    for ch in line:
        if q:
            buf.append(ch)
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf).strip())
    return parts


def _fields(text: str) -> list[FieldSpec]:
    out = []
    for part in split_top(text):
        if part.strip() == "*":
            out.append(FieldSpec("*", "*", is_star=True))
            continue
        idx = -1
        for i, _, depth in _scan(part):
            if depth == 0 and part[i : i + 4].lower() == " as " or (depth == 0 and part[i : i + 4].lower() in ("\tas ", "\nas ")):
                idx = i
        if idx >= 0:
            expr, alias = part[:idx].strip(), unquote(part[idx + 4 :].strip())
        else:
            expr, alias = part.strip(), unquote(part.strip())
        out.append(FieldSpec(expr, alias))
    return out


def _parse_load(text: str) -> LoadStep:
    """`LOAD [DISTINCT] fields [FROM x (fmt) | RESIDENT t | INLINE [...] | AUTOGENERATE n] [WHERE] [GROUP BY]`"""
    body = re.sub(r"^\s*load\s+", "", text, count=1, flags=re.I)
    step = LoadStep(kind="load", text=text)
    if re.match(r"distinct\b", body, re.I):
        step.distinct = True
        body = body[8:].lstrip()
    kw_at, kw = find_keyword(body, ("from", "resident", "inline", "autogenerate", "where", "while", "group by", "order by"))
    field_part = body if kw_at < 0 else body[:kw_at]
    step.fields = _fields(field_part)
    rest = "" if kw_at < 0 else body[kw_at:]
    if kw == "from":
        rest = rest[4:].lstrip()
        if rest.startswith("["):
            end = rest.index("]") + 1
        elif rest[:1] in "'\"":
            end = rest.index(rest[0], 1) + 1
        else:
            m = re.match(r"[^\s(]+", rest)
            end = m.end() if m else len(rest)
        step.source = Source("file", path=unquote(rest[:end]))
        rest = rest[end:].lstrip()
        if rest.startswith("("):
            close = next(i for i, ch, d in _scan(rest) if ch == ")" and d == 0)
            step.source.format = _parse_format(rest[1:close])
            rest = rest[close + 1 :]
    elif kw == "resident":
        rest = rest[8:].lstrip()
        m = re.match(r"(\[[^\]]+\]|\"[^\"]+\"|[^\s]+)", rest)
        step.source = Source("resident", table=unquote(m.group(1)))
        rest = rest[m.end() :]
    elif kw == "inline":
        rest = rest[6:].lstrip()
        if rest.startswith("["):
            end = rest.index("]")
            header, rows = _parse_inline(rest[1:end])
            step.source = Source("inline", header=header, rows=rows)
            rest = rest[end + 1 :]
            if rest.lstrip().startswith("("):
                r = rest.lstrip()
                close = next(i for i, ch, d in _scan(r) if ch == ")" and d == 0)
                step.source.format = _parse_format(r[1:close])
                rest = r[close + 1 :]
    elif kw == "autogenerate":
        rest = rest[12:].lstrip()
        m = re.match(r"(\([^)]*\)|[^\s]+)", rest)
        step.source = Source("autogenerate", count=m.group(1) if m else "")
        rest = rest[m.end() if m else 0 :]
    for clause in ("where", "while", "group by", "order by"):
        at, _ = find_keyword(rest, (clause,))
        if at < 0:
            continue
        nxt = [find_keyword(rest, (c,), at + len(clause))[0] for c in ("where", "while", "group by", "order by")]
        nxt = [x for x in nxt if x > at]
        end = min(nxt) if nxt else len(rest)
        value = rest[at + len(clause) : end].strip()
        if clause == "where":
            step.where = value
        elif clause == "while":
            step.while_ = value
        elif clause == "group by":
            step.group_by = [unquote(f) for f in split_top(value)]
        else:
            step.order_by = value
    return step


def _parse_table_statement(text: str, line: int, tab: str) -> Statement:
    st = Statement(kind="load", text=text, line=line, tab=tab)
    body = text
    m = _LABEL.match(body)
    if m and not re.match(r"^\s*(lib|https?|file)\s*:", body, re.I):
        candidate = unquote(m.group(1))
        # "SQL:" is not a label; neither is a prefix keyword.
        if candidate.lower() not in ("sql", "load", "select"):
            st.label = candidate
            body = body[m.end() :]
    while True:
        pm = _PREFIX.match(body)
        if not pm:
            break
        word = re.sub(r"\s+", " ", pm.group(1).lower())
        body = body[pm.end() :].lstrip()
        args: list[str] = []
        if body.startswith("("):
            close = next(i for i, ch, d in _scan(body) if ch == ")" and d == 0)
            args = split_top(body[1:close])
            body = body[close + 1 :].lstrip()
        if word.endswith("join") or word.endswith("keep"):
            how = word.split()[0] if " " in word else ("outer" if word == "join" else "inner")
            st.prefixes.append(Prefix("join" if word.endswith("join") else "keep", how=how,
                                      target=unquote(args[0]) if args else ""))
        elif word in ("concatenate",):
            st.prefixes.append(Prefix("concatenate", target=unquote(args[0]) if args else ""))
        elif word in ("first", "sample"):
            n = re.match(r"(\d+)", body)
            if n:
                args = [n.group(1)]
                body = body[n.end() :].lstrip()
            st.prefixes.append(Prefix(word, args=args))
        else:
            st.prefixes.append(Prefix(word if word in ("mapping", "noconcatenate", "crosstable", "generic", "hierarchy",
                                                      "intervalmatch", "buffer") else "other",
                                      args=[unquote(a) for a in args], how=word))
    low = body.lower().lstrip()
    if low.startswith("sql ") or low.startswith("select ") or low.startswith("select\n"):
        sql = re.sub(r"^\s*sql\s+", "", body, count=1, flags=re.I)
        st.steps.append(LoadStep(kind="select", source=Source("sql", sql=sql.strip()), text=body))
    elif low.startswith("load"):
        st.steps.append(_parse_load(body))
    else:
        st.kind = "unknown"
    return st


def parse_statement(text: str, line: int, tab: str) -> Statement:
    low = text.lower().strip()
    first = low.split(None, 1)[0] if low else ""
    mk = lambda kind, **kw: Statement(kind=kind, text=text, line=line, tab=tab, **kw)  # noqa: E731
    if low.startswith("$(include") or low.startswith("$(must_include"):
        return mk("include", value=text)
    if _CONTROL.match(low):
        return mk("control", value=first)
    if first in ("set", "let"):
        m = re.match(r"^\s*(set|let)\s+(\[[^\]]+\]|[^\s=]+)\s*=\s*(.*)$", text, re.I | re.S)
        if m:
            return mk(first, name=unquote(m.group(2)), value=m.group(3).strip())
        return mk("unknown")
    if re.match(r"^(lib\s+)?connect\s+to\b|^(odbc|oledb|custom)\s+connect", low):
        m = re.search(r"connect\s+(?:\d+\s+)?to\s+(.*)$", text, re.I | re.S)
        target = unquote(m.group(1).strip()) if m else text
        return mk("connect", name=target, value=first)
    if low.startswith("disconnect"):
        return mk("ignored")
    if first in ("qualify", "unqualify"):
        rest = text.strip()[len(first) :].strip()
        return mk(first, names=[unquote(x) for x in split_top(rest)])
    if re.match(r"drop\s+tables?\b", low):
        rest = re.sub(r"^\s*drop\s+tables?\s+", "", text, flags=re.I)
        return mk("drop_tables", names=[unquote(x) for x in split_top(rest)])
    if re.match(r"drop\s+fields?\b", low):
        rest = re.sub(r"^\s*drop\s+fields?\s+", "", text, flags=re.I)
        at, _ = find_keyword(rest, ("from",))
        tables = [unquote(x) for x in split_top(rest[at + 4 :])] if at >= 0 else []
        names = [unquote(x) for x in split_top(rest[:at] if at >= 0 else rest)]
        return mk("drop_fields", names=names, extra=",".join(tables))
    if re.match(r"rename\s+fields?\b", low) or re.match(r"rename\s+tables?\b", low):
        is_table = bool(re.match(r"rename\s+tables?\b", low))
        rest = re.sub(r"^\s*rename\s+(fields?|tables?)\s+", "", text, flags=re.I)
        if re.match(r"using\b", rest.strip(), re.I):
            return mk("unknown", value="RENAME ... USING a mapping table")
        pairs = []
        for part in split_top(rest):
            at, _ = find_keyword(part, ("to",))
            if at > 0:
                pairs.append((unquote(part[:at]), unquote(part[at + 2 :])))
        return mk("rename_table" if is_table else "rename_field", names=[f"{a}\u0000{b}" for a, b in pairs])
    if first == "store":
        m = re.match(r"^\s*store\s+(.*?)\s+into\s+(.*)$", text, re.I | re.S)
        return mk("store", name=unquote(m.group(1)) if m else "", value=m.group(2).strip() if m else "")
    if re.match(r"section\s+(access|application)", low):
        return mk("section", value="access" if "access" in low else "application")
    if first == "binary":
        return mk("binary", value=text)
    if first in ("trace", "sleep", "directory", "execute", "exit", "force", "comment", "tag", "untag", "alias",
                 "star", "nullasvalue", "nullasnull", "search", "declare", "derive", "map", "unmap"):
        return mk("ignored" if first in ("trace", "sleep", "directory", "exit") else "unknown", value=first)
    return _parse_table_statement(text, line, tab)


def parse_script(script: str) -> list[Statement]:
    """Statements, with preceding loads folded into the load they sit on."""
    out: list[Statement] = []
    pending: Statement | None = None
    for text, line, tab in split_statements(script):
        st = parse_statement(text, line, tab)
        if st.kind == "load" and st.steps:
            step = st.steps[0]
            if pending is not None:
                # A preceding load's chain continues: the label and prefixes stay
                # with the first statement written.
                pending.steps.append(step)
                pending.text += ";\n" + st.text
                if step.source.kind != "none":
                    out.append(pending)
                    pending = None
                continue
            if step.kind == "load" and step.source.kind == "none":
                pending = st
                continue
        elif pending is not None:
            pending.kind = "unknown"
            pending.value = "preceding LOAD without a following LOAD or SELECT"
            out.append(pending)
            pending = None
        out.append(st)
    if pending is not None:
        pending.kind = "unknown"
        pending.value = "preceding LOAD without a following LOAD or SELECT"
        out.append(pending)
    return out
