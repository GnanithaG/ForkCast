# ForkCast — Restaurant Chain Operations & Growth Analytics

An end-to-end analytics project for a 6-location restaurant chain (**Harbor & Hearth Kitchen**, fictional, South Florida). It answers the questions an operator actually asks:

- **Where is the money going?** Food, beverage, labor, rent and operating costs, down to vendor category and waste reason.
- **How well is the floor used?** Seat utilization, RevPASH, table turns, dwell time, and the guests who walk away because no table frees up.
- **Which KPIs are off, and where?** Prime cost, food cost vs recipe cost, labor productivity and menu margins, scored against industry rules of thumb.
- **What happens next?** A 12-month sales forecast with prediction intervals, a projected P&L, growth scenarios, a sized list of operating opportunities, and the business case for a seventh store.

The project runs two ways:

| | Real-time (Power BI) | Batch (portable) |
|---|---|---|
| Data | Live POS event stream into **PostgreSQL** | 24 months of history in parquet |
| SQL | `realtime/sql/`: facts, P&L, revenue flow, menu, live views | `sql/` on DuckDB |
| Python | `realtime/evaluator.py`: KPIs, OKRs, pain points, pacing, forecast, every 2 min | `src/forecast.py`, `src/growth.py` |
| Dashboard | **Power BI** project, DirectQuery, 10 pages, 129 DAX measures | Self-contained HTML (`dashboard/index.html`) |

## Real-time quick start

```bash
docker compose up -d                 # Postgres + setup + live streamer + evaluator
#   or with a local PostgreSQL:  pip install -r requirements.txt && python realtime/setup.py --start
```

Then open `powerbi/ForkCast.pbip` in Power BI Desktop. [`powerbi/POWERBI_GUIDE.md`](powerbi/POWERBI_GUIDE.md) covers connecting, live page refresh and finishing touches. [`docs/business_guide.md`](docs/business_guide.md) explains the KPIs, OKRs, pain points and revenue flow.

```
realtime/streamer.py ──► PostgreSQL pos.* ──► analytics.* (SQL) ──► realtime/evaluator.py (Python) ──► analytics.* ──► Power BI (DirectQuery)
 guests arrive, get seated,   orders, tables,     daily & hourly facts,    KPI snapshot, OKR progress,       live service, KPIs, OKRs,
 order, pay; staff clock in;  shifts, invoices,   P&L, revenue flow,       pain points with $ impact,        pain points, revenue flow,
 vendors invoice; waste       waste, heartbeat    menu, live views         intraday pace, daily forecast     floor, labor, menu, forecast
```

The streamer uses the same demand, seating and cost model as the history. On start it catches up from the last event to now (restaurant time, America/New_York), then writes every 5 seconds. `--speed 60` runs a fast clock for demos outside the 11 am – 11 pm trading hours.

## Batch pipeline

```
raw data (11 tables, ~6M rows)  ──►  DuckDB warehouse + SQL KPI marts  ──►  forecast + growth models  ──►  interactive dashboard
     src/generate_data.py             sql/*.sql  (src/build_warehouse.py)     src/forecast.py, growth.py      dashboard/index.html
```

## Headline findings (trailing 12 months, Sep 2025 – Aug 2026)

| | |
|---|---|
| Net sales | **$40.7M**, +17% YoY (incl. new Brickell store), $49.64 per cover |
| Prime cost | **65.3%** of sales, up 1.5 pts: protein inflation + the Florida minimum-wage step |
| Four-wall EBITDA | **$7.0M**, 17.1% margin |
| Food cost gap | Actual food cost 34.6% vs recipe cost 30.4%, a **4.2-pt gap** from inflation, waste and over-portioning |
| Floor | Seat utilization only 23%, yet **29% of parties of 5–6 walk away**: the constraint is table mix at peak, not seats |
| Labor | Mizner Park (Boca Raton) runs 37.7% labor vs 30–34% at the other stores, scheduling ~25% more hours per cover |
| Waste | Atlantic Ave (Delray) wastes 8.4% of food spend vs a 3.9% peer median |
| Opportunity | **$1.62M/yr** in profit across six levers (table below) |
| Next 12 months | **$43.5M** projected sales (80% range $42.0–45.1M), $7.3M EBITDA after the Sep-2026 wage step |
| Store seven | $2.58M investment, $7.3M mature sales, 54% cash-on-cash, **30-month payback** |

