"""Load-script parsing and symbolic execution."""

from qlik2pbi.app.model import Connection, QlikApp
from qlik2pbi.findings import FindingLog
from qlik2pbi.script.interpret import interpret
from qlik2pbi.script.statements import parse_script, split_statements


def run(script: str, connections=None):
    app = QlikApp(script=script, connections=connections or [])
    return interpret(app, FindingLog())


def test_split_respects_strings_brackets_comments_and_control_lines():
    parts = [p for p, _, _ in split_statements(
        "// a comment; with semicolon\nSET a = 'x;y';\nT: LOAD * INLINE [\nA\n1;2\n];\n"
        "FOR i = 1 TO 3\n  LOAD 1 AUTOGENERATE 1;\nNEXT i\n/* block; */ DROP TABLE T;")]
    assert parts[0] == "SET a = 'x;y'"
    assert parts[1].startswith("T: LOAD * INLINE [") and "1;2" in parts[1]
    assert parts[2] == "FOR i = 1 TO 3"
    assert parts[4] == "NEXT i"
    assert parts[-1] == "DROP TABLE T"


def test_preceding_load_is_folded_with_label_and_prefix():
    st = parse_script("Orders:\nNoConcatenate\nLOAD *, Year(D) as Y;\nSQL SELECT * FROM dbo.Orders;")[0]
    assert st.label == "Orders" and [p.kind for p in st.prefixes] == ["noconcatenate"]
    assert [s.kind for s in st.steps] == ["load", "select"]


def test_inline_becomes_typed_table_and_resident_rename():
    sm = run("A:\nLOAD * INLINE [\nID, Name\n1, x\n2, y\n];\nB:\nNoConcatenate LOAD ID as Key, Upper(Name) as N RESIDENT A;\nDROP TABLE A;")
    b = sm.table("B")
    assert b.fields == ["Key", "N"]
    assert '#table(type table [#"ID" = number, #"Name" = text], {{1, "x"}, {2, "y"}})' in b.m
    assert "Text.Upper([Name])" in b.m
    assert sm.table("A") is None


def test_implicit_and_explicit_concatenation_and_uniqueness():
    sm = run("T:\nLOAD * INLINE [\nK, V\n1, a\n];\nLOAD * INLINE [\nK, V\n2, b\n];")
    assert len(sm.loaded()) == 1 and "Table.Combine" in sm.loaded()[0].m
    assert "K" in sm.loaded()[0].non_unique


def test_left_join_uses_text_keys_and_outer_join_merges_keys():
    sm = run("A:\nLOAD * INLINE [\nK, X\n1, a\n];\nLEFT JOIN (A) LOAD * INLINE [\nK, Y\n1, b\n];")
    m = sm.table("A").m
    assert "JoinKind.LeftOuter" in m and 'Text.From' in m
    assert sm.table("A").fields == ["K", "X", "Y"]
    sm = run("A:\nLOAD * INLINE [\nK, X\n1, a\n];\nOUTER JOIN (A) LOAD * INLINE [\nK, Y\n2, b\n];")
    assert "JoinKind.FullOuter" in sm.table("A").m and "?? Record.Field(_, \"__rk0\")" in sm.table("A").m


def test_applymap_where_and_group_by():
    sm = run(
        "M:\nMAPPING LOAD * INLINE [\nC, R\nUS, NA\n];\n"
        "T:\nLOAD ApplyMap('M', C, 'Other') as R, V WHERE V > 1;\nLOAD * INLINE [\nC, V\nUS, 2\n];\n"
        "G:\nNoConcatenate LOAD R, Sum(V) as Total RESIDENT T GROUP BY R;"
    )
    t = sm.table("T").m
    assert "Table.SelectRows(Input, each ([V] > 1))" in t and "__map_M" in t and "Table.Buffer" in t
    g = sm.table("G")
    assert "Table.Group" in g.m and g.unique == {"R"}


def test_crosstable_unpivots():
    sm = run("T:\nCrossTable(Month, Amount, 1)\nLOAD * INLINE [\nRegion, Jan, Feb\nN, 1, 2\n];")
    t = sm.table("T")
    assert t.fields == ["Region", "Month", "Amount"]
    assert 'Table.UnpivotOtherColumns(Input, {"Region"}, "Month", "Amount")' in t.m


def test_exact_or_absent():
    sm = run("Q:\nLOAD A, B FROM [lib://X/f.qvd] (qvd);\nC:\nLOAD * AUTOGENERATE 10;\n"
             "W:\nLOAD A WHERE Wildmatch(A, 'x*');\nLOAD * INLINE [\nA\n1\n];")
    assert sm.table("Q").m is None and "QVD" in sm.table("Q").manual[0]
    assert sm.table("C").m is None
    assert sm.table("W").m is None and "WHERE" in sm.table("W").manual[0]


def test_variables_qualify_rename_and_sql_connection():
    sm = run(
        "SET vT = 'Sales';\nLIB CONNECT TO 'DW';\nQUALIFY *;\nUNQUALIFY Key;\n"
        "$(vT):\nLOAD Key, Amount;\nSQL SELECT Key, Amount FROM dbo.$(vT);\nRENAME FIELD Sales.Amount TO Amt;",
        [Connection("DW", "sqlserver", "provider=x;host=db1;database=Dwh")],
    )
    t = sm.table("Sales")
    assert t.fields == ["Key", "Amt"]
    assert 'Sql.Database("db1", "Dwh")' in t.m and "FROM dbo.Sales" in t.m


def test_loops_and_dynamic_paths_are_refused():
    sm = run("FOR EACH f IN FileList('lib://D/*.csv')\n  L: LOAD * FROM [$(f)] (txt);\nNEXT f")
    assert sm.table("L").m is None
