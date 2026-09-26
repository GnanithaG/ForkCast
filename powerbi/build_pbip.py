"""Generate the ForkCast Power BI project (PBIP) from the live Postgres schema.

Output (powerbi/):
  ForkCast.pbip                       open this in Power BI Desktop (full report, 10 pages)
  ForkCast-BlankPages.pbip            same model, empty pages (fallback if a visual fails to load)
  ForkCast.SemanticModel/             model.bim: DirectQuery tables, relationships, ~110 DAX measures
  ForkCast.Report/                    report.json: pages and visuals
  ForkCast-BlankPages.Report/
  measures.dax                        every measure as text, for building by hand
  ForkCast-theme.json                 colors and fonts (View > Themes > Browse for themes)

Columns and types are read from information_schema, so the model always matches
the database. validate_model() then checks that every DAX and visual reference
points at a real table, column or measure.

Run (with Postgres up and the evaluator run at least once):
    python powerbi/build_pbip.py
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "realtime"))
from db import connect  # noqa: E402

OUT = HERE
NS = uuid.UUID("7a1d5c2e-0b1e-4d3a-9f6e-5f0c4b2a9e11")
tag = lambda *parts: str(uuid.uuid5(NS, "/".join(parts)))

# ---------------------------------------------------------------- tables
# friendly name -> (sql object, description, hidden columns)
TABLES = {
    "Date": ("dim_date", "Calendar, one row per day (marked as the date table)", []),
    "Month": ("dim_month", "One row per month", []),
    "Hour": ("dim_hour", "Opening hours 11 am - 10 pm", []),
    "Location": ("dim_location", "The six restaurants", []),
    "Scope": ("scope_labels", "Chain or single store, for KPI / OKR / menu slicers", ["scope_sort"]),
    "Menu Item": ("dim_menu_item", "30-item menu with plate cost", []),
    "Daily Location": ("daily_location", "Daily sales, floor, labor and cost facts per store", ["location_id", "business_date"]),
    "Hourly Floor": ("hourly_floor", "Seat occupancy, sales and staff hours per store per hour", ["location_id", "business_date", "hr"]),
    "Item Daily": ("item_daily", "Units and sales per menu item per day", ["location_id", "business_date", "item_id"]),
    "Monthly P&L": ("monthly_pnl", "Store P&L per month (opex estimated for the open month)", ["location_id", "month_start"]),
    "Revenue Flow": ("revenue_flow", "Source -> target dollar flows for a Sankey", ["location_id", "month_start"]),
    "P&L Waterfall": ("pnl_waterfall", "Steps from net sales to four-wall EBITDA", ["location_id", "month_start", "step_order"]),
    "Menu Engineering": ("menu_board", "Kasavana-Smith classification per item, last 90 / 365 days", ["location_id"]),
    "KPI Scorecard": ("kpi_scorecard", "Every KPI x period x scope with last year and status (Python evaluator)", ["scope", "period_sort", "sort_order", "status_sort", "scope_sort"]),
    "OKR Board": ("okr_board", "Latest OKR key-result progress (Python evaluator)", ["scope", "status_sort", "scope_sort"]),
    "OKR History": ("okr_history", "Key-result progress per evaluation day", ["scope"]),
    "Pain Points": ("alerts_open", "Open pain points with estimated annual $ impact", ["location_id", "severity_sort", "alert_key"]),
    "Live Alerts": ("alerts_live", "Pain points detected today / this hour", ["location_id", "severity_sort", "alert_key"]),
    "Forecast": ("forecast_latest", "Latest daily sales forecast with P10 / P90 band", ["location_id", "date", "month_start"]),
    "Forecast vs Actual": ("forecast_vs_actual", "Past forecasts scored against actual sales", ["location_id"]),
    "Live Today": ("live_today", "Right-now totals per store (straight from POS tables)", ["location_id"]),
    "Live Hourly": ("live_hourly_today", "Today's sales by hour next to the expected curve", ["location_id", "hr"]),
    "Live Floor": ("live_floor", "Every table and whether it is occupied now", ["location_id"]),
    "Live Staffing": ("live_staffing", "Staff on the clock now by role", ["location_id"]),
    "Data Freshness": ("v_clock", "Stream heartbeat", []),
    "Orders": ("fact_orders", "Closed guest checks (row level, for drill-through)", ["location_id", "business_date"]),
}

RELATIONSHIPS = [  # many side -> one side
    ("Daily Location", "location_id", "Location", "location_id"),
    ("Daily Location", "business_date", "Date", "date"),
    ("Hourly Floor", "location_id", "Location", "location_id"),
    ("Hourly Floor", "business_date", "Date", "date"),
    ("Hourly Floor", "hr", "Hour", "hr"),
    ("Item Daily", "location_id", "Location", "location_id"),
    ("Item Daily", "business_date", "Date", "date"),
    ("Item Daily", "item_id", "Menu Item", "item_id"),
    ("Date", "month_start", "Month", "month_start"),
    ("Monthly P&L", "location_id", "Location", "location_id"),
    ("Monthly P&L", "month_start", "Month", "month_start"),
    ("Revenue Flow", "location_id", "Location", "location_id"),
    ("Revenue Flow", "month_start", "Month", "month_start"),
    ("P&L Waterfall", "location_id", "Location", "location_id"),
    ("P&L Waterfall", "month_start", "Month", "month_start"),
    ("Menu Engineering", "location_id", "Scope", "scope"),
    ("KPI Scorecard", "scope", "Scope", "scope"),
    ("OKR Board", "scope", "Scope", "scope"),
    ("OKR History", "scope", "Scope", "scope"),
    ("Pain Points", "location_id", "Location", "location_id"),
    ("Live Alerts", "location_id", "Location", "location_id"),
    ("Forecast", "location_id", "Location", "location_id"),
    ("Forecast", "date", "Date", "date"),
    ("Forecast vs Actual", "location_id", "Location", "location_id"),
    ("Forecast vs Actual", "date", "Date", "date"),
    ("Live Today", "location_id", "Location", "location_id"),
    ("Live Hourly", "location_id", "Location", "location_id"),
    ("Live Hourly", "hr", "Hour", "hr"),
    ("Live Floor", "location_id", "Location", "location_id"),
    ("Live Staffing", "location_id", "Location", "location_id"),
    ("Orders", "location_id", "Location", "location_id"),
    ("Orders", "business_date", "Date", "date"),
]

SORT_BY = {  # (table, column) -> sort column
    ("Date", "month_label"): "month_start", ("Date", "weekday"): "weekday_num",
    ("Month", "month_label"): "month_start", ("Hour", "hour_label"): "hr",
    ("Live Hourly", "hour_label"): "hr", ("P&L Waterfall", "step"): "step_order",
    ("KPI Scorecard", "kpi_name"): "sort_order", ("KPI Scorecard", "period"): "period_sort",
    ("Scope", "scope_label"): "scope_sort",
}

# ---------------------------------------------------------------- measures
USD0, USD2, PCT1, PCT0, INT, DEC2, DEC1 = (r"\$#,0;(\$#,0);\$#,0", r"\$#,0.00;(\$#,0.00);\$#,0.00", "0.0%", "0%",
                                           "#,0", "#,0.00", "#,0.0")
LY = "DATEADD('Date'[date], -364, DAY)"   # same weekday last year

def status_pair(name, expr, good, warn, lower_better=True, labels=("On target", "Watch", "Over")):
    op = "<=" if lower_better else ">="
    return [
        (f"{name} Status", f"VAR v = {expr} RETURN SWITCH(TRUE(), ISBLANK(v), BLANK(), v {op} {good}, \"{labels[0]}\", v {op} {warn}, \"{labels[1]}\", \"{labels[2]}\")", None, "Status"),
        (f"{name} Color", f"VAR v = {expr} RETURN SWITCH(TRUE(), ISBLANK(v), BLANK(), v {op} {good}, \"#0A7D0A\", v {op} {warn}, \"#D98A00\", \"#B3261E\")", None, "Status"),
    ]

D = "'Daily Location'"
MEASURES = {
    "Daily Location": [
        ("Net Sales", f"SUM({D}[net_sales])", USD0, "Revenue"),
        ("Gross Sales", f"SUM({D}[gross_sales])", USD0, "Revenue"),
        ("Total Covers", f"SUM({D}[covers])", INT, "Revenue"),
        ("Total Checks", f"SUM({D}[checks])", INT, "Revenue"),
        ("Average Check", "DIVIDE([Net Sales], [Total Checks])", USD2, "Revenue"),
        ("Sales per Cover", "DIVIDE([Net Sales], [Total Covers])", USD2, "Revenue"),
        ("Dine-in Sales", f"SUM({D}[dine_in_sales])", USD0, "Revenue"),
        ("Takeout Sales", f"SUM({D}[takeout_sales])", USD0, "Revenue"),
        ("Delivery Sales", f"SUM({D}[delivery_sales])", USD0, "Revenue"),
        ("Off-Premise Share", "DIVIDE([Takeout Sales] + [Delivery Sales], [Net Sales])", PCT1, "Revenue"),
        ("Lunch Sales", f"SUM({D}[lunch_sales])", USD0, "Revenue"),
        ("Dinner Sales", f"SUM({D}[dinner_sales])", USD0, "Revenue"),
        ("Net Sales LY", f"CALCULATE([Net Sales], {LY})", USD0, "Revenue"),
        ("Net Sales vs LY %", "DIVIDE([Net Sales] - [Net Sales LY], [Net Sales LY])", "+0.0%;-0.0%;0.0%", "Revenue"),
        ("Covers LY", f"CALCULATE([Total Covers], {LY})", INT, "Revenue"),
        ("Covers vs LY %", "DIVIDE([Total Covers] - [Covers LY], [Covers LY])", "+0.0%;-0.0%;0.0%", "Revenue"),
        ("Seat Utilization", f"DIVIDE(SUM({D}[occupied_seat_min]), SUM({D}[available_seat_min]))", PCT1, "Floor"),
        ("RevPASH", f"DIVIDE([Dine-in Sales], DIVIDE(SUM({D}[available_seat_min]), 60))", USD2, "Floor"),
        ("Table Turns per Day", f"DIVIDE(SUM({D}[dine_in_checks]), SUM({D}[tables]))", DEC2, "Floor"),
        ("Avg Dwell (min)", f"DIVIDE(SUM({D}[dwell_min_total]), SUM({D}[dine_in_checks]))", DEC1, "Floor"),
        ("Lost Covers", f"SUM({D}[lost_covers])", INT, "Floor"),
        ("Walk-away Rate", f"DIVIDE(SUM({D}[lost_parties]), SUM({D}[dine_in_checks]) + SUM({D}[lost_parties]))", PCT1, "Floor"),
        ("Large-Party Walk-away Rate", f"DIVIDE(SUM({D}[lost_large_parties]), SUM({D}[dine_large_parties]) + SUM({D}[lost_large_parties]))", PCT1, "Floor"),
        ("Lost Sales (est.)", "[Lost Covers] * [Sales per Cover]", USD0, "Floor"),
        ("Avg Wait (min)", f"DIVIDE(SUM({D}[wait_min_total]), SUM({D}[parties_waited]))", DEC1, "Floor"),
        ("Labor Cost", f"SUM({D}[labor_cost])", USD0, "Labor"),
        ("Labor Hours", f"SUM({D}[labor_hours])", INT, "Labor"),
        ("Labor %", "DIVIDE([Labor Cost], [Net Sales])", PCT1, "Labor"),
        ("Labor % LY", f"CALCULATE([Labor %], {LY})", PCT1, "Labor"),
        ("Sales per Labor Hour", "DIVIDE([Net Sales], [Labor Hours])", USD2, "Labor"),
        ("Covers per Labor Hour", "DIVIDE([Total Covers], [Labor Hours])", DEC2, "Labor"),
        ("Food Sales", f"SUM({D}[food_sales])", USD0, "Cost of goods"),
        ("Food Purchases", f"SUM({D}[purchases_food])", USD0, "Cost of goods"),
        ("Food Cost %", "DIVIDE([Food Purchases], [Food Sales])", PCT1, "Cost of goods"),
        ("Recipe Food Cost %", f"DIVIDE(SUM({D}[theoretical_food_cost]), [Food Sales])", PCT1, "Cost of goods"),
        ("Food Cost Variance (pts)", "([Food Cost %] - [Recipe Food Cost %]) * 100", "0.0", "Cost of goods"),
        ("Beverage Cost %", f"DIVIDE(SUM({D}[purchases_bev]), SUM({D}[beverage_sales]))", PCT1, "Cost of goods"),
        ("COGS", f"SUM({D}[purchases_food]) + SUM({D}[purchases_bev]) + SUM({D}[purchases_packaging])", USD0, "Cost of goods"),
        ("Waste Cost", f"SUM({D}[waste_cost])", USD0, "Cost of goods"),
        ("Waste %", "DIVIDE([Waste Cost], [Food Purchases])", PCT1, "Cost of goods"),
        ("Prime Cost", "[COGS] + [Labor Cost]", USD0, "Profit"),
        ("Prime Cost %", "DIVIDE([Prime Cost], [Net Sales])", PCT1, "Profit"),
        ("Prime Cost % LY", f"CALCULATE([Prime Cost %], {LY})", PCT1, "Profit"),
        ("Discount Rate", f"DIVIDE(SUM({D}[discounts]), [Gross Sales])", PCT1, "Profit"),
        ("Delivery Commissions (est.)", f"SUM({D}[delivery_commission_est])", USD0, "Profit"),
        *status_pair("Prime Cost %", "[Prime Cost %]", 0.65, 0.68),
        *status_pair("Labor %", "[Labor %]", 0.32, 0.35),
        *status_pair("Food Cost %", "[Food Cost %]", 0.33, 0.35),
        *status_pair("Waste %", "[Waste %]", 0.045, 0.06, labels=("On target", "Watch", "High")),
    ],
    "Hourly Floor": [
        ("Hourly Seat Utilization", "DIVIDE(SUM('Hourly Floor'[occupied_seat_min]), SUM('Hourly Floor'[available_seat_min]))", PCT0, "Floor by hour"),
        ("Hourly RevPASH", "DIVIDE(SUM('Hourly Floor'[dine_sales]), DIVIDE(SUM('Hourly Floor'[available_seat_min]), 60))", USD2, "Floor by hour"),
        ("Hourly Sales", "SUM('Hourly Floor'[net_sales])", USD0, "Floor by hour"),
        ("Hourly Covers", "SUM('Hourly Floor'[covers])", INT, "Floor by hour"),
        ("Hourly Lost Parties", "SUM('Hourly Floor'[lost_parties])", INT, "Floor by hour"),
        ("Staff Hours", "SUM('Hourly Floor'[staff_hours])", DEC1, "Floor by hour"),
        ("Covers per Staff Hour", "DIVIDE([Hourly Covers], [Staff Hours])", DEC2, "Floor by hour"),
    ],
    "Item Daily": [
        ("Item Units", "SUM('Item Daily'[units])", INT, "Menu"),
        ("Item Sales", "SUM('Item Daily'[sales])", USD0, "Menu"),
        ("Item Margin", "SUM('Item Daily'[sales]) - SUM('Item Daily'[theoretical_cost])", USD0, "Menu"),
    ],
    "Monthly P&L": [
        ("P&L Net Sales", "SUM('Monthly P&L'[net_sales])", USD0, "P&L"),
        ("P&L COGS", "SUM('Monthly P&L'[total_cogs])", USD0, "P&L"),
        ("P&L Labor", "SUM('Monthly P&L'[labor_cost])", USD0, "P&L"),
        ("P&L Operating Expenses", "SUM('Monthly P&L'[opex])", USD0, "P&L"),
        ("Four-Wall EBITDA", "SUM('Monthly P&L'[four_wall_ebitda])", USD0, "P&L"),
        ("EBITDA Margin", "DIVIDE([Four-Wall EBITDA], [P&L Net Sales])", PCT1, "P&L"),
        ("Occupancy %", "DIVIDE(SUM('Monthly P&L'[rent]), [P&L Net Sales])", PCT1, "P&L"),
        *status_pair("EBITDA Margin", "[EBITDA Margin]", 0.17, 0.12, lower_better=False, labels=("Healthy", "Thin", "Weak")),
    ],
    "Revenue Flow": [("Flow Amount", "SUM('Revenue Flow'[amount])", USD0, None)],
    "P&L Waterfall": [("Waterfall Amount", "SUM('P&L Waterfall'[amount])", USD0, None)],
    "Menu Engineering": [
        ("Units Sold", "SUM('Menu Engineering'[units])", INT, None),
        ("Menu Mix", "SUM('Menu Engineering'[menu_mix])", PCT1, None),
        ("Margin per Plate", "DIVIDE(SUM('Menu Engineering'[total_cm]), SUM('Menu Engineering'[units]))", USD2, None),
        ("Total Margin", "SUM('Menu Engineering'[total_cm])", USD0, None),
        ("Popularity Threshold", "MAX('Menu Engineering'[popularity_threshold])", PCT1, None),
        ("Category Avg Margin", "MAX('Menu Engineering'[avg_cm_category])", USD2, None),
    ],
    "KPI Scorecard": [
        ("KPI Value", "MAX('KPI Scorecard'[value])", "#,0.####", None),
        ("KPI Display", """VAR v = [KPI Value]
