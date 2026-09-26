"""Regenerate samples/sales_app - the SYNTHETIC test fixture, in `qlik app unbuild` layout.

Every construct the converter handles appears at least once, including the ones
it must refuse (QVD sources, AUTOGENERATE calendars, FOR loops, Above(), synthetic
keys, extension objects). Edit this file, not the output:

    python samples/generate_sales_app.py samples/sales_app
"""

import json
import os
import sys

out = sys.argv[1]
os.makedirs(os.path.join(out, "objects"), exist_ok=True)


def w(rel: str, content) -> None:
    with open(os.path.join(out, rel), "w", encoding="utf-8", newline="\n") as f:
        f.write(content if isinstance(content, str) else json.dumps(content, indent=2) + "\n")


SCRIPT = r"""///$tab Main
SET ThousandSep=',';
SET DecimalSep='.';
SET DateFormat='YYYY-MM-DD';
SET vDataPath = 'lib://DataFiles/';
LET vCurrentYear = 2024;
LET vLoadTime = Now();

///$tab Dimensions
Customers:
LOAD CustomerID,
     CustomerName,
     Upper(Country) as Country,
     Segment
FROM [$(vDataPath)Customers.csv]
(txt, utf8, embedded labels, delimiter is ',', msq);

Products:
LOAD ProductID,
     ProductName,
     CategoryID,
     UnitCost
FROM [lib://DataFiles/Products.xlsx]
(ooxml, embedded labels, table is Products);

LEFT JOIN (Products)
LOAD * INLINE [
CategoryID, Category
1, Bikes
2, Accessories
3, Clothing
];

Region_Map:
MAPPING LOAD * INLINE [
Country, Region
USA, North America
CANADA, North America
GERMANY, Europe
FRANCE, Europe
];

///$tab Facts
LIB CONNECT TO 'SQL_DW';

Orders:
LOAD OrderID,
     CustomerID,
     ProductID,
     OrderDate,
     Year(OrderDate) as OrderYear,
     Month(OrderDate) as OrderMonth,
     Quantity,
     UnitPrice,
     Quantity * UnitPrice as SalesAmount,
     ApplyMap('Region_Map', Upper(ShipCountry), 'Other') as Region,
     Status
WHERE Status <> 'Cancelled';
SQL SELECT OrderID, CustomerID, ProductID, OrderDate, Quantity, UnitPrice, ShipCountry, Status
FROM dbo.Orders;

Returns:
LOAD OrderID,
     ReturnDate,
     ReturnReason
FROM [lib://DataFiles/Returns.csv]
(txt, utf8, embedded labels, delimiter is ',');

OrderLines_Tmp:
LOAD OrderID, LineNo, Discount
FROM [lib://DataFiles/OrderLines.csv]
(txt, utf8, embedded labels, delimiter is ';');

OrderDiscounts:
NoConcatenate
LOAD OrderID,
     Sum(Discount) as TotalDiscount
RESIDENT OrderLines_Tmp
GROUP BY OrderID;

DROP TABLE OrderLines_Tmp;

Targets:
CrossTable(TargetMonth, Target, 1)
LOAD Region, Jan, Feb, Mar
FROM [lib://DataFiles/Targets.xlsx]
(ooxml, embedded labels, table is Targets);

// Budget comes from the QVD layer: Power Query cannot read it.
Budget:
LOAD Region,
     OrderYear,
     BudgetAmount
FROM [lib://QVD/Budget.qvd] (qvd);

///$tab Calendar
TempCal:
LOAD Min(OrderDate) as MinDate,
     Max(OrderDate) as MaxDate
RESIDENT Orders;

MasterCalendar:
LOAD Date(MinDate + IterNo() - 1) as CalendarDate
AUTOGENERATE 1 WHILE MinDate + IterNo() - 1 <= MaxDate;

DROP TABLE TempCal;

///$tab Logs
FOR EACH vFile in FileList('lib://DataFiles/Logs/*.csv')
  Logs:
  LOAD * FROM [$(vFile)] (txt, utf8, embedded labels);
NEXT vFile

STORE Orders INTO [lib://QVD/Orders.qvd] (qvd);

///$tab Security
Section Access;
LOAD * INLINE [
ACCESS, USERID, REGION
ADMIN, DOMAIN\ADMIN,
USER, DOMAIN\ALICE, NORTH AMERICA
USER, DOMAIN\BOB, EUROPE
];
Section Application;
"""