| Opportunity lever | Annual profit impact | Evidence |
|---|---:|---|
| Staff to demand | $666k | Hourly SPLH below peer median at Mizner Park ($432k), Brickell and Atlantic Ave |
| Recover walk-away demand | $320k | 40% of lost large parties recaptured via waitlist + combinable tables |
| Re-price plowhorse mains (+$1) | $226k | Popular, below-average-margin mains; assumes 3% unit loss |
| Portion & recipe control | $207k | Close a quarter of the actual-vs-recipe cost gap |
| Shift delivery to own channel | $115k | 20% of third-party orders moved from 22% to 5% fees |
| Cut food waste | $89k | Bring high-waste stores to peer median |

## What's in the data

`src/generate_data.py` builds a realistic 24-month dataset (Sep 2024 – Aug 2026). The drivers are modeled on what a real South Florida operator sees, so the analysis has real signal to find:

- **Seasonality:** winter "snowbird" peak (Feb–Mar ~1.3×), late-summer trough (Sep ~0.77×); Las Olas swings harder, Brickell (Miami) less.
- **Floor simulation:** every party arrives, gets seated at the smallest free table that fits, waits if one frees soon, or walks away. Seat utilization, RevPASH, turns and lost demand are therefore measured, not invented. Patios close on some summer afternoons.
- **Costs:** 30-item menu with plate costs; vendor invoices by category; a 9% protein price jump in Jun 2025; a 4% menu price increase on 15 Jan 2026; the Florida minimum wage stepping $13 → $14 → $15 each 30 Sep (tipped wage = minimum − $3.02); a walk-in cooler failure at Delray in Jul 2025.
- **Labor:** employee-level shifts by role and daypart, scheduled from expected covers, with one store deliberately overstaffed.
- **A new store:** Brickell opened Mar 2025 with an opening-buzz spike and a ramp curve.

| Table | Rows | Grain |
|---|---:|---|
| `fact_checks` | 763k | guest check: channel, table, covers, open/close time, wait, sales, tip |
| `fact_check_items` | 4.9M | menu item on a check, price, recipe cost |
| `fact_lost_demand` | 31k | party that walked away, with quoted wait |
| `fact_labor_shifts` | 331k | employee shift: role, daypart, hours, wage, burden |
| `fact_purchases` | 6k | vendor invoice by category |
| `fact_waste_log` | 4k | daily waste cost and reason |
| `fact_operating_expenses` | 1.2k | monthly opex by category |
| `dim_locations`, `dim_tables`, `dim_menu_items`, `dim_employees` | | floor plans (276 tables), menu, staff |

## KPI layer (SQL, DuckDB)

`sql/02_floor_ops.sql` and `sql/03_spend_pnl.sql` build the marts. Definitions are in [`docs/kpi_dictionary.md`](docs/kpi_dictionary.md). Highlights:

- **Seat utilization** — occupied seat-minutes ÷ available seat-minutes, computed hour by hour with interval overlap joins (a party seated 7:40–9:05 counts 20, 60 and 5 minutes in three hours).
- **RevPASH** — dine-in revenue per available seat-hour, the core capacity-yield metric.
- **Prime cost** — COGS + total labor, the number most operators manage to.
- **Actual vs theoretical food cost** — invoices vs recipe cost of what was sold; the gap is inflation + waste + portioning.
- **Four-wall EBITDA** — store-level profit before corporate overhead.
- **Menu engineering (Kasavana–Smith)** — each item classed Star / Plowhorse / Puzzle / Dog by menu mix vs contribution margin within its category.

## Projections

`src/forecast.py` — log-linear regression per store on daily data: trend, day of week, annual Fourier seasonality (K=3), holiday effects, the price-increase step, and a new-store ramp term. Brickell has only one season of history, so it **borrows seasonality, trend and price effect from the five mature stores** (a light hierarchical approach). Prediction bands come from a 7-day block bootstrap of residuals (1,000 paths).

**Backtest** (train through May 2026, score Jun–Aug 2026, monthly MAPE):

| | Clematis | Mizner | Las Olas | Atlantic | Harbourside | Brickell | **Chain** |
|---|---:|---:|---:|---:|---:|---:|---:|
| Model | 3.7% | 1.6% | 1.8% | 3.5% | 0.7% | 9.2% | **3.4%** |
| Seasonal naive | 3.6% | 3.6% | 2.1% | 3.0% | 2.2% | 10.4% | 4.2% |

