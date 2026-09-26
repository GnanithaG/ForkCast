# Power BI guide

The Power BI project reads the live PostgreSQL database in **DirectQuery** mode, so visuals show the data the streamer and evaluator just wrote. The Live Service page can refresh itself every 30 seconds.

What's in `powerbi/`:

| File | What it is |
|---|---|
| `ForkCast.pbip` | Open this. The semantic model plus a 10-page report (98 visuals). |
| `ForkCast-BlankPages.pbip` | The same model with the 10 pages left empty. Use it if the full report won't open. |
| `ForkCast.SemanticModel/model.bim` | 26 DirectQuery tables, 32 relationships, 129 DAX measures, `Server` and `Database` parameters |
| `measures.dax` | Every measure as plain text, for building the model by hand |
| `ForkCast-theme.json` | Colors and fonts |
| `build_pbip.py` | Regenerates all of the above from the live schema, and checks every reference |

> **Note:** I generated and validated these files with scripts, but I couldn't open them in Power BI Desktop, which only runs on Windows. The script checks that every table, column, relationship and measure reference exists in the database and the model. If Desktop still rejects a visual, use `ForkCast-BlankPages.pbip` with the page catalogue below, or follow the manual route at the end.

## 1. Start the data

```bash
docker compose up -d        # Postgres + one-off setup + streamer + evaluator
# or, with a local PostgreSQL 14+:
python realtime/setup.py --start
```

Setup takes about 90 seconds. It loads 24 months of history, catches the stream up to now, builds the analytics layer, backfills 28 days of forecasts and OKR history, and regenerates the Power BI project.

The restaurants trade from 11 am to 11 pm Eastern. Outside those hours the live pages stay quiet. For a demo at any hour, run the streamer on a fast clock:

```bash
python realtime/streamer.py --speed 60     # 1 real second = 1 restaurant minute
```

Demo mode writes events ahead of real time. To get back to real time afterwards, run `python realtime/streamer.py --reset-stream --catchup-only`.

## 2. Open and connect

1. Install **Power BI Desktop** (free, from the Microsoft Store).
2. **File → Open → `ForkCast.pbip`**. If Desktop doesn't offer .pbip files, turn on *File → Options → Preview features → Power BI Project (.pbip) save option* and restart.
3. When asked for credentials for `localhost:5432;forkcast`, choose **Database**, user `postgres`, password `forkcast`.
4. **If you see an SSL or certificate error**, the local Postgres has no certificate. Go to *File → Options and settings → Data source settings → localhost:5432;forkcast → Edit permissions*, untick **Encrypt connections**, and refresh.
5. Postgres on another machine? Open *Transform data → Edit parameters* and change **Server** (for example `10.0.0.5:5432`) and **Database**.
6. Apply the theme: *View → Themes → Browse for themes → `ForkCast-theme.json`*.

## 3. Turn on live refresh

- **Live Service page:** Format page → **Page refresh** → On → **30 seconds**.
- **Other pages:** 5 minutes is plenty, since the evaluator recomputes KPIs, OKRs and pain points every 2 minutes.
- The **Seconds Since Last Update** card shows how fresh the feed is. If it keeps climbing, the streamer has stopped.

## 4. Finishing touches (about 10 minutes)

These settings are easiest to set by hand, so the generated file leaves them out:

| Where | Setting |
|---|---|
| KPI Scorecard matrix | *KPI Display → Conditional formatting → Font color → Field value → `KPI Status Color`*. Turn off row subtotals. |
| KPI cards (Prime Cost %, Labor %, Food Cost %, Waste %, EBITDA Margin) | *Callout value → fx → Field value →* the matching `… Color` measure |
| OKR Key results table | *KR Progress (0-100%) → Cell elements → Data bars*. Set the `status` column font color from `KR Status Color`. |
| Live floor matrix | *Table Label → Background color → Field value → `Table Color`* |
| Seat utilization heatmap | *Hourly Seat Utilization → Background color → Gradient*, light to `#1C5CAB` |
| Pain Points table | Sort by `$ at Stake (annual)` descending. Set `severity` font color from `Severity Color`. |
| Forecast line chart | Select *Forecast Sales → Error bars → Upper = Forecast High (P90), Lower = Forecast Low (P10), Bar type: Shaded*, then hide the two band lines |
| Revenue Flow page | *Get more visuals → "Sankey Chart" (Microsoft)*. Source = `Revenue Flow[source]`, Destination = `target`, Weight = `Flow Amount`. |

## 5. Page catalogue

Use this list to check the report, or to rebuild it on the blank-pages version. `DL` = Daily Location.

**1. Live Service** (Location slicer). *What is happening on the floor right now?*
- Cards: Sales Today, Sales vs Pace %, Covers Today, Occupancy Now %, Staff on Clock, Labor % Today, Walk-aways Today, Seconds Since Last Update
- Clustered column: Hour[hour_label] × Hourly Actual Sales, Hourly Expected Sales
- Matrix: rows Live Floor[zone], columns Live Floor[table_no], value Table Label
- Table: Live Alerts[severity, location_name, title, evidence, recommended_action]