w("script.qvs", SCRIPT)
w("connections.yml", """connections:
  DataFiles:
    connectionstring: C:\\Data\\Sales\\
    type: folder
  QVD:
    connectionstring: \\\\fileserver\\qvd\\
    type: folder
  SQL_DW:
    connectionstring: 'CUSTOM CONNECT TO "provider=QvOdbcConnectorPackage.exe;driver=sqlserver;host=sqlprod01.contoso.local;database=SalesDW;"'
    type: QvOdbcConnectorPackage.exe
""")
w("config.yml", "# synthetic sample - not customer data\n")
w("app-properties.json", {"qTitle": "Sales Analytics", "qStateNames": ["StateA"]})
w("variables.json", [
    {"qName": "vSalesExpr", "qDefinition": "Sum(SalesAmount)"},
    {"qName": "vTargetMargin", "qDefinition": "0.25", "qComment": "Target margin"},
])


def mdim(qid, title, fields, grouping="N"):
    return {"qInfo": {"qId": qid, "qType": "dimension"}, "qDim": {"qGrouping": grouping, "qFieldDefs": fields},
            "qMetaDef": {"title": title}}


w("dimensions.json", [
    mdim("dCust", "Customer", ["CustomerName"]),
    mdim("dProdH", "Product drill-down", ["Category", "ProductName"], "H"),
    mdim("dGeoC", "Geography switch", ["Region", "Country"], "C"),
])


def mmea(qid, title, expr, fmt=""):
    m = {"qInfo": {"qId": qid, "qType": "measure"}, "qMeasure": {"qLabel": title, "qDef": expr}, "qMetaDef": {"title": title}}
    if fmt:
        m["qMeasure"]["qNumFormat"] = {"qType": "F", "qFmt": fmt}
    return m


w("measures.json", [
    mmea("mSales", "Total Sales", "Sum(SalesAmount)", "#,##0"),
    mmea("mQty", "Total Quantity", "Sum(Quantity)", "#,##0"),
    mmea("mOrders", "Order Count", "Count(DISTINCT OrderID)", "#,##0"),
    mmea("mAOV", "Avg Order Value", "Sum(SalesAmount) / Count(DISTINCT OrderID)", "#,##0.00"),
    mmea("mCY", "Sales CY", "Sum({<OrderYear={$(vCurrentYear)}>} SalesAmount)"),
    mmea("mMaxY", "Sales Latest Year", 'Sum({<OrderYear={"$(=Max(OrderYear))"}>} SalesAmount)'),
    mmea("mAllReg", "Sales All Regions", "Sum({<Region=>} SalesAmount)"),
    mmea("mIgnore", "Sales Ignoring Selections", "Sum({1} SalesAmount)"),
    mmea("mEU", "Sales Europe", "Sum({<Region={'Europe'}>} SalesAmount)"),
    mmea("mBig", "Big Order Sales", 'Sum({<Quantity={">=10"}>} SalesAmount)'),
    mmea("mShare", "Share of Total", "Sum(SalesAmount) / Sum(TOTAL SalesAmount)", "0.0%"),
    mmea("mAvgCust", "Avg Sales per Customer", "Avg(Aggr(Sum(SalesAmount), CustomerName))"),
    mmea("mRun", "Running Sales", "RangeSum(Above(Sum(SalesAmount), 0, RowNo()))"),
    mmea("mMargin", "Margin", "Sum(SalesAmount) - Sum(Quantity * UnitCost)"),
    mmea("mBudget", "Budget", "Sum(BudgetAmount)"),
    mmea("mVar", "Sales via Variable", "$(vSalesExpr) * (1 - $(vTargetMargin))"),
    mmea("mDup", "Revenue", "Sum( SalesAmount )"),
    mmea("mUnused", "Legacy Discount", "Sum(TotalDiscount) / Sum(SalesAmount)"),
])


def dim(field, lib=""):
    d = {"qDef": {"qFieldDefs": [field] if field else []}}
    if lib:
        d["qLibraryId"] = lib
    return d


def mea(expr="", label="", lib=""):
    m = {"qDef": {"qDef": expr, "qLabel": label}}
    if lib:
        m["qLibraryId"] = lib
    return m