The model beats the naive baseline at chain level and on four of six stores; at the two most stable stores last year is already as good a guide.

`src/growth.py` — projected P&L (COGS from trailing ratios; hourly labor from a fitted `hours = fixed + b × covers` model, R² ≈ 0.97, costed at the new $15 minimum wage; fixed and variable opex), three scenarios, the opportunity sizing above, and new-store unit economics built from mature-store sales per seat and Brickell's actual ramp.

Stated assumptions: store seven capex $2.3M + $275k pre-opening; revenue levers flow through at 34% (after food and variable labor).

## Run it

```bash
pip install -r requirements.txt
python run_all.py          # ~30 s: data -> warehouse -> forecast -> growth -> dashboard
open dashboard/index.html  # self-contained, data baked in
```

The warehouse lands in `data/forkcast.duckdb`; query it directly, or connect Power BI / Tableau to the parquet files in `data/raw` and the CSVs in `data/marts`.

## Project layout

```
forkcast/
├── run_all.py
├── src/
│   ├── config.py            chain constants, wage schedule, dates
│   ├── generate_data.py     synthetic data + floor simulation
│   ├── build_warehouse.py   runs sql/*.sql into DuckDB
│   ├── forecast.py          sales & covers forecast, backtest
│   ├── growth.py            projected P&L, opportunities, scenarios, new-unit model
│   └── export_dashboard.py  bakes all metrics into the dashboard
├── sql/                     staging, floor & ops marts, spend & P&L marts
├── dashboard/               template.html -> index.html
├── realtime/
│   ├── setup.py             one-command setup (+ --start to run live)
│   ├── load_history.py      schema + bulk COPY of the 24-month history
│   ├── streamer.py          live POS event stream (catch-up, live, demo speed)
│   ├── evaluator.py         KPIs, OKRs, pain points, pacing, forecast -> Postgres
│   ├── definitions.py       KPI catalogue with targets; OKR objectives and key results
│   └── sql/                 00 pos schema · 10 analytics layer · 20 evaluation tables & BI views
├── powerbi/
│   ├── ForkCast.pbip        open in Power BI Desktop (DirectQuery to Postgres)
│   ├── ForkCast-BlankPages.pbip, ForkCast.SemanticModel/, ForkCast.Report/
│   ├── measures.dax, ForkCast-theme.json, build_pbip.py
│   └── POWERBI_GUIDE.md
├── docs/                    kpi_dictionary.md, business_guide.md
├── docker-compose.yml, Dockerfile
└── data/                    raw parquet, marts, warehouse (generated)
```

## Resume bullets

- Built an end-to-end restaurant analytics platform for a 6-unit chain (~6M transactions): Python data pipeline, DuckDB SQL KPI layer, forecasting models and an interactive dashboard across floor, cost, labor and menu performance.
- Designed 25+ operational and financial KPIs (RevPASH, seat utilization, prime cost, actual-vs-theoretical food cost, SPLH, four-wall EBITDA, menu engineering) using interval-overlap SQL to measure hourly floor occupancy.
- Forecast 12-month store sales with a seasonal regression and bootstrap prediction intervals, reaching 3.4% chain-level MAPE on a holdout backtest (vs 4.2% seasonal-naive), with borrowed seasonality for a store with limited history.
- Built a real-time pipeline: a POS event stream into PostgreSQL, a SQL analytics layer, and a Python evaluator that recomputes 21 KPIs, 9 OKR key results and 14 pain-point detectors (each with estimated annual $ impact) every 2 minutes, served to a 10-page Power BI DirectQuery report with 129 DAX measures.
- Sized $1.6M/yr in profit opportunities (labor scheduling, lost large-party demand, menu pricing, waste, delivery fees) and built new-store unit economics showing a 30-month payback.

## Why this maps to Skoruz

Skoruz's analytics practice covers customer, finance, operations and supply-chain analytics with data science, data engineering and visualization. This project touches each: operations (floor and labor), finance (P&L, projections, unit economics), supply chain (vendor spend, waste), customer (channel mix, lost demand), built as data engineering → modeling → decision dashboard, the same consult-build-manage flow they describe.
