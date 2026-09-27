"""Chart-expression translation: one row per promise of the trust contract."""

import pytest

from qlik2pbi.expr.to_dax import ChartContext, Manual
from qlik2pbi.findings import Fidelity

S = "SUM('Orders'[SalesAmount])"


@pytest.mark.parametrize(
    "expr, dax, fidelity",
    [
        ("Sum(SalesAmount)", S, Fidelity.EXACT),
        ("Sum(Quantity * UnitPrice)", "SUMX('Orders', ('Orders'[Quantity] * 'Orders'[UnitPrice]))", Fidelity.EXACT),
        ("Sum(Quantity * UnitCost)", "SUMX('Orders', ('Orders'[Quantity] * RELATED('Products'[UnitCost])))", Fidelity.EXACT),
        ("Count(DISTINCT CustomerName)", "DISTINCTCOUNTNOBLANK('Customers'[CustomerName])", Fidelity.EXACT),
        ("Sum(SalesAmount)/Sum(Quantity)", f"DIVIDE({S}, SUM('Orders'[Quantity]))", Fidelity.EXACT),
        ("Sum({<Region={'Europe','North America'}>} SalesAmount)",
         f"CALCULATE({S}, 'Orders'[Region] IN {{\"Europe\", \"North America\"}})", Fidelity.EXACT),
        ("Sum({<Region=>} SalesAmount)", f"CALCULATE({S}, REMOVEFILTERS('Orders'[Region]))", Fidelity.EXACT),
        ('Sum({<Region={"*"}>} SalesAmount)', f"CALCULATE({S}, NOT ISBLANK('Orders'[Region]))", Fidelity.EXACT),
        ('Sum({<OrderYear={">=2020<=2023"}>} SalesAmount)',
         f"CALCULATE({S}, 'Orders'[OrderYear] >= 2020 && 'Orders'[OrderYear] <= 2023)", Fidelity.EXACT),
        ("Sum({<Region*={'Europe'}>} SalesAmount)", f"CALCULATE({S}, KEEPFILTERS('Orders'[Region] = \"Europe\"))", Fidelity.EXACT),
        ("Sum({<Region-={'Europe'}>} SalesAmount)", f"CALCULATE({S}, KEEPFILTERS(NOT ('Orders'[Region] = \"Europe\")))", Fidelity.EXACT),
        ('Sum({<Status={"Can*"}>} SalesAmount)', f"CALCULATE({S}, LEFT('Orders'[Status], 3) = \"Can\")", Fidelity.ASSUMED),
        ("Sum(TOTAL SalesAmount)", f"CALCULATE({S}, ALLSELECTED())", Fidelity.ASSUMED),
        ("Sum({1} SalesAmount)", f"CALCULATE({S}, REMOVEFILTERS())", Fidelity.ASSUMED),
        ("Avg(Aggr(Sum(SalesAmount), CustomerName))",
         f"AVERAGEX(VALUES('Customers'[CustomerName]), CALCULATE({S}))", Fidelity.ASSUMED),
        ("If(Sum(SalesAmount) > 100, 'High', 'Low')", f"IF(({S} > 100), \"High\", \"Low\")", Fidelity.EXACT),
        ("Alt(Sum(SalesAmount), 0)", f"COALESCE({S}, 0)", Fidelity.EXACT),
        ("$(vSalesExpr)", S, Fidelity.EXACT),
        ("Only(Status)", "SELECTEDVALUE('Orders'[Status])", Fidelity.EXACT),
        ("[Total Sales] * 2", "([Total Sales] * 2)", Fidelity.EXACT),
    ],
)
def test_translations(planned, expr, dax, fidelity):
    *_, tr, _ = planned
    res = tr.measure(expr)
    assert res.dax == dax
    assert res.fidelity is fidelity
    if fidelity is Fidelity.ASSUMED:
        assert res.notes


def test_num_format_moves_to_the_measure(planned):
    *_, tr, _ = planned
    res = tr.measure("Num(Sum(SalesAmount), '#,##0.0%')")
    assert res.dax == S and res.format_string == "#,##0.0%"


def test_dollar_equals_becomes_a_var_over_allselected(planned):
    *_, tr, _ = planned
    res = tr.measure('Sum({<OrderYear={"$(=Max(OrderYear))"}>} SalesAmount)')
    assert res.dax.startswith("VAR __v1 = CALCULATE(MAX('Orders'[OrderYear]), ALLSELECTED()) RETURN CALCULATE(")
    assert res.fidelity is Fidelity.ASSUMED


def test_chart_context_for_one_rank_and_column(planned):
    *_, tr, _ = planned
    ctx = ChartContext(dimensions=[("Customers", "CustomerName")], measures=["Total Sales", "Share"])
    assert tr.measure("Sum({1} SalesAmount)", ctx).dax == f"CALCULATE({S}, REMOVEFILTERS(), VALUES('Customers'[CustomerName]))"
    assert tr.measure("Rank(Sum(SalesAmount))", ctx).dax == f"RANKX(ALLSELECTED('Customers'[CustomerName]), CALCULATE({S}),, DESC, SKIP)"
    assert tr.measure("Column(1) / 2", ctx).dax == "DIVIDE([Total Sales], 2)"


@pytest.mark.parametrize(
    "expr, reason",
    [
        ("RangeSum(Above(Sum(SalesAmount), 0, RowNo()))", "visual calculation"),
        ("Sum({<Region+={'Europe'}>} SalesAmount)", "+="),
        ("Sum({BM01} SalesAmount)", "bookmark"),
        ("Sum({1}+{$} SalesAmount)", "Set operators"),
        ("Sum({<Region=P(Country)>} SalesAmount)", "P()"),
        ("GetSelectedCount(Region)", "selections"),
        ("Sum(Sum(SalesAmount))", "Aggr"),
        ("Sum($(vUnknown))", "not defined"),
        ("Sum(NoSuchField)", "not in the migrated model"),
        ("Rank(Sum(SalesAmount))", "single-dimension chart"),
        ('Sum({<Region={"=Sum(SalesAmount)>10"}>} SalesAmount)', "Search string"),
    ],
)
def test_refusals(planned, expr, reason):
    *_, tr, _ = planned
    with pytest.raises(Manual) as exc:
        tr.measure(expr)
    assert reason.lower() in str(exc.value).lower()