VAR u = SELECTEDVALUE('KPI Scorecard'[unit])
VAR txt = SWITCH(u, "%", FORMAT(v, "0.0%"), "pts", FORMAT(v * 100, "0.0") & " pts",
    "$", IF(v >= 10000, FORMAT(v, "$#,0"), FORMAT(v, "$#,0.00")), "min", FORMAT(v, "0.0") & " min",
    "x", FORMAT(v, "0.00") & "x", FORMAT(v, "#,0"))
RETURN IF(ISBLANK(v), BLANK(), txt & "  " & SELECTEDVALUE('KPI Scorecard'[status_icon]))""", None, None),
        ("KPI Change vs LY", """VAR c = MAX('KPI Scorecard'[change_vs_ly])
VAR u = SELECTEDVALUE('KPI Scorecard'[unit])
RETURN IF(ISBLANK(c), BLANK(), IF(u IN {"%", "pts"}, FORMAT(c * 100, "+0.0;-0.0") & " pts", FORMAT(c, "+0.0%;-0.0%")))""", None, None),
        ("KPI Status Color", """SWITCH(SELECTEDVALUE('KPI Scorecard'[status]), "good", "#0A7D0A", "warning", "#D98A00", "critical", "#B3261E", "#7A8784")""", None, None),
        ("Critical KPIs", """CALCULATE(COUNTROWS('KPI Scorecard'), 'KPI Scorecard'[status] = "critical")""", INT, None),
        ("KPIs on Target", """CALCULATE(COUNTROWS('KPI Scorecard'), 'KPI Scorecard'[status] = "good")""", INT, None),
    ],
    "OKR Board": [
        ("KR Progress", "MAX('OKR Board'[progress])", PCT0, None),
        ("KR Progress (0-100%)", "MAX(0, MIN(1, [KR Progress]))", PCT0, None),
        ("Objective Progress", "AVERAGEX(VALUES('OKR Board'[kr_id]), MAX(0, MIN(1, CALCULATE(MAX('OKR Board'[progress])))))", PCT0, None),
        ("KRs On Track", """CALCULATE(DISTINCTCOUNT('OKR Board'[kr_id]), 'OKR Board'[status] = "on track")""", INT, None),
        ("KRs At Risk", """CALCULATE(DISTINCTCOUNT('OKR Board'[kr_id]), 'OKR Board'[status] = "at risk")""", INT, None),
        ("KRs Off Track", """CALCULATE(DISTINCTCOUNT('OKR Board'[kr_id]), 'OKR Board'[status] = "off track")""", INT, None),
        ("KR Baseline", """VAR v = MAX('OKR Board'[baseline]) VAR u = SELECTEDVALUE('OKR Board'[unit])
