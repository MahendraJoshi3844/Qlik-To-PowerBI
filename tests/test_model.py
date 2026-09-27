"""Associative model -> star schema."""

from qlik2pbi.findings import Fidelity


def rels(model):
    return {(r.from_table, r.to_table, r.from_column): (r.active, r.many_to_many) for r in model.relationships}


def test_star_relationships_and_cardinality(planned):
    _, _, model, _, log = planned
    r = rels(model)
    assert r[("Orders", "Customers", "CustomerID")] == (True, False)
    assert r[("Orders", "Products", "ProductID")] == (True, False)
    assert r[("Orders", "OrderDiscounts", "OrderID")] == (True, False)  # proven by GROUP BY
    assert r[("Orders", "Targets", "Region")] == (True, True)  # CROSSTABLE: not unique -> many-to-many
    tiers = {f.object_name: f.fidelity for f in log.items if f.object_type == "relationship" and "Written inactive" not in f.message}
    assert tiers["Orders -> OrderDiscounts"] is Fidelity.EXACT
    assert tiers["Orders -> Customers"] is Fidelity.ASSUMED


def test_synthetic_key_is_reported_not_guessed(planned):
    _, _, model, _, log = planned
    assert not any({r.from_table, r.to_table} == {"Orders", "Budget"} for r in model.relationships)
    assert any(f.object_type == "synthetic key" and f.fidelity is Fidelity.MANUAL for f in log.items)


def test_circular_reference_goes_inactive(planned):
    _, _, model, _, _ = planned
    inactive = [r for r in model.relationships if not r.active]
    assert len(inactive) == 1 and inactive[0].from_column == "OrderID"


def test_types_come_from_evidence(planned):
    _, _, model, _, _ = planned
    orders = model.table("Orders")
    types = {c.name: c.data_type for c in orders.columns}
    assert types["OrderYear"] == "integer" and types["OrderDate"] == "date"
    assert types["SalesAmount"] == "double" and types["UnitPrice"] == "double"
    assert types["CustomerID"] == "string" and types["Region"] == "string"
    assert model.table("Customers").column("CustomerName").data_type == "string"
    assert 'Table.TransformColumnTypes(Source, {{"OrderID", type text}' in orders.partition_m


def test_unconvertible_tables_keep_columns_with_empty_query(planned):
    _, _, model, _, _ = planned
    budget = model.table("Budget")
    assert not budget.has_query and [c.name for c in budget.columns] == ["Region", "OrderYear", "BudgetAmount"]
    assert model.table("MasterCalendar") is None  # no field list at all


def test_lib_folders_become_parameters(planned):
    _, _, model, _, _ = planned
    params = {p.name: p.value for p in model.parameters}
    assert params == {"Lib DataFiles": "C:\\Data\\Sales\\"}  # the QVD folder feeds no written query


def test_hierarchy_and_security_table(planned):
    _, _, model, _, _ = planned
    assert [h.levels for h in model.table("Products").hierarchies] == [["Category", "ProductName"]]
    sec = model.table("Section Access")
    assert sec.kind == "security" and sec.hidden
