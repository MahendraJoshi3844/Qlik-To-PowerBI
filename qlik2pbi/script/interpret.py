"""Run the load script symbolically: statements in, tables with Power Query M out.

Qlik builds its data model by executing the script top to bottom. This module
does the same without data: it keeps, for every table, the list of fields it
holds and an M expression that would produce it, and updates both as each
statement is applied - so a JOIN, a later CONCATENATE or a DROP FIELD changes the
table exactly where Qlik would.

The rule for a table's M is **exact or absent**. If any step that shaped a table
cannot be expressed faithfully (a QVD source, an untranslatable WHERE, a loop),
the table gets no M; the planner writes it with its known columns and an empty
query, and the finding says which statement stopped it. A table that silently
loaded different rows than Qlik would is the failure this avoids.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from qlik2pbi.app.model import QlikApp
from qlik2pbi.expr.to_m import ToM, Untranslatable, m_identifier, m_string
from qlik2pbi.findings import Fidelity, FindingLog
from qlik2pbi.script.statements import LoadStep, Statement, parse_script, split_top, unquote


@dataclass
class ScriptTable:
    name: str
    fields: list[str] = field(default_factory=list)
    m: str | None = None
    manual: list[str] = field(default_factory=list)  # why there is no M
    assumed: list[str] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    section_access: bool = False
    mapping: bool = False
    dropped: bool = False
    line: int = 0
    tab: str = ""
    known_fields: bool = True  # False when a LOAD * from an unlisted source contributed
    #: Evidence about key uniqueness, used to decide relationship cardinality:
    #: `unique` is proven (GROUP BY on exactly that field), `non_unique` is proven
    #: (CROSSTABLE qualifiers, anything concatenated).
    unique: set[str] = field(default_factory=set)
    non_unique: set[str] = field(default_factory=set)
    #: Type evidence from the script itself: field -> integer | double | date | string.
    hints: dict[str, str] = field(default_factory=dict)

    def fail(self, reason: str) -> None:
        if reason not in self.manual:
            self.manual.append(reason)
        self.m = None


@dataclass
class LibFolder:
    name: str  # parameter (M expression) name
    connection: str
    path: str  # resolved folder path, or "" when the connection is not in the export


@dataclass
class ScriptModel:
    tables: list[ScriptTable] = field(default_factory=list)
    variables: dict[str, str] = field(default_factory=dict)
    dynamic_variables: set[str] = field(default_factory=set)
    folders: list[LibFolder] = field(default_factory=list)
    statement_count: int = 0
    features: dict[str, int] = field(default_factory=dict)  # join, concatenate, applymap, qvd, loop, ...

    def table(self, name: str) -> ScriptTable | None:
        low = name.lower()
        for t in reversed(self.tables):
            if t.name.lower() == low and not t.dropped:
                return t
        return None

    def loaded(self) -> list[ScriptTable]:
        return [t for t in self.tables if not t.dropped and not t.mapping]


def _let(steps: list[tuple[str, str]]) -> str:
    body = ",\n".join(f"    {name} = {expr}" for name, expr in steps)
    return f"let\n{body}\nin\n    {steps[-1][0]}"


def _indent(m: str, n: int = 4) -> str:
    pad = " " * n
    return m.replace("\n", "\n" + pad)


def _cols(names: list[str]) -> str:
    return "{" + ", ".join(m_string(n) for n in names) + "}"


# --- connections -------------------------------------------------------------------


def _kv(connection_string: str) -> dict[str, str]:
    out = {}
    for part in re.split(r";", connection_string.replace('"', ";")):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def sql_source(app: QlikApp, connection: str) -> tuple[str | None, str]:
    """(M source expression, note) for a CONNECT target; None when unknown."""
    conn = app.connection(connection)
    if conn is None:
        return None, f"connection '{connection}' is not in the export"
    kv = _kv(conn.connection_string)
    text = (conn.kind + " " + conn.connection_string).lower()
    host = kv.get("host") or kv.get("server") or kv.get("data source") or kv.get("address") or ""
    db = kv.get("database") or kv.get("initial catalog") or kv.get("db") or ""
    if any(w in text for w in ("sqlserver", "sql server", "sqlncli", "msoledbsql", "mssql", "azure sql")):
        return f"Sql.Database({m_string(host)}, {m_string(db)})", ""
    if "postgres" in text:
        return f"PostgreSQL.Database({m_string(host)}, {m_string(db)})", ""
    if "oracle" in text:
        return f"Oracle.Database({m_string(host)})", ""
    if "mysql" in text:
        return f"MySQL.Database({m_string(host)}, {m_string(db)})", ""
    if "snowflake" in text:
        wh = kv.get("warehouse", "")
        return f"Snowflake.Databases({m_string(host)}, {m_string(wh)}){{[Name={m_string(db)}]}}[Data]", ""
    if "teradata" in text:
        return f"Teradata.Database({m_string(host)})", ""
    dsn = kv.get("dsn") or conn.name
    return f"Odbc.DataSource({m_string('dsn=' + dsn)})", "ODBC fallback"


# --- the interpreter ---------------------------------------------------------------


class Interpreter:
    def __init__(self, app: QlikApp, log: FindingLog):
        self.app = app
        self.log = log
        self.model = ScriptModel()
        self.vars: dict[str, str] = {v.name: v.definition for v in app.variables}
        self.qualify_all = False
        self.qualified: set[str] = set()
        self.unqualified: set[str] = set()
        self.connection = ""
        self.section = "application"
        self.block: list[str] = []  # open control blocks
        self.auto = 0

    # -- helpers --
    def feature(self, name: str) -> None:
        self.model.features[name] = self.model.features.get(name, 0) + 1

    def find(self, name: str) -> ScriptTable | None:
        return self.model.table(name)

    def folder_param(self, conn_name: str) -> str:
        for f in self.model.folders:
            if f.connection.lower() == conn_name.lower():
                return f.name
        conn = self.app.connection(conn_name)
        path = conn.path or conn.connection_string if conn else ""
        param = f"Lib {conn_name}"
        self.model.folders.append(LibFolder(param, conn_name, path))
        return param

    def file_path_m(self, path: str) -> tuple[str, str]:
        """(M expression for the full path, file extension)."""
        ext = PurePosixPath(path.replace("\\", "/")).suffix.lower()
        m = re.match(r"lib://([^/\\]+)[/\\]?(.*)$", path, re.I)
        if m:
            param = self.folder_param(m.group(1))
            rest = m.group(2).replace("/", "\\")
            return f"{m_identifier(param)} & {m_string(rest)}", ext
        if re.match(r"^[A-Za-z]:[\\/]|^\\\\", path):
            return m_string(path), ext
        # A relative QlikView path: relative to the document's folder.
        param = self.folder_param("Document folder")
        return f"{m_identifier(param)} & {m_string(path.replace('/', chr(92)))}", ext

    # -- sources --
    def source(self, step: LoadStep, table: ScriptTable) -> tuple[str | None, list[str] | None]:
        src = step.source
        if src.kind == "inline":
            header = src.header
            numeric = [all(_is_number(r[i]) for r in src.rows if i < len(r) and r[i] != "") and
                       any(i < len(r) and r[i] != "" for r in src.rows) for i in range(len(header))]
            types = ", ".join(f"{m_identifier(h)} = {'number' if num else 'text'}" for h, num in zip(header, numeric))
            rows = ", ".join(
                "{" + ", ".join((v if numeric[i] and v != "" else ("null" if v == "" else m_string(v)))
                                for i, v in enumerate(r + [""] * (len(header) - len(r)))) + "}"
                for r in src.rows
            )
            table.sources.append("INLINE")
            return f"#table(type table [{types}], {{{rows}}})", list(header)
        if src.kind == "resident":
            other = self.find(src.table)
            if other is None:
                table.fail(f"RESIDENT {src.table}: that table does not exist at this point in the script")
                return None, None
            table.sources.append(f"RESIDENT {other.name}")
            if other.m is None:
                table.fail(f"reads RESIDENT {other.name}, which could not be converted")
                return None, list(other.fields)
            return other.m, list(other.fields)
        if src.kind == "sql":
            self.feature("sql")
            table.sources.append(f"SQL via {self.connection or '(no connection)'}")
            cols = _select_columns(src.sql)
            if not self.connection:
                table.fail("SQL SELECT with no CONNECT before it")
                return None, cols
            base, note = sql_source(self.app, self.connection)
            if base is None:
                table.fail(note)
                return None, cols
            if note:
                table.assumed.append(f"{self.connection}: {note}; replace with the native connector if one exists.")
            return f"Value.NativeQuery({base}, {m_string(src.sql)}, null, [EnableFolding = true])", cols
        if src.kind == "autogenerate":
            self.feature("autogenerate")
            table.fail("AUTOGENERATE (generated rows, usually a master calendar): build it as a DAX calculated table "
                       "(CALENDAR/ADDCOLUMNS) or in Power Query with List.Dates")
            return None, None
        if src.kind == "file":
            if "$(" in src.path:
                table.fail(f"source path '{src.path}' depends on a variable only known while the script runs")
                return None, None
            path_m, ext = self.file_path_m(src.path)
            fmt = dict(src.format)
            kind = fmt.get("type") or {".csv": "txt", ".txt": "txt", ".tsv": "txt", ".xlsx": "ooxml", ".xlsm": "ooxml",
                                       ".xls": "biff", ".qvd": "qvd", ".parquet": "parquet", ".json": "json",
                                       ".xml": "xml"}.get(ext, "")
            table.sources.append(f"{kind or ext or 'file'}: {src.path}")
            if kind == "qvd":
                self.feature("qvd")
                table.fail(f"reads the QVD '{src.path}'. Power Query cannot read QVD files: load from the QVD's own "
                           "source instead, or land the data as Parquet/Delta (e.g. a Fabric Lakehouse) and read that")
                return None, None
            if kind == "txt":
                delim = fmt.get("delimiter", ",")
                delim = {"\\t": "#(tab)", "'\\t'": "#(tab)", "tab": "#(tab)"}.get(delim.lower(), delim)
                enc = 65001 if fmt.get("encoding", "utf8") in ("utf8", "unicode") else 1252
                steps = [("Source", f"Csv.Document(File.Contents({path_m}), [Delimiter = {m_string(delim) if not delim.startswith('#') else chr(34) + delim + chr(34)}, "
                                    f"Encoding = {enc}, QuoteStyle = QuoteStyle.Csv])")]
                if fmt.get("labels", "embedded") == "embedded":
                    steps.append(("Headers", "Table.PromoteHeaders(Source, [PromoteAllScalars = true])"))
                return _let(steps), None
            if kind == "ooxml":
                sheet = fmt.get("table", "Sheet1")
                steps = [("Book", f"Excel.Workbook(File.Contents({path_m}), null, true)"),
                         ("Sheet", f"Book{{[Item = {m_string(sheet)}, Kind = \"Sheet\"]}}[Data]")]
                if fmt.get("labels", "embedded") == "embedded":
                    steps.append(("Headers", "Table.PromoteHeaders(Sheet, [PromoteAllScalars = true])"))
                return _let(steps), None
            if kind == "parquet":
                return f"Parquet.Document(File.Contents({path_m}))", None
            table.fail(f"file format '{kind or ext}' of '{src.path}' is not mapped")
            return None, None
        table.fail(f"source kind '{src.kind}'")
        return None, None

    # -- one LOAD/SELECT step on top of an input --
    def apply_step(self, step: LoadStep, m: str | None, fields: list[str] | None, table: ScriptTable,
                   ) -> tuple[str | None, list[str] | None]:
        if step.kind == "select":
            return m, fields
        if step.while_:
            table.fail("WHILE clause (row generation loop)")
        star = any(f.is_star for f in step.fields)
        explicit = [f for f in step.fields if not f.is_star]
        available = set(fields or []) | ({f.expr.strip().strip("[]\"") for f in explicit if f.is_plain} if fields is None else set())
        out_fields: list[str] = list(fields or []) if star else []
        if star and fields is None:
            table.known_fields = False
        steps: list[tuple[str, str]] = [("Input", _indent(m))] if m is not None else []
        tm = ToM(fields=available if fields is not None else None, lookup=self.applymap)
        cur = "Input"
        if step.where and m is not None:
            try:
                pred = tm.translate(step.where)
                steps.append(("Filtered", f"Table.SelectRows({cur}, each {pred})"))
                cur = "Filtered"
            except Untranslatable as exc:
                table.fail(f"WHERE {step.where}: {exc}")
        if step.group_by:
            return self.group_by(step, m, fields, table, steps, cur)
        renames: list[tuple[str, str]] = []
        added: list[str] = []
        for i, f in enumerate(explicit):
            alias = f.alias
            if f.is_plain:
                src_name = unquote(f.expr)
                if fields is not None and src_name not in fields and src_name.lower() not in {x.lower() for x in fields}:
                    table.fail(f"field '{src_name}' is not in the source at this point")
                if src_name != alias:
                    tmp = f"__c{i}"
                    steps.append((f"Copy{i}", f"Table.DuplicateColumn({cur}, {m_string(src_name)}, {m_string(tmp)})"))
                    cur = f"Copy{i}"
                    renames.append((tmp, alias))
                elif alias not in out_fields:
                    pass
            else:
                if m is None:
                    continue
                _type_hints(f.expr, alias, table.hints)
                try:
                    expr = tm.translate(f.expr)
                    tmp = f"__c{i}"
                    steps.append((f"Add{i}", f"Table.AddColumn({cur}, {m_string(tmp)}, each {expr})"))
                    cur = f"Add{i}"
                    renames.append((tmp, alias))
                    added.append(alias)
                except Untranslatable as exc:
                    table.fail(f"field '{alias}' = {f.expr}: {exc}")
            if alias not in out_fields:
                out_fields.append(alias)
        for note in tm.notes:
            if note not in table.assumed:
                table.assumed.append(note)
        if m is None:
            return None, out_fields
        keep = [f for f in (fields or []) if f in out_fields] if star else []
        plain_keep = [unquote(f.expr) for f in explicit if f.is_plain and unquote(f.expr) == f.alias]
        select = keep + [p for p in plain_keep if p not in keep] + [t for t, _ in renames]
        steps.append(("Selected", f"Table.SelectColumns({cur}, {_cols(select)})"))
        cur = "Selected"
        if renames:
            pairs = "{" + ", ".join("{" + m_string(a) + ", " + m_string(b) + "}" for a, b in renames) + "}"
            steps.append(("Renamed", f"Table.RenameColumns({cur}, {pairs})"))
            cur = "Renamed"
        if step.distinct:
            steps.append(("Distinct", f"Table.Distinct({cur})"))
        return _let(steps), out_fields

    def group_by(self, step, m, fields, table, steps, cur):
        self.feature("group_by")
        keys = step.group_by
        aggs, out = [], list(keys)
        fns = {"sum": "List.Sum", "min": "List.Min", "max": "List.Max", "avg": "List.Average", "count": "List.Count"}
        for f in step.fields:
            if f.is_star:
                table.fail("LOAD * with GROUP BY")
                continue
            if f.is_plain and unquote(f.expr) in keys:
                continue
            mt = re.fullmatch(r"\s*(sum|min|max|avg|count)\s*\(\s*(\[[^\]]+\]|\"[^\"]+\"|[\w .]+?)\s*\)\s*", f.expr, re.I)
            if not mt:
                table.fail(f"GROUP BY aggregate '{f.expr}' is not a simple Sum/Min/Max/Avg/Count of a field")
                continue
            col = unquote(mt.group(2))
            fn = fns[mt.group(1).lower()]
            # `_` is the group's rows; Count ignores nulls, as Qlik's Count(field) does.
            list_expr = f"Table.Column(_, {m_string(col)})"
            if fn == "List.Count":
                list_expr = f"List.RemoveNulls({list_expr})"
                table.hints[f.alias] = "integer"
            else:
                table.hints.setdefault(col, "double")
                table.hints[f.alias] = "double"
            aggs.append("{" + f"{m_string(f.alias)}, each {fn}({list_expr})" + "}")
            out.append(f.alias)
        if m is None or table.manual:
            return None, out
        if len(keys) == 1:
            table.unique.add(keys[0])
        steps.append(("Grouped", f"Table.Group({cur}, {_cols(keys)}, {{{', '.join(aggs)}}})"))
        return _let(steps), out

    def applymap(self, map_name: str, key_m: str, default_m: str) -> str:
        self.feature("applymap")
        mt = next((t for t in reversed(self.model.tables) if t.mapping and t.name.lower() == map_name.lower()), None)
        if mt is None or mt.m is None or len(mt.fields) < 2:
            raise Untranslatable(f"ApplyMap('{map_name}'): mapping table not converted")
        k, v = mt.fields[0], mt.fields[1]
        # ApplyMap returns the first match, or the default; a keyed lookup would fail on duplicates.
        return (f"(let __m = Table.SelectRows(__map_{_ident(map_name)}, (r) => Record.Field(r, {m_string(k)}) = {key_m}) "
                f"in if Table.IsEmpty(__m) then {default_m} else Record.Field(__m{{0}}, {m_string(v)}))")

    # -- statements --
    def run(self) -> ScriptModel:
        statements = parse_script(self.app.script)
        self.model.statement_count = len(statements)
        for st in statements:
            self.statement(st)
        self.model.variables = dict(self.vars)
        # Inline the mapping tables used by ApplyMap.
        for t in self.model.tables:
            if t.m and "__map_" in t.m:
                binds = []
                for mt in self.model.tables:
                    if mt.mapping and mt.m and f"__map_{_ident(mt.name)}" in t.m:
                        binds.append((f"__map_{_ident(mt.name)}", f"Table.Buffer({_indent(mt.m, 4)})"))
                t.m = _let(binds + [("Result", _indent(t.m, 4))]) if binds else t.m
        return self.model

    def statement(self, st: Statement) -> None:
        # Dollar-sign expansion is textual and happens before the statement is
        # read, with the variables as they stand at this point of the script.
        if st.kind not in ("set", "let", "control"):
            text, _ = _expand_vars(st.text, self.vars)
            if text != st.text:
                again = parse_script(text + ";")
                if len(again) == 1:
                    line, tab = st.line, st.tab
                    st = again[0]
                    st.line, st.tab = line, tab
        kind = st.kind
        if kind == "set":
            self.vars[st.name] = _strip_quotes(st.value)
            self.model.dynamic_variables.discard(st.name)
        elif kind == "let":
            value = _eval_let(st.value, self.vars)
            if value is None:
                self.model.dynamic_variables.add(st.name)
                self.vars.pop(st.name, None)
            else:
                self.vars[st.name] = value
        elif kind == "connect":
            self.connection = st.name
        elif kind == "qualify":
            if "*" in st.names:
                self.qualify_all = True
            self.qualified |= {n.lower() for n in st.names if n != "*"}
        elif kind == "unqualify":
            if "*" in st.names:
                self.qualify_all = False
                self.qualified.clear()
            self.unqualified |= {n.lower() for n in st.names if n != "*"}
        elif kind == "section":
            self.section = st.value
        elif kind == "control":
            self.feature("control")
            word = st.value.lower()
            if word in ("for", "do", "sub", "if", "switch"):
                self.block.append(word)
            elif word in ("next", "loop", "end", "end sub", "endsub", "end if", "endif", "end switch") and self.block:
                self.block.pop()
            elif re.match(r"end\s*(if|sub|switch)", word) and self.block:
                self.block.pop()
            if word not in ("for", "do", "sub", "if", "switch", "call"):
                return
            self.log.add("script", "control statement", f"line {st.line}", Fidelity.MANUAL,
                         f"'{st.text.splitlines()[0][:80]}': script control flow is not executed; statements inside "
                         "the block were read once, as written.",
                         action="Rebuild loops/conditions in Power Query (e.g. Folder.Files + a function for FOR EACH file).")
        elif kind == "load":
            self.load(st)
        elif kind == "drop_tables":
            for name in st.names:
                t = self.find(name)
                if t is not None:
                    t.dropped = True
        elif kind == "drop_fields":
            targets = [self.find(n) for n in st.extra.split(",") if n] if st.extra else self.model.loaded()
            for t in [t for t in targets if t is not None]:
                gone = [f for f in st.names if f in t.fields]
                if gone:
                    t.fields = [f for f in t.fields if f not in gone]
                    if t.m is not None:
                        t.m = _let([("Input", _indent(t.m)), ("Dropped", f"Table.RemoveColumns(Input, {_cols(gone)})")])
        elif kind == "rename_field":
            for pair in st.names:
                old, new = pair.split("\u0000")
                for t in self.model.loaded() + [x for x in self.model.tables if x.mapping]:
                    if old in t.fields:
                        t.fields = [new if f == old else f for f in t.fields]
                        if t.m is not None:
                            t.m = _let([("Input", _indent(t.m)),
                                        ("Renamed", f"Table.RenameColumns(Input, {{{{{m_string(old)}, {m_string(new)}}}}})")])
        elif kind == "rename_table":
            for pair in st.names:
                old, new = pair.split("\u0000")
                t = self.find(old)
                if t is not None:
                    t.name = new
        elif kind == "store":
            self.feature("store")
            self.log.add("script", "store", st.name or f"line {st.line}", Fidelity.INFO,
                         f"STORE {st.name} INTO {st.value}: Qlik's QVD layer.",
                         action="Replace QVD staging with dataflows or a Fabric Lakehouse if other models reuse it.")
        elif kind == "binary":
            self.feature("binary")
            self.log.add("script", "binary load", f"line {st.line}", Fidelity.MANUAL,
                         "BINARY loads another app's entire data model.",
                         action="Migrate that app first and build this one on its semantic model (a composite model).",
                         source=st.text)
        elif kind == "include":
            self.feature("include")
            self.log.add("script", "include", f"line {st.line}", Fidelity.MANUAL,
                         "$(Include) pulls in a script file that is not in the export.",
                         action="Add the included file's statements to the export and convert again.", source=st.text)
        elif kind == "unknown":
            self.log.add("script", "statement", f"line {st.line}", Fidelity.MANUAL,
                         f"Statement not understood{': ' + st.value if st.value else ''}.",
                         action="Rebuild this step in Power Query by hand.", source=st.text[:400])

    def load(self, st: Statement) -> None:
        prefixes = {p.kind: p for p in st.prefixes}
        base = st.steps[-1]
        name = st.label or ("Section Access" if self.section == "access" else self.default_name(base))
        table = ScriptTable(name=name, line=st.line, tab=st.tab, statements=[st.text],
                            section_access=self.section == "access")
        if any(b in ("for", "do") for b in self.block):
            table.fail("loaded inside a FOR/DO loop: each iteration adds rows, which a single query cannot know")
        elif self.block:
            table.assumed.append(f"loaded inside a {'/'.join(self.block)} block, assumed to run once")
        for p in st.prefixes:
            if p.kind in ("generic", "hierarchy", "intervalmatch", "other", "buffer") and p.how not in ("buffer",):
                self.feature(p.kind)
                table.fail(f"{p.how or p.kind.upper()} prefix: rebuild by hand "
                           + {"intervalmatch": "(a range join: Table.SelectRows over a cross join, or a DAX filter)",
                              "generic": "(generic load: pivot the attribute/value pairs with Table.Pivot)",
                              "hierarchy": "(parent-child: DAX PATH()/PATHITEM() columns)"}.get(p.kind, ""))
        m, fields = self.source(base, table)
        for step in reversed(st.steps):
            m, fields = self.apply_step(step, m, fields, table)
            if table.manual:
                m = None
        if fields is None:
            fields = []
            table.known_fields = False
        if "first" in prefixes and m is not None and prefixes["first"].args:
            m = _let([("Input", _indent(m)), ("First", f"Table.FirstN(Input, {prefixes['first'].args[0]})")])
        if "crosstable" in prefixes:
            self.feature("crosstable")
            args = prefixes["crosstable"].args
            attr, data = args[0] if args else "Attribute", args[1] if len(args) > 1 else "Data"
            n = int(args[2]) if len(args) > 2 and args[2].isdigit() else 1
            qual = fields[:n]
            if m is not None:
                m = _let([("Input", _indent(m)),
                          ("Unpivoted", f"Table.UnpivotOtherColumns(Input, {_cols(qual)}, {m_string(attr)}, {m_string(data)})")])
            if not table.known_fields:
                table.fail("CROSSTABLE over a LOAD * whose columns are not listed")
            fields = qual + [attr, data]
            table.non_unique |= set(qual)
        # QUALIFY renames fields to Table.Field at load time.
        if self.qualify_all or self.qualified:
            renames = [(f, f"{name}.{f}") for f in fields
                       if (self.qualify_all or f.lower() in self.qualified) and f.lower() not in self.unqualified]
            if renames:
                fields = [dict(renames).get(f, f) for f in fields]
                if m is not None:
                    pairs = "{" + ", ".join("{" + m_string(a) + ", " + m_string(b) + "}" for a, b in renames) + "}"
                    m = _let([("Input", _indent(m)), ("Qualified", f"Table.RenameColumns(Input, {pairs})")])
        table.fields = fields
        table.m = m if not table.manual else None
        if "mapping" in prefixes:
            table.mapping = True
            self.feature("mapping")
            self.model.tables.append(table)
            return
        if "join" in prefixes:
            self.join(table, prefixes["join"])
            return
        if "keep" in prefixes:
            self.keep(table, prefixes["keep"])
            return
        target = None
        if "concatenate" in prefixes:
            target = self.find(prefixes["concatenate"].target) if prefixes["concatenate"].target else self.last()
            if target is None:
                table.fail("CONCATENATE into a table that does not exist")
        elif "noconcatenate" not in prefixes:
            # Qlik concatenates automatically into a table with the same set of fields.
            target = next((t for t in self.model.loaded()
                           if t.section_access == table.section_access and t.known_fields and table.known_fields
                           and {f.lower() for f in t.fields} == {f.lower() for f in fields} and fields), None)
            if target is not None:
                table.assumed.append(f"automatically concatenated into '{target.name}' (identical field set)")
        if target is not None:
            self.concatenate(target, table)
            return
        if self.find(name) is not None:
            # Qlik gives a clashing name a suffix.
            n = 1
            while self.find(f"{name}-{n}") is not None:
                n += 1
            table.name = f"{name}-{n}"
        self.model.tables.append(table)

    def last(self) -> ScriptTable | None:
        loaded = self.model.loaded()
        return loaded[-1] if loaded else None

    def default_name(self, step: LoadStep) -> str:
        src = step.source
        if src.kind == "file":
            return PurePosixPath(src.path.replace("\\", "/")).stem or "Table"
        if src.kind == "sql":
            m = re.search(r"\bfrom\s+([\w.\[\]\"]+)", src.sql, re.I)
            if m:
                return unquote(m.group(1).split(".")[-1])
        if src.kind == "resident":
            return src.table
        self.auto += 1
        return f"Table{self.auto}"

    def concatenate(self, target: ScriptTable, new: ScriptTable) -> None:
        self.feature("concatenate")
        # Rows from two loads: nothing is known to be unique any more.
        target.non_unique |= set(target.fields) | set(new.fields)
        target.unique.clear()
        target.statements += new.statements
        target.sources += new.sources
        target.assumed += [a for a in new.assumed if a not in target.assumed]
        for r in new.manual:
            target.fail(r)
        target.fields = target.fields + [f for f in new.fields if f not in target.fields]
        target.known_fields = target.known_fields and new.known_fields
        if target.m is not None and new.m is not None:
            target.m = f"Table.Combine({{\n    {_indent(target.m)},\n    {_indent(new.m)}\n}})"
        else:
            target.fail("a CONCATENATE into it could not be converted")

    def join(self, new: ScriptTable, prefix) -> None:
        self.feature("join")
        target = self.find(prefix.target) if prefix.target else self.last()
        if target is None:
            new.fail("JOIN into a table that does not exist")
            self.model.tables.append(new)
            return
        target.statements += new.statements
        target.sources += new.sources
        keys = [f for f in new.fields if f in target.fields]
        extra = [f for f in new.fields if f not in target.fields]
        for r in new.manual:
            target.fail(r)
        if not target.known_fields or not new.known_fields:
            target.fail("JOIN where a side's fields are not listed (LOAD *): the join keys cannot be determined")
        elif not keys:
            target.fail("JOIN with no common field (a cartesian product)")
        target.fields = target.fields + extra
        if target.m is None or new.m is None:
            target.fail("a JOIN into it could not be converted")
            return
        how = prefix.how
        kind = {"left": "JoinKind.LeftOuter", "inner": "JoinKind.Inner", "right": "JoinKind.RightOuter",
                "outer": "JoinKind.FullOuter"}[how]
        temp_keys = [f"__rk{i}" for i in range(len(keys))]
        # Qlik matches 1 and '1' when it joins; Power Query does not. Keys are
        # compared as text so a CSV key (text) still meets an INLINE key (number).
        as_text = "{" + ", ".join("{" + m_string(k) + ", Text.From}" for k in keys) + "}"
        steps = [
            ("L", f"Table.TransformColumns({_indent(target.m)}, {as_text})"),
            ("R", f"Table.TransformColumns({_indent(new.m)}, {as_text})"),
            ("Joined", f"Table.NestedJoin(L, {_cols(keys)}, R, {_cols(keys)}, \"__r\", {kind})"),
        ]
        if how in ("right", "outer"):
            # Qlik keeps one key field; rows only on the right must carry the right's key values.
            steps.append(("Expanded", f"Table.ExpandTableColumn(Joined, \"__r\", {_cols(keys + extra)}, {_cols(temp_keys + extra)})"))
            cur = "Expanded"
            for i, k in enumerate(keys):
                steps.append((f"Key{i}", f"Table.AddColumn({cur}, \"__k{i}\", each Record.Field(_, {m_string(k)}) ?? Record.Field(_, \"__rk{i}\"))"))
                cur = f"Key{i}"
            steps.append(("Clean", f"Table.RemoveColumns({cur}, {_cols(keys + temp_keys)})"))
            pairs = "{" + ", ".join("{" + m_string(f"__k{i}") + ", " + m_string(k) + "}" for i, k in enumerate(keys)) + "}"
            steps.append(("Keys", f"Table.RenameColumns(Clean, {pairs})"))
            steps.append(("Ordered", f"Table.SelectColumns(Keys, {_cols(target.fields)})"))
        else:
            steps.append(("Expanded", f"Table.ExpandTableColumn(Joined, \"__r\", {_cols(extra)}, {_cols(extra)})"))
        target.m = _let(steps)

    def keep(self, new: ScriptTable, prefix) -> None:
        self.feature("keep")
        target = self.find(prefix.target) if prefix.target else self.last()
        self.model.tables.append(new)
        if target is None:
            new.fail("KEEP against a table that does not exist")
            return
        keys = [f for f in new.fields if f in target.fields]
        if not keys or not target.known_fields or not new.known_fields:
            new.fail("KEEP whose common fields cannot be determined")
            return

        as_text = "{" + ", ".join("{" + m_string(k) + ", Text.From}" for k in keys) + "}"

        def semi(keep_m: str, other_m: str) -> str:
            return _let([
                ("T", f"Table.TransformColumns({_indent(keep_m)}, {as_text})"),
                ("K", f"Table.Distinct(Table.TransformColumns(Table.SelectColumns({_indent(other_m)}, {_cols(keys)}), {as_text}))"),
                ("J", f"Table.NestedJoin(T, {_cols(keys)}, K, {_cols(keys)}, \"__k\", JoinKind.Inner)"),
                ("Kept", "Table.RemoveColumns(J, {\"__k\"})"),
            ])

        if target.m is None or new.m is None:
            new.fail("KEEP against a table that could not be converted")
            return
        tm, nm = target.m, new.m
        if prefix.how in ("left", "inner"):
            new.m = semi(nm, tm)
        if prefix.how in ("right", "inner"):
            target.m = semi(tm, nm)


# --- small helpers -------------------------------------------------------------------


_INT_FUNCS = {"year", "month", "day", "week", "weekday", "quarter", "len", "index", "div", "floor", "ceil", "iterno", "recno", "rowno"}
_DATE_FUNCS = {"date", "makedate", "monthstart", "monthend", "yearstart", "yearend", "weekstart", "quarterstart",
               "today", "addmonths", "date#", "floor_date"}


def _type_hints(expr: str, alias: str, hints: dict[str, str]) -> None:
    """What a computed field's expression says about its type and its operands' types."""
    from qlik2pbi.expr.ast import Binary, Call, FieldRef, walk  # noqa: PLC0415
    from qlik2pbi.expr.parser import ParseError, parse  # noqa: PLC0415

    try:
        node = parse(expr)
    except ParseError:
        return
    if isinstance(node, Call) and node.name.lower() in _INT_FUNCS:
        hints[alias] = "integer"
        for a in node.args[:1]:
            if node.name.lower() in ("year", "month", "day", "week", "weekday", "quarter"):
                for r in walk(a):
                    if isinstance(r, FieldRef):
                        hints.setdefault(r.name, "date")
    elif isinstance(node, Call) and node.name.lower() in _DATE_FUNCS:
        hints[alias] = "date"
    elif isinstance(node, Binary) and node.op in ("+", "-", "*", "/"):
        hints[alias] = "double"
        for r in walk(node):
            if isinstance(r, FieldRef):
                hints.setdefault(r.name, "double")
    elif isinstance(node, Binary) and node.op == "&":
        hints[alias] = "string"


def _is_number(v: str) -> bool:
    try:
        float(v)
        return True
    except ValueError:
        return False


def _plain(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))


def _ident(name: str) -> str:
    return re.sub(r"\W", "_", name)


def _strip_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def _expand_vars(text: str, variables: dict[str, str]) -> tuple[str, list[str]]:
    from qlik2pbi.expr.parser import expand  # noqa: PLC0415

    try:
        return expand(text, variables)
    except Exception:  # noqa: BLE001 - an unexpandable statement is reported by its parser
        return text, []


def _eval_let(value: str, variables: dict[str, str]) -> str | None:
    """A LET whose value is a constant; None when it depends on run time."""
    v, unknown = _expand_vars(value.strip(), variables)
    if unknown:
        return None
    if re.fullmatch(r"'(?:[^']|'')*'", v):
        return v[1:-1].replace("''", "'")
    if re.fullmatch(r"[\d.+\-*/() ]+", v) and re.search(r"\d", v):
        try:
            result = eval(v, {"__builtins__": {}}, {})  # noqa: S307 - digits and operators only
            return str(int(result)) if float(result).is_integer() else str(result)
        except Exception:  # noqa: BLE001
            return None
    if re.fullmatch(r"'(?:[^']|'')*'(\s*&\s*'(?:[^']|'')*')+", v):
        return "".join(p[1:-1] for p in re.findall(r"'(?:[^']|'')*'", v))
    return None


def _select_columns(sql: str) -> list[str] | None:
    m = re.match(r"\s*select\s+(distinct\s+)?(.*?)\s+from\s", sql, re.I | re.S)
    if not m:
        return None
    cols = []
    for part in split_top(m.group(2)):
        if part.strip() == "*" or part.strip().endswith(".*"):
            return None
        am = re.search(r"\s+as\s+(\S+)\s*$", part, re.I)
        name = am.group(1) if am else part.strip().split(".")[-1]
        cols.append(unquote(name.strip().strip('"[]`')))
    return cols


def interpret(app: QlikApp, log: FindingLog) -> ScriptModel:
    return Interpreter(app, log).run()