RETURN IF(ISBLANK(v), BLANK(), SWITCH(u, "%", FORMAT(v, "0.0%"), "pts", FORMAT(v * 100, "0.0") & " pts", "$", FORMAT(v, "$#,0.00"), "min", FORMAT(v, "0.0") & " min", FORMAT(v, "#,0.00")))""", None, None),
        ("KR Target", """VAR v = MAX('OKR Board'[target]) VAR u = SELECTEDVALUE('OKR Board'[unit])
RETURN IF(ISBLANK(v), BLANK(), SWITCH(u, "%", FORMAT(v, "0.0%"), "pts", FORMAT(v * 100, "0.0") & " pts", "$", FORMAT(v, "$#,0.00"), "min", FORMAT(v, "0.0") & " min", FORMAT(v, "#,0.00")))""", None, None),
        ("KR Current", """VAR v = MAX('OKR Board'[current_value]) VAR u = SELECTEDVALUE('OKR Board'[unit])
RETURN IF(ISBLANK(v), BLANK(), SWITCH(u, "%", FORMAT(v, "0.0%"), "pts", FORMAT(v * 100, "0.0") & " pts", "$", FORMAT(v, "$#,0.00"), "min", FORMAT(v, "0.0") & " min", FORMAT(v, "#,0.00")))""", None, None),
        ("KR Status Color", """SWITCH(SELECTEDVALUE('OKR Board'[status]), "on track", "#0A7D0A", "at risk", "#D98A00", "off track", "#B3261E", "#7A8784")""", None, None),
    ],
    "OKR History": [("KR Progress Trend", "AVERAGE('OKR History'[progress])", PCT0, None)],
    "Pain Points": [
        ("Open Pain Points", "COUNTROWS('Pain Points')", INT, None),
        ("Critical Pain Points", """CALCULATE(COUNTROWS('Pain Points'), 'Pain Points'[severity] = "critical")""", INT, None),
        ("$ at Stake (annual)", "SUM('Pain Points'[est_annual_impact])", USD0, None),
        ("Severity Color", """SWITCH(SELECTEDVALUE('Pain Points'[severity]), "critical", "#B3261E", "warning", "#D98A00", "#2A78D6")""", None, None),
    ],
    "Forecast": [
        ("Forecast Sales", "SUM('Forecast'[sales_p50])", USD0, None),
        ("Forecast Low (P10)", "SUM('Forecast'[sales_p10])", USD0, None),
        ("Forecast High (P90)", "SUM('Forecast'[sales_p90])", USD0, None),
        ("Forecast Covers", "SUM('Forecast'[covers_p50])", INT, None),
    ],
    "Forecast vs Actual": [
        ("Forecast MAPE", "AVERAGE('Forecast vs Actual'[abs_pct_error])", PCT1, None),
        ("Within Band %", "DIVIDE(CALCULATE(COUNTROWS('Forecast vs Actual'), 'Forecast vs Actual'[within_band] = TRUE()), COUNTROWS('Forecast vs Actual'))", PCT0, None),
    ],
    "Live Today": [
        ("Sales Today", "SUM('Live Today'[sales_today])", USD0, None),
        ("Covers Today", "SUM('Live Today'[covers_today])", INT, None),
        ("Open Checks", "SUM('Live Today'[open_checks])", INT, None),
        ("Tables Occupied", "SUM('Live Today'[tables_occupied])", INT, None),
        ("Guests Seated Now", "SUM('Live Today'[guests_seated])", INT, None),
        ("Occupancy Now %", "DIVIDE(SUM('Live Today'[seats_blocked]), SUM('Live Today'[seats_total]))", PCT0, None),
        ("Staff on Clock", "SUM('Live Today'[staff_on_clock])", INT, None),
        ("Labor Cost Today", "SUM('Live Today'[labor_cost_today])", USD0, None),
        ("Labor % Today", "DIVIDE([Labor Cost Today], [Sales Today])", PCT1, None),
        ("Walk-aways Today", "SUM('Live Today'[lost_parties_today])", INT, None),
        ("Avg Wait Today (min)", "AVERAGE('Live Today'[avg_wait_min])", DEC1, None),
    ],
    "Live Hourly": [
        ("Hourly Actual Sales", "SUM('Live Hourly'[sales])", USD0, None),
        ("Hourly Expected Sales", "SUM('Live Hourly'[expected_sales])", USD0, None),
        ("Expected Sales Today", "SUM('Live Hourly'[expected_sales])", USD0, None),
        ("Sales vs Pace %", """VAR a = CALCULATE(SUM('Live Hourly'[sales]), 'Live Hourly'[is_settled] = TRUE())
