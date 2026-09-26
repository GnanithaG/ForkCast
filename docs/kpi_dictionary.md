# KPI Dictionary

All KPIs are computed in DuckDB (`sql/`) from the raw fact tables. "TTM" = trailing twelve months (Sep 2025 – Aug 2026). Target bands are common full-service rules of thumb used for the dashboard's status chips.

## Sales & guests

| KPI | Definition | Source |
|---|---|---|
| Net sales | Item sales − discounts/comps, excluding tax and tips | `fact_checks.net_sales` |
| Covers | Guests served (party size for dine-in, people fed for off-premise) | `fact_checks.covers` |
| Checks | Guest checks (one per party or off-premise order) | `fact_checks` |
| Average check | Net sales ÷ checks | `mart_monthly_pnl` |
| Sales per cover | Net sales ÷ covers | `mart_monthly_pnl` |
| Channel mix | Dine-in / takeout / delivery share of net sales | `mart_daily_ops` |
| Daypart mix | Lunch (before 4pm) vs dinner sales | `mart_daily_ops` |

## Floor & capacity

| KPI | Definition | Source |
|---|---|---|
| Seat utilization | Occupied seat-minutes (covers × minutes seated) ÷ available seat-minutes (seats × 60 per open hour) | `mart_floor_hourly` |
| Seats blocked | Seat-minutes held by seated parties including empty chairs at their table | `mart_floor_hourly` |
| RevPASH | Dine-in net sales ÷ available seat-hours | `mart_daily_ops` |
| Table turns | Dine-in parties ÷ tables, per day | `mart_daily_ops` |
| Dwell time | Minutes from seating to check close | `mart_daily_ops` |
| Wait time | Minutes from arrival to seating, for parties that waited | `fact_checks.wait_min` |
| Lost covers | Guests in parties that walked away when no table freed within their tolerance | `fact_lost_demand` |
| Seat fill | Guests ÷ chairs at occupied tables, by table size | `mart_table_fit` |
| Sales per seat by zone | Net sales ÷ seats for main room, bar, patio | `mart_zone_usage` |

## Cost of goods

| KPI | Definition | Target |
|---|---|---|
| Food cost % (actual) | Food purchases (protein, produce, dairy, dry goods) ÷ food sales | ≤ 33% |
| Food cost % (theoretical) | Recipe cost of food items sold ÷ food sales | |
| Food cost variance | Actual − theoretical, in points of food sales | → 0 |
| Beverage cost % | Beverage + alcohol purchases ÷ beverage sales | 20–25% |
| COGS % | All purchases incl. packaging ÷ net sales | |
| Waste % | Logged waste cost ÷ food purchases | ≤ 4.5% |

## Labor

| KPI | Definition | Target |
|---|---|---|
| Labor % | Wages + 12% payroll burden, all roles, ÷ net sales | ≤ 32% |
| SPLH | Net sales ÷ labor hours | higher is better |
| Covers per labor hour | Covers ÷ labor hours | |
| Hourly labor % by daypart | Non-manager labor cost ÷ daypart sales | |

## Profitability

| KPI | Definition | Target |
|---|---|---|
| Prime cost % | (COGS + total labor) ÷ net sales | ≤ 65% |
| Occupancy % | Rent & CAM ÷ net sales | ≤ 8% |
| Four-wall EBITDA | Net sales − COGS − labor − store operating expenses | margin ≥ 17% |
| Delivery commissions | 22% of third-party delivery sales | |

## Menu engineering (Kasavana–Smith)

Computed per menu category over TTM.

| Term | Definition |
|---|---|
| Menu mix % | Item units ÷ category units |
| Popularity threshold | 70% × (1 ÷ items in category) |
| Contribution margin (CM) | Average selling price − plate cost |
| Star | Mix ≥ threshold and CM ≥ category average |
| Plowhorse | Mix ≥ threshold, CM below average |
| Puzzle | Mix below threshold, CM ≥ average |
| Dog | Mix below threshold, CM below average |

## Projections

| Output | Method |
|---|---|
| Sales & covers forecast | Daily log-linear OLS per store: trend, day of week, Fourier seasonality (K=3), holidays, price step, new-store ramp; P10/P50/P90 from 7-day block bootstrap |
| Backtest | Train through May 2026, score Jun–Aug 2026, monthly MAPE vs seasonal-naive |
| Projected labor | Daily `hours = fixed + b × covers` per store, costed at the Sep-2026 wage |
| Scenarios | Conservative (P10, COGS +1 pt), Base (P50), Optimistic (P90 + half of opportunities) |
| New-store economics | Mature sales per seat × 150 seats, Brickell's observed ramp and margin curve, payback on capex + pre-opening |