def chart(qid, qtype, title, dims=(), meas=(), **extra):
    o = {"qInfo": {"qId": qid, "qType": qtype}, "visualization": qtype, "title": title,
         "qHyperCubeDef": {"qDimensions": list(dims), "qMeasures": list(meas)}}
    o.update(extra)
    return o


def listbox(qid, field):
    return {"qInfo": {"qId": qid, "qType": "listbox"}, "qListObjectDef": {"qDef": {"qFieldDefs": [field]}}}


def cell(name, col, row, colspan, rowspan):
    return {"name": name, "type": "", "col": col, "row": row, "colspan": colspan, "rowspan": rowspan}


objects = [
    {"qInfo": {"qId": "sheet1", "qType": "sheet"}, "qMetaDef": {"title": "Overview"}, "rank": 0, "cells": [
        cell("fp1", 0, 0, 4, 12), cell("kpi1", 4, 0, 5, 3), cell("bar1", 9, 0, 15, 6),
        cell("line1", 4, 3, 5, 9), cell("pie1", 9, 6, 7, 6), cell("tbl1", 16, 6, 8, 6)]},
    chart("kpi1", "kpi", "Sales", meas=[mea(lib="mSales")]),
    chart("bar1", "barchart", "Sales by Region", dims=[dim("Region")], meas=[mea(lib="mSales")],
          orientation="vertical", barGrouping={"grouping": "grouped"}),
    chart("line1", "linechart", "Sales trend", dims=[dim("OrderMonth")],
          meas=[mea(lib="mCY"), mea(lib="mMaxY")]),
    chart("pie1", "piechart", "Sales by category", dims=[dim("Category")],
          meas=[mea("Sum(SalesAmount)", "Sales")], donut={"showAsDonut": True}),
    chart("tbl1", "table", "Customers", dims=[dim("", lib="dCust"), dim("Country")],
          meas=[mea(lib="mSales"), mea(lib="mOrders"), mea("Sum(SalesAmount)/Count(DISTINCT OrderID)", "AOV")]),
    {"qInfo": {"qId": "fp1", "qType": "filterpane"}, "qChildList": {"qItems": [
        {"qInfo": {"qId": "lb1", "qType": "listbox"}}, {"qInfo": {"qId": "lb2", "qType": "listbox"}},
        {"qInfo": {"qId": "lb3", "qType": "listbox"}}]}},
    listbox("lb1", "Region"), listbox("lb2", "OrderYear"), listbox("lb3", "Segment"),

    {"qInfo": {"qId": "sheet2", "qType": "sheet"}, "qMetaDef": {"title": "Details"}, "rank": 1, "cells": [
        cell("pvt1", 0, 0, 12, 6), cell("sc1", 12, 0, 12, 6), cell("combo1", 0, 6, 8, 6),
        cell("tree1", 8, 6, 6, 6), cell("g1", 14, 6, 4, 3), cell("txt1", 14, 9, 4, 3),
        cell("ext1", 18, 6, 6, 3), cell("map1", 18, 9, 6, 3)]},
    chart("pvt1", "pivot-table", "Sales pivot", dims=[dim("Region"), dim("Category"), dim("OrderYear")],
          meas=[mea(lib="mSales")]),
    chart("sc1", "scatterplot", "Customers scatter", dims=[dim("CustomerName")],
          meas=[mea(lib="mSales"), mea(lib="mQty")]),
    chart("combo1", "combochart", "Sales and share", dims=[dim("OrderMonth")],
          meas=[mea(lib="mSales"), mea(lib="mShare")]),
    chart("tree1", "treemap", "Product mix", dims=[dim("Category"), dim("ProductName")], meas=[mea(lib="mQty")]),
    chart("g1", "gauge", "Share", meas=[mea(lib="mShare")]),
    {"qInfo": {"qId": "txt1", "qType": "text-image"}, "markdown": "Figures exclude cancelled orders."},
    chart("ext1", "sn-sankey-chart", "Flows", dims=[dim("Region"), dim("Category")], meas=[mea(lib="mSales")]),
    chart("map1", "map", "Sales map", dims=[dim("Country")], meas=[mea(lib="mSales")], qStateName="StateA"),
]
for o in objects:
    w(f"objects/{o['qInfo']['qType']}-{o['qInfo']['qId']}.json", o)

w("bookmarks.json", [{"qInfo": {"qId": "bm1", "qType": "bookmark"}, "qMetaDef": {"title": "Europe 2024"}}])