VAR e = CALCULATE(SUM('Live Hourly'[expected_sales]), 'Live Hourly'[is_settled] = TRUE())
RETURN DIVIDE(a - e, e)""", "+0%;-0%;0%", None),
    ],
    "Live Floor": [
        ("Table Label", """IF(SELECTEDVALUE('Live Floor'[status]) = "occupied",
    SELECTEDVALUE('Live Floor'[party_size]) & "/" & SELECTEDVALUE('Live Floor'[seats]), "·")""", None, None),
        ("Table Color", """IF(SELECTEDVALUE('Live Floor'[status]) = "occupied", "#2A78D6", "#DCE4E1")""", None, None),
        ("Occupied Tables Now", """CALCULATE(COUNTROWS('Live Floor'), 'Live Floor'[status] = "occupied")""", INT, None),
    ],
    "Live Staffing": [("Staff On Clock by Role", "SUM('Live Staffing'[on_clock])", INT, None)],
    "Data Freshness": [
        ("Seconds Since Last Update", "MAX('Data Freshness'[seconds_since_last_write])", INT, None),
        ("Restaurant Clock", """FORMAT(MAX('Data Freshness'[now_local]), "ddd h:mm AM/PM")""", None, None),
    ],
    "Orders": [("Order Count", "COUNTROWS('Orders')", INT, None)],
}

PG_TYPES = {"text": "string", "character varying": "string", "integer": "int64", "bigint": "int64", "smallint": "int64",
            "numeric": "double", "double precision": "double", "real": "double", "date": "dateTime",
            "timestamp without time zone": "dateTime", "timestamp with time zone": "dateTime", "boolean": "boolean"}


def m_expr(sql_object: str) -> list[str]:
    return ["let",
            "    Source = PostgreSQL.Database(Server, Database),",
            f"    Data = Source{{[Schema=\"analytics\",Item=\"{sql_object}\"]}}[Data]",
            "in",
            "    Data"]


def build_model(columns: dict) -> dict:
    tables = []
    for name, (obj, desc, hidden) in TABLES.items():
        cols = []
        for col, pgtype in columns[obj]:
            c = {"name": col, "dataType": PG_TYPES[pgtype], "sourceColumn": col, "lineageTag": tag(name, col),
                 "summarizeBy": "none"}
            if pgtype == "date":
                c["formatString"] = "yyyy-mm-dd"
            if col in hidden:
                c["isHidden"] = True
            if (name, col) in SORT_BY:
                c["sortByColumn"] = SORT_BY[(name, col)]
            if name == "Date" and col == "date":
                c["isKey"] = True
            cols.append(c)
        t = {"name": name, "lineageTag": tag(name), "description": desc, "columns": cols,
             "partitions": [{"name": name, "mode": "directQuery", "source": {"type": "m", "expression": m_expr(obj)}}]}
        if name == "Date":
            t["dataCategory"] = "Time"
        ms = []
        for mname, expr, fmt, folder in MEASURES.get(name, []):
            m = {"name": mname, "expression": expr.split("\n") if "\n" in expr else expr, "lineageTag": tag(name, "m", mname)}
            if fmt:
                m["formatString"] = fmt
            if folder:
                m["displayFolder"] = folder
            ms.append(m)
        if ms:
            t["measures"] = ms
        tables.append(t)
    rels = [{"name": tag("rel", a, b, c, d), "fromTable": a, "fromColumn": b, "toTable": c, "toColumn": d}
            for a, b, c, d in RELATIONSHIPS]
    return {
        "compatibilityLevel": 1567,
        "model": {
            "culture": "en-US",
            "defaultMode": "directQuery",
            "dataAccessOptions": {"legacyRedirects": True, "returnErrorValuesAsNull": True},
            "defaultPowerBIDataSourceVersion": "powerBI_V3",
            "sourceQueryCulture": "en-US",
            "tables": tables,
            "relationships": rels,
            "expressions": [
                {"name": "Server", "kind": "m", "lineageTag": tag("param", "Server"),
                 "expression": "\"localhost:5432\" meta [IsParameterQuery=true, Type=\"Text\", IsParameterQueryRequired=true]"},
                {"name": "Database", "kind": "m", "lineageTag": tag("param", "Database"),
                 "expression": "\"forkcast\" meta [IsParameterQuery=true, Type=\"Text\", IsParameterQueryRequired=true]"},
            ],
            "annotations": [{"name": "PBI_QueryOrder", "value": json.dumps(list(TABLES))},
                            {"name": "__PBI_TimeIntelligenceEnabled", "value": "0"}],
        },
    }


# ---------------------------------------------------------------- report
class Page:
    def __init__(self, name, title, subtitle):
        self.name, self.visuals, self.z = name, [], 0
        self.text(16, 8, 900, 44, title, subtitle)

    def _container(self, x, y, w, h, single):
        self.z += 1
        cid = tag("visual", self.name, str(self.z))[:20].replace("-", "")
        cfg = {"name": cid, "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": self.z * 1000, "width": w, "height": h,
                                                              "tabOrder": self.z * 1000}}],
               "singleVisual": single}
        self.visuals.append({"x": x, "y": y, "z": self.z * 1000, "width": w, "height": h,
                             "config": json.dumps(cfg), "filters": "[]"})

    def text(self, x, y, w, h, title, subtitle=None):
        runs = [{"value": title, "textStyle": {"fontWeight": "bold", "fontSize": "18pt"}}]
        paras = [{"textRuns": runs}]
        if subtitle:
            paras.append({"textRuns": [{"value": subtitle, "textStyle": {"fontSize": "10pt", "color": "#5B6B67"}}]})
        self._container(x, y, w, h, {"visualType": "textbox", "drillFilterOtherVisuals": True,
                                     "objects": {"general": [{"properties": {"paragraphs": paras}}]}})

    def visual(self, vtype, x, y, w, h, roles: dict, title=None, objects=None):
        """roles: {role: [("m"|"c", table, field), ...]}"""
        aliases, frm, sel, proj = {}, [], [], {}
        for role, fields in roles.items():
            proj[role] = []
            for kind, table, field in fields:
                if table not in aliases:
                    a = f"t{len(aliases)}"
                    aliases[table] = a
                    frm.append({"Name": a, "Entity": table, "Type": 0})
                qref = f"{table}.{field}"
                key = "Measure" if kind == "m" else "Column"
                if not any(s["Name"] == qref for s in sel):
                    sel.append({key: {"Expression": {"SourceRef": {"Source": aliases[table]}}, "Property": field}, "Name": qref})
                p = {"queryRef": qref}
                if role in ("Category", "Rows") and not proj[role]:
                    p["active"] = True
                proj[role].append(p)
        single = {"visualType": vtype, "projections": proj,
                  "prototypeQuery": {"Version": 2, "From": frm, "Select": sel},
                  "drillFilterOtherVisuals": True}
        if vtype != "card":
            single["hasDefaultSort"] = True
        if objects:
            single["objects"] = objects
        if title:
            single["vcObjects"] = {"title": [{"properties": {
                "show": {"expr": {"Literal": {"Value": "true"}}},
                "text": {"expr": {"Literal": {"Value": "'" + title.replace("'", "''") + "'"}}}}}]}
        self._container(x, y, w, h, single)

    def slicer(self, x, y, w, h, table, column, title=None):
        self.visual("slicer", x, y, w, h, {"Values": [("c", table, column)]}, title,
                    objects={"data": [{"properties": {"mode": {"expr": {"Literal": {"Value": "'Dropdown'"}}}}}]})

    def cards(self, y, measures, h=84, x0=16, width=1248, gap=8):
        n = len(measures)
        w = (width - gap * (n - 1)) / n
        for i, (table, m) in enumerate(measures):
            self.visual("card", round(x0 + i * (w + gap)), y, round(w), h, {"Values": [("m", table, m)]})

    def section(self, ordinal, blank=False):
        return {"config": "{}", "displayName": self.name, "displayOption": 1, "filters": "[]", "height": 720,
                "name": "ReportSection" + tag("page", self.name)[:18].replace("-", ""), "ordinal": ordinal,
                "visualContainers": [] if blank else self.visuals, "width": 1280}


def build_pages():
    DL, M, L = "Daily Location", "Month", "Location"
    pages = []

    p = Page("Live Service", "Live service", "DirectQuery on the POS stream · set Page refresh to 30 seconds (Format > Page refresh)")
    p.slicer(1040, 8, 224, 50, L, "location_name", "Location")
    p.cards(64, [("Live Today", "Sales Today"), ("Live Hourly", "Sales vs Pace %"), ("Live Today", "Covers Today"),
                 ("Live Today", "Occupancy Now %"), ("Live Today", "Staff on Clock"), ("Live Today", "Labor % Today"),
                 ("Live Today", "Walk-aways Today"), ("Data Freshness", "Seconds Since Last Update")])
    p.visual("clusteredColumnChart", 16, 158, 620, 280, {"Category": [("c", "Hour", "hour_label")],
             "Y": [("m", "Live Hourly", "Hourly Actual Sales"), ("m", "Live Hourly", "Hourly Expected Sales")]},
             "Sales by hour: actual vs expected")
    p.visual("pivotTable", 652, 158, 612, 280, {"Rows": [("c", "Live Floor", "zone")], "Columns": [("c", "Live Floor", "table_no")],
             "Values": [("m", "Live Floor", "Table Label")]}, "Floor now (guests / seats)")
    p.visual("tableEx", 16, 448, 1248, 256, {"Values": [("c", "Live Alerts", "severity"), ("c", "Live Alerts", "location_name"),
             ("c", "Live Alerts", "title"), ("c", "Live Alerts", "evidence"), ("c", "Live Alerts", "recommended_action")]},
             "Needs attention now")
    pages.append(p)

    p = Page("Executive Overview", "Executive overview", "Sales, profit and guest KPIs vs last year · filter by month and store")
    p.slicer(820, 8, 210, 50, M, "month_label", "Month")
    p.slicer(1040, 8, 224, 50, L, "location_name", "Location")
    p.cards(64, [(DL, "Net Sales"), (DL, "Net Sales vs LY %"), (DL, "Total Covers"), (DL, "Sales per Cover"),
                 (DL, "Prime Cost %"), ("Monthly P&L", "EBITDA Margin"), (DL, "Seat Utilization"), (DL, "Lost Sales (est.)")])
    p.visual("clusteredColumnChart", 16, 158, 620, 270, {"Category": [("c", M, "month_label")],
             "Y": [("m", DL, "Net Sales"), ("m", DL, "Net Sales LY")]}, "Net sales vs same weeks last year")
    p.visual("lineChart", 652, 158, 612, 270, {"Category": [("c", M, "month_label")],
             "Y": [("m", DL, "Prime Cost %"), ("m", DL, "Labor %"), ("m", DL, "Food Cost %")]}, "Cost ratios by month")
    p.visual("pivotTable", 16, 438, 1248, 266, {"Rows": [("c", L, "location_name")],
             "Values": [("m", DL, "Net Sales"), ("m", DL, "Net Sales vs LY %"), ("m", DL, "Sales per Cover"), ("m", DL, "Food Cost %"),
                        ("m", DL, "Labor %"), ("m", DL, "Prime Cost %"), ("m", "Monthly P&L", "EBITDA Margin"),
                        ("m", DL, "Seat Utilization"), ("m", DL, "Walk-away Rate")]}, "Store scorecard")
    pages.append(p)

    p = Page("KPI Scorecard", "KPI scorecard", "Computed by the Python evaluator every few minutes · ● on target  ▲ watch  ■ off target")
    p.slicer(1040, 8, 224, 50, "Scope", "scope_label", "Chain or store")
    p.cards(64, [("KPI Scorecard", "KPIs on Target"), ("KPI Scorecard", "Critical KPIs"), ("Data Freshness", "Restaurant Clock")],
            x0=16, width=700)
    p.visual("pivotTable", 16, 158, 1248, 546, {"Rows": [("c", "KPI Scorecard", "category"), ("c", "KPI Scorecard", "kpi_name")],
             "Columns": [("c", "KPI Scorecard", "period")], "Values": [("m", "KPI Scorecard", "KPI Display")]},
             "Every KPI by period")
    pages.append(p)

    p = Page("OKR Tracker", "OKRs: Fall-Holiday 2026", "Key results measured against the same dates last year · progress 100% = target met")
    p.slicer(1040, 8, 224, 50, "Scope", "scope_label", "Chain or store")
    p.cards(64, [("OKR Board", "Objective Progress"), ("OKR Board", "KRs On Track"), ("OKR Board", "KRs At Risk"),
                 ("OKR Board", "KRs Off Track")])
    p.visual("clusteredBarChart", 16, 158, 500, 250, {"Category": [("c", "OKR Board", "objective")],
             "Y": [("m", "OKR Board", "Objective Progress")]}, "Progress by objective")
    p.visual("lineChart", 532, 158, 732, 250, {"Category": [("c", "OKR History", "eval_date")],
             "Y": [("m", "OKR History", "KR Progress Trend")], "Series": [("c", "OKR History", "kr_id")]},
             "Key-result progress over time")
    p.visual("tableEx", 16, 418, 1248, 286, {"Values": [("c", "OKR Board", "kr_id"), ("c", "OKR Board", "key_result"),
             ("c", "OKR Board", "owner"), ("m", "OKR Board", "KR Baseline"), ("m", "OKR Board", "KR Target"),
             ("m", "OKR Board", "KR Current"), ("m", "OKR Board", "KR Progress (0-100%)"), ("c", "OKR Board", "status"),
             ("c", "OKR Board", "note")]}, "Key results")
    pages.append(p)

    p = Page("Pain Points", "Pain points", "Detected by rules and baselines in the evaluator · $ = estimated annual profit impact")
    p.slicer(1040, 8, 224, 50, L, "location_name", "Location")
    p.cards(64, [("Pain Points", "Open Pain Points"), ("Pain Points", "Critical Pain Points"), ("Pain Points", "$ at Stake (annual)")],
            width=900)
    p.visual("clusteredBarChart", 16, 158, 400, 250, {"Category": [("c", "Pain Points", "category")],
             "Y": [("m", "Pain Points", "$ at Stake (annual)")]}, "$ at stake by area")
    p.visual("clusteredBarChart", 432, 158, 400, 250, {"Category": [("c", "Pain Points", "location_name")],
             "Y": [("m", "Pain Points", "$ at Stake (annual)")]}, "$ at stake by store")
    p.visual("clusteredColumnChart", 848, 158, 416, 250, {"Category": [("c", "Pain Points", "severity")],
             "Y": [("m", "Pain Points", "Open Pain Points")]}, "Open pain points by severity")
    p.visual("tableEx", 16, 418, 1248, 286, {"Values": [("c", "Pain Points", "severity"), ("c", "Pain Points", "location_name"),
             ("c", "Pain Points", "title"), ("c", "Pain Points", "evidence"), ("c", "Pain Points", "recommended_action"),
             ("m", "Pain Points", "$ at Stake (annual)"), ("c", "Pain Points", "first_seen")]}, "What to fix, biggest first")
    pages.append(p)

    p = Page("Revenue Flow", "Revenue flow & P&L", "Where every sales dollar goes, from channel to four-wall EBITDA")
    p.slicer(820, 8, 210, 50, M, "month_label", "Month")
    p.slicer(1040, 8, 224, 50, L, "location_name", "Location")
    p.visual("waterfallChart", 16, 64, 620, 320, {"Category": [("c", "P&L Waterfall", "step")],
             "Y": [("m", "P&L Waterfall", "Waterfall Amount")]}, "Net sales to four-wall EBITDA")
    p.visual("columnChart", 652, 64, 612, 320, {"Category": [("c", M, "month_label")],
             "Y": [("m", "Monthly P&L", "P&L COGS"), ("m", "Monthly P&L", "P&L Labor"),
                   ("m", "Monthly P&L", "P&L Operating Expenses"), ("m", "Monthly P&L", "Four-Wall EBITDA")]},
             "Monthly cost structure")
    p.visual("tableEx", 16, 394, 620, 310, {"Values": [("c", "Revenue Flow", "stage"), ("c", "Revenue Flow", "source"),
             ("c", "Revenue Flow", "target"), ("m", "Revenue Flow", "Flow Amount")]}, "Revenue flow (feeds the Sankey visual)")
    p.visual("clusteredBarChart", 652, 394, 612, 310, {"Category": [("c", L, "location_name")],
             "Y": [("m", DL, "Dine-in Sales"), ("m", DL, "Takeout Sales"), ("m", DL, "Delivery Sales")]}, "Sales by channel")
    pages.append(p)

    p = Page("Floor & Capacity", "Floor & capacity", "Seat utilization by day and hour, walk-aways and staffing against demand")
    p.slicer(820, 8, 210, 50, M, "month_label", "Month")
    p.slicer(1040, 8, 224, 50, L, "location_name", "Location")
    p.cards(64, [(DL, "Seat Utilization"), (DL, "RevPASH"), (DL, "Table Turns per Day"), (DL, "Avg Dwell (min)"),
                 (DL, "Walk-away Rate"), (DL, "Large-Party Walk-away Rate")])
    p.visual("pivotTable", 16, 158, 760, 300, {"Rows": [("c", "Date", "weekday")], "Columns": [("c", "Hour", "hour_label")],
             "Values": [("m", "Hourly Floor", "Hourly Seat Utilization")]}, "Seat utilization by weekday and hour")
    p.visual("clusteredBarChart", 792, 158, 472, 300, {"Category": [("c", L, "location_name")],
             "Y": [("m", DL, "Lost Covers")]}, "Guests turned away")
    p.visual("lineChart", 16, 468, 760, 236, {"Category": [("c", M, "month_label")], "Y": [("m", DL, "Seat Utilization")]},
             "Seat utilization by month")
    p.visual("clusteredColumnChart", 792, 468, 472, 236, {"Category": [("c", "Hour", "hour_label")],
             "Y": [("m", "Hourly Floor", "Covers per Staff Hour")]}, "Covers per staff hour, by hour")
    pages.append(p)

    p = Page("Labor & Costs", "Labor & cost of goods", "Scheduling efficiency, food cost against recipe cost, and waste")
    p.slicer(820, 8, 210, 50, M, "month_label", "Month")
    p.slicer(1040, 8, 224, 50, L, "location_name", "Location")
    p.cards(64, [(DL, "Labor %"), (DL, "Sales per Labor Hour"), (DL, "Food Cost %"), (DL, "Food Cost Variance (pts)"),
                 (DL, "Waste %"), (DL, "Prime Cost %")])
    p.visual("lineChart", 16, 158, 620, 270, {"Category": [("c", M, "month_label")], "Y": [("m", DL, "Sales per Labor Hour")],
             "Series": [("c", L, "location_name")]}, "Sales per labor hour by store")
    p.visual("clusteredBarChart", 652, 158, 612, 270, {"Category": [("c", L, "location_name")],
             "Y": [("m", DL, "Labor %"), ("m", DL, "Food Cost %")]}, "Labor and food cost by store")
    p.visual("lineChart", 16, 438, 620, 266, {"Category": [("c", M, "month_label")],
             "Y": [("m", DL, "Food Cost %"), ("m", DL, "Recipe Food Cost %")]}, "Actual vs recipe food cost")
    p.visual("clusteredBarChart", 652, 438, 612, 266, {"Category": [("c", L, "location_name")], "Y": [("m", DL, "Waste %")]},
             "Food waste by store")
    pages.append(p)

    p = Page("Menu Engineering", "Menu engineering", "Popularity vs margin per plate · Stars, Plowhorses, Puzzles and Dogs")
    p.slicer(600, 8, 200, 50, "Menu Engineering", "period", "Period")
    p.slicer(816, 8, 210, 50, "Menu Engineering", "category", "Category")
    p.slicer(1040, 8, 224, 50, "Scope", "scope_label", "Chain or store")
    p.visual("scatterChart", 16, 64, 760, 640, {"Category": [("c", "Menu Engineering", "item_name")],
             "Series": [("c", "Menu Engineering", "menu_class")], "X": [("m", "Menu Engineering", "Menu Mix")],
             "Y": [("m", "Menu Engineering", "Margin per Plate")]}, "Menu mix vs margin per plate")
    p.visual("tableEx", 792, 64, 472, 640, {"Values": [("c", "Menu Engineering", "item_name"), ("c", "Menu Engineering", "menu_class"),
             ("m", "Menu Engineering", "Units Sold"), ("m", "Menu Engineering", "Menu Mix"),
             ("m", "Menu Engineering", "Margin per Plate"), ("m", "Menu Engineering", "Total Margin")]}, "Items")
    pages.append(p)

    p = Page("Forecast", "Forecast & accuracy", "Seasonal regression per store, refreshed daily · shaded range = P10 to P90")
    p.slicer(1040, 8, 224, 50, L, "location_name", "Location")
    p.cards(64, [("Forecast", "Forecast Sales"), ("Forecast", "Forecast Covers"), ("Forecast vs Actual", "Forecast MAPE"),
                 ("Forecast vs Actual", "Within Band %")], width=1000)
    p.visual("lineChart", 16, 158, 1248, 300, {"Category": [("c", M, "month_label")],
             "Y": [("m", DL, "Net Sales"), ("m", "Forecast", "Forecast Sales"), ("m", "Forecast", "Forecast Low (P10)"),
                   ("m", "Forecast", "Forecast High (P90)")]}, "Actual and forecast net sales by month")
    p.visual("tableEx", 16, 468, 1248, 236, {"Values": [("c", "Forecast vs Actual", "date"), ("c", L, "location_name"),
             ("c", "Forecast vs Actual", "forecast"), ("c", "Forecast vs Actual", "actual"),
             ("c", "Forecast vs Actual", "abs_pct_error"), ("c", "Forecast vs Actual", "within_band")]},
             "Recent days: forecast vs actual")
    pages.append(p)
    return pages


def report_json(pages, blank=False):
    return {
        "config": json.dumps({"version": "5.43", "activeSectionIndex": 0, "linguisticSchemaSyncVersion": 0,
                              "objects": {"outspacePane": [{"properties": {"expanded": {"expr": {"Literal": {"Value": "true"}}}}}]}}),
        "layoutOptimization": 0,
        "resourcePackages": [],
        "sections": [p.section(i, blank) for i, p in enumerate(pages)],
    }


# ---------------------------------------------------------------- validation
def validate_model(model, pages):
    tables = {t["name"]: t for t in model["model"]["tables"]}
    cols = {(t, c["name"]) for t, tb in tables.items() for c in tb["columns"]}
    meas = {m["name"]: t for t, tb in tables.items() for m in tb.get("measures", [])}
    errors = []
    if len(meas) != sum(len(tb.get("measures", [])) for tb in tables.values()):
        errors.append("duplicate measure names")
    for t, tb in tables.items():
        for c in tb["columns"]:
            if "sortByColumn" in c and (t, c["sortByColumn"]) not in cols:
                errors.append(f"sortBy {t}.{c['sortByColumn']} missing")
        for m in tb.get("measures", []):
            expr = "\n".join(m["expression"]) if isinstance(m["expression"], list) else m["expression"]
            for tt, cc in re.findall(r"'([^']+)'\[([^\]]+)\]", expr):
                if (tt, cc) not in cols:
                    errors.append(f"measure {m['name']}: column '{tt}'[{cc}] not found")
            bare = re.sub(r"'[^']+'\[[^\]]+\]", "", expr)
            for ref in re.findall(r"\[([^\]]+)\]", bare):
                if ref not in meas:
                    errors.append(f"measure {m['name']}: [{ref}] is not a measure")
    colnames = {c.lower() for _, c in cols}
    for name in meas:
        if name.lower() in colnames:
            errors.append(f"measure name '{name}' collides with a column name")
    for r in model["model"]["relationships"]:
        for side in (("fromTable", "fromColumn"), ("toTable", "toColumn")):
            if (r[side[0]], r[side[1]]) not in cols:
                errors.append(f"relationship column {r[side[0]]}.{r[side[1]]} missing")
    for p in pages:
        for v in p.visuals:
            sv = json.loads(v["config"])["singleVisual"]
            for sel in sv.get("prototypeQuery", {}).get("Select", []):
                ent = next(f["Entity"] for f in sv["prototypeQuery"]["From"]
                           if f["Name"] == (sel.get("Measure") or sel.get("Column"))["Expression"]["SourceRef"]["Source"])
                if "Measure" in sel:
                    name = sel["Measure"]["Property"]
                    if meas.get(name) != ent:
                        errors.append(f"page {p.name}: measure {ent}.{name} not found")
                else:
                    if (ent, sel["Column"]["Property"]) not in cols:
                        errors.append(f"page {p.name}: column {ent}.{sel['Column']['Property']} not found")
    return errors, len(meas)


def main():
    con = connect()
    columns = {}
    for obj, *_ in TABLES.values():
        rows = con.execute("""SELECT column_name, data_type FROM information_schema.columns
                              WHERE table_schema = 'analytics' AND table_name = %s ORDER BY ordinal_position""", (obj,)).fetchall()
        if not rows:
            raise SystemExit(f"analytics.{obj} not found: run the evaluator once before building the model")
        columns[obj] = rows
    model = build_model(columns)
    pages = build_pages()
    errors, n_measures = validate_model(model, pages)
    if errors:
        raise SystemExit("model validation failed:\n  " + "\n  ".join(errors))

    sm = OUT / "ForkCast.SemanticModel"
    sm.mkdir(parents=True, exist_ok=True)
    (sm / "model.bim").write_text(json.dumps(model, indent=2, ensure_ascii=False), encoding="utf-8")
    (sm / "definition.pbism").write_text(json.dumps({"version": "1.0", "settings": {}}, indent=2))
    for rep, blank in (("ForkCast.Report", False), ("ForkCast-BlankPages.Report", True)):
        rd = OUT / rep
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "definition.pbir").write_text(json.dumps({"version": "1.0", "datasetReference": {
            "byPath": {"path": "../ForkCast.SemanticModel"}, "byConnection": None}}, indent=2))
        (rd / "report.json").write_text(json.dumps(report_json(pages, blank), indent=2, ensure_ascii=False), encoding="utf-8")
        pbip = rep.replace(".Report", ".pbip")
        (OUT / pbip).write_text(json.dumps({
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
            "version": "1.0", "artifacts": [{"report": {"path": rep}}], "settings": {"enableAutoRecovery": True}}, indent=2))

    # measures.dax for building by hand
    lines = ["// ForkCast DAX measures. Paste each into Modeling > New measure on the named table.\n"]
    for t, ms in MEASURES.items():
        lines.append(f"\n// ===== {t} =====")
        for name, expr, fmt, folder in ms:
            lines.append(f"\n{name} =\n{expr}")
            if fmt:
                lines.append(f"// format: {fmt}")
    (OUT / "measures.dax").write_text("\n".join(lines))
    print(f"model: {len(TABLES)} tables, {len(RELATIONSHIPS)} relationships, {n_measures} measures")
    print(f"report: {len(pages)} pages, {sum(len(p.visuals) for p in pages)} visuals · validation passed")


if __name__ == "__main__":
    main()