**2. Executive Overview** (Month, Location slicers). *Are we growing, and at what margin?*
- Cards: Net Sales, Net Sales vs LY %, Total Covers, Sales per Cover, Prime Cost %, EBITDA Margin, Seat Utilization, Lost Sales (est.)
- Column chart: Month × Net Sales, Net Sales LY
- Line chart: Month × Prime Cost %, Labor %, Food Cost %
- Matrix: Location × Net Sales, vs LY %, Sales per Cover, Food Cost %, Labor %, Prime Cost %, EBITDA Margin, Seat Utilization, Walk-away Rate

**3. KPI Scorecard** (Scope slicer). *Which of our 21 KPIs are off target, and over which period?*
- Matrix: rows KPI Scorecard[category] › [kpi_name], columns [period], value KPI Display (● on target ▲ watch ■ off target)

**4. OKR Tracker** (Scope slicer). *Are we delivering this season's objectives?*
- Cards: Objective Progress, KRs On Track / At Risk / Off Track
- Bar: OKR Board[objective] × Objective Progress
- Line: OKR History[eval_date] × KR Progress Trend, legend kr_id
- Table: kr_id, key_result, owner, KR Baseline, KR Target, KR Current, KR Progress (0-100%), status, note

**5. Pain Points** (Location slicer). *What should we fix first, and what is it worth?*
- Cards: Open Pain Points, Critical Pain Points, $ at Stake (annual)
- Bars: $ at stake by category, by location; column: count by severity
- Table: severity, location, title, evidence, recommended_action, $ at Stake, first_seen

**6. Revenue Flow** (Month, Location slicers). *Where does each sales dollar go?*
- Waterfall: P&L Waterfall[step] × Waterfall Amount (net sales → costs; the total bar is four-wall EBITDA)
- Stacked column: Month × P&L COGS, P&L Labor, P&L Operating Expenses, Four-Wall EBITDA
- Table: Revenue Flow[stage, source, target] × Flow Amount (add the Sankey here)
- Bar: Location × Dine-in, Takeout, Delivery Sales

**7. Floor & Capacity** (Month, Location slicers). *When is the room full, and whom do we turn away?*
- Cards: Seat Utilization, RevPASH, Table Turns per Day, Avg Dwell (min), Walk-away Rate, Large-Party Walk-away Rate
- Matrix heatmap: Date[weekday] × Hour[hour_label], value Hourly Seat Utilization
- Bar: Location × Lost Covers; line: Month × Seat Utilization; column: Hour × Covers per Staff Hour

**8. Labor & Costs** (Month, Location slicers). *Are we staffed to demand, and buying to recipe?*
- Cards: Labor %, Sales per Labor Hour, Food Cost %, Food Cost Variance (pts), Waste %, Prime Cost %
- Line: Month × Sales per Labor Hour, legend Location; bar: Location × Labor %, Food Cost %
- Line: Month × Food Cost %, Recipe Food Cost %; bar: Location × Waste %

**9. Menu Engineering** (Period, Category, Scope slicers). *Which dishes to promote, re-price or drop?*
- Scatter: details item_name, legend menu_class, X Menu Mix, Y Margin per Plate
- Table: item_name, menu_class, Units Sold, Menu Mix, Margin per Plate, Total Margin

**10. Forecast** (Location slicer). *What's coming, and how much should we trust it?*
- Cards: Forecast Sales, Forecast Covers, Forecast MAPE, Within Band %
- Line: Month × Net Sales, Forecast Sales, Forecast Low (P10), Forecast High (P90)
- Table: Forecast vs Actual[date, forecast, actual, abs_pct_error, within_band], Location

## 6. Manual route (always works)

1. *Get data → PostgreSQL database*: server `localhost:5432`, database `forkcast`, **DirectQuery**.
2. Tick the `analytics` tables and views listed in `build_pbip.py` → `TABLES`, and rename them to the friendly names shown there.
3. In Model view, create the 32 relationships in `build_pbip.py` → `RELATIONSHIPS`. Each goes many-to-one, single direction, from the first table to the second.
4. Mark `Date` as the date table, using the `date` column.
5. Create each measure from `measures.dax` on the table named in its section header.
6. Build the pages from the catalogue above.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Live cards blank or zero | Restaurants are closed (before 11 am or after 11 pm ET), or the streamer isn't running. Use `--speed 60` for a demo. |
| "Seconds Since Last Update" keeps rising | Restart `realtime/streamer.py`; it catches up automatically. |
| KPI / OKR / Pain Points look stale | The evaluator isn't running. Start `python realtime/evaluator.py`. |
| Forecast vs Actual empty | Run `python realtime/evaluator.py --backfill 28` |
| A column is missing after you change SQL | Run `python powerbi/build_pbip.py` again and reopen the project |
