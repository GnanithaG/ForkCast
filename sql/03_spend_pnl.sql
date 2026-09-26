-- ============================================================================
-- 03_spend_pnl.sql  ·  where the money goes
--   monthly store P&L (four-wall EBITDA), cost of goods by category,
--   theoretical vs actual food cost, waste, labor productivity, menu engineering
-- ============================================================================

CREATE OR REPLACE TABLE mart_monthly_sales AS
SELECT location_id, date_trunc('month', business_date)::DATE AS month,
       sum(net_sales) AS net_sales, sum(discounts) AS discounts, sum(checks) AS checks, sum(covers) AS covers,
       sum(dine_in_sales) AS dine_in_sales, sum(takeout_sales) AS takeout_sales, sum(delivery_sales) AS delivery_sales,
       sum(dine_in_covers) AS dine_in_covers, sum(dine_in_parties) AS dine_in_parties,
       sum(lunch_sales) AS lunch_sales, sum(dinner_sales) AS dinner_sales,
       count(*) AS open_days,
       sum(seat_utilization * open_hours) / sum(open_hours) AS seat_utilization,
       sum(dine_in_sales) / sum(seats * open_hours)          AS revpash,
       sum(dine_in_parties)::DOUBLE / sum(tables)            AS table_turns_per_day,
       sum(avg_dwell_min * dine_in_parties) / sum(dine_in_parties) AS avg_dwell_min,
       sum(lost_parties) AS lost_parties, sum(lost_covers) AS lost_covers,
       sum(parties_waited) AS parties_waited
FROM mart_daily_ops GROUP BY ALL;

-- Theoretical (recipe) cost of what was sold, by menu category
CREATE OR REPLACE TABLE mart_theoretical_cost AS
SELECT c.location_id, date_trunc('month', c.business_date)::DATE AS month, m.category,
       sum(i.quantity) AS units, sum(i.line_total) AS gross_item_sales, sum(i.theoretical_cost) AS theoretical_cost
FROM fact_check_items i
JOIN fact_checks c USING (check_id)
JOIN dim_menu_items m USING (item_id)
GROUP BY ALL;

CREATE OR REPLACE TABLE mart_monthly_purchases AS
SELECT location_id, date_trunc('month', invoice_date)::DATE AS month, purchase_category, vendor,
       sum(amount) AS amount, count(*) AS invoices
FROM fact_purchases GROUP BY ALL;

CREATE OR REPLACE TABLE mart_monthly_labor AS
SELECT location_id, date_trunc('month', business_date)::DATE AS month, role, daypart,
       sum(hours) AS hours, sum(wage_cost) AS wage_cost, sum(total_labor_cost) AS labor_cost,
       count(DISTINCT employee_id) AS employees, count(*) AS shifts
FROM fact_labor_shifts GROUP BY ALL;

CREATE OR REPLACE TABLE mart_monthly_waste AS
SELECT location_id, date_trunc('month', business_date)::DATE AS month, reason,
       sum(waste_cost) AS waste_cost
FROM fact_waste_log GROUP BY ALL;

-- Store P&L per location-month
CREATE OR REPLACE TABLE mart_monthly_pnl AS
WITH s AS (SELECT * FROM mart_monthly_sales),
p AS (
    SELECT location_id, month,
           sum(amount) AS total_cogs,
           sum(CASE WHEN purchase_category IN ('Protein','Produce','Dairy','Dry Goods') THEN amount END) AS food_cogs,
           sum(CASE WHEN purchase_category IN ('Alcohol','Beverage') THEN amount END)                    AS beverage_cogs,
           sum(CASE WHEN purchase_category = 'Packaging' THEN amount END)                                AS packaging_cogs
    FROM mart_monthly_purchases GROUP BY ALL
),
t AS (
    SELECT location_id, month,
           sum(CASE WHEN category IN ('Starter','Main','Side','Dessert') THEN theoretical_cost END) AS theoretical_food_cost,
           sum(CASE WHEN category IN ('Starter','Main','Side','Dessert') THEN gross_item_sales END) AS food_sales,
           sum(CASE WHEN category IN ('Beverage','Bar') THEN gross_item_sales END)                  AS beverage_sales
    FROM mart_theoretical_cost GROUP BY ALL
),
l AS (
    SELECT location_id, month, sum(labor_cost) AS labor_cost, sum(hours) AS labor_hours,
           sum(CASE WHEN role = 'Manager' THEN labor_cost END) AS mgmt_labor_cost,
           sum(CASE WHEN role <> 'Manager' THEN labor_cost END) AS hourly_labor_cost
    FROM mart_monthly_labor GROUP BY ALL
),
o AS (
    SELECT location_id, month::DATE AS month, sum(amount) AS opex,
           sum(CASE WHEN expense_category = 'Rent & CAM' THEN amount END)           AS occupancy,
           sum(CASE WHEN expense_category = 'Delivery Commissions' THEN amount END) AS delivery_commissions,
           sum(CASE WHEN expense_category = 'Utilities' THEN amount END)            AS utilities,
           sum(CASE WHEN expense_category = 'Marketing' THEN amount END)            AS marketing
    FROM fact_operating_expenses GROUP BY ALL
),
w AS (SELECT location_id, month, sum(waste_cost) AS waste_cost FROM mart_monthly_waste GROUP BY ALL)
SELECT s.location_id, s.month, s.net_sales, s.covers, s.checks,
       p.total_cogs, p.food_cogs, p.beverage_cogs, p.packaging_cogs,
       t.theoretical_food_cost, t.food_sales, t.beverage_sales,
       w.waste_cost,
       l.labor_cost, l.labor_hours, l.mgmt_labor_cost, l.hourly_labor_cost,
       o.opex, o.occupancy, o.delivery_commissions, o.utilities, o.marketing,
       p.total_cogs + l.labor_cost                                   AS prime_cost,
       s.net_sales - p.total_cogs - l.labor_cost - o.opex            AS four_wall_ebitda,
       -- ratios
       p.food_cogs / t.food_sales                                    AS food_cost_pct,
       t.theoretical_food_cost / t.food_sales                        AS theoretical_food_cost_pct,
       (p.food_cogs - t.theoretical_food_cost) / t.food_sales        AS food_cost_variance_pts,
       p.beverage_cogs / t.beverage_sales                            AS beverage_cost_pct,
       p.total_cogs / s.net_sales                                    AS cogs_pct,
       l.labor_cost / s.net_sales                                    AS labor_pct,
       (p.total_cogs + l.labor_cost) / s.net_sales                   AS prime_cost_pct,
       o.occupancy / s.net_sales                                     AS occupancy_pct,
       (s.net_sales - p.total_cogs - l.labor_cost - o.opex) / s.net_sales AS ebitda_margin,
       w.waste_cost / p.food_cogs                                    AS waste_pct_of_food,
       s.net_sales / l.labor_hours                                   AS sales_per_labor_hour,
       s.covers / l.labor_hours                                      AS covers_per_labor_hour,
       s.net_sales / s.covers                                        AS sales_per_cover,
       s.net_sales / s.checks                                        AS avg_check
FROM s
JOIN p USING (location_id, month)
JOIN t USING (location_id, month)
JOIN l USING (location_id, month)
JOIN o USING (location_id, month)
LEFT JOIN w USING (location_id, month);

-- Labor efficiency by daypart: are we staffed to demand?
CREATE OR REPLACE TABLE mart_daypart_labor AS
WITH l AS (
    SELECT location_id, daypart, sum(hours) AS hours, sum(labor_cost) AS labor_cost
    FROM mart_monthly_labor WHERE month >= DATE '{TTM_START}' AND role <> 'Manager' GROUP BY ALL
),
s AS (
    SELECT location_id, daypart, sum(net_sales) AS net_sales, sum(covers) AS covers
    FROM fact_checks WHERE business_date >= DATE '{TTM_START}' GROUP BY ALL
)
SELECT l.*, s.net_sales, s.covers,
       s.net_sales / l.hours AS splh, s.covers / l.hours AS covers_per_labor_hour,
       l.labor_cost / s.net_sales AS hourly_labor_pct
FROM l JOIN s USING (location_id, daypart);

-- Menu engineering (Kasavana-Smith), trailing twelve months, chain + per location
CREATE OR REPLACE TABLE mart_menu_engineering AS
WITH base AS (
    SELECT coalesce(c.location_id, 'ALL') AS location_id, i.item_id,
           sum(i.quantity) AS units, sum(i.line_total) AS sales, sum(i.theoretical_cost) AS food_cost
    FROM fact_check_items i JOIN fact_checks c USING (check_id)
    WHERE c.business_date >= DATE '{TTM_START}'
    GROUP BY GROUPING SETS ((c.location_id, i.item_id), (i.item_id))
),
m AS (
    SELECT b.*, mi.item_name, mi.category,
           (b.sales - b.food_cost) / b.units              AS cm_per_unit,
           b.sales - b.food_cost                           AS total_cm,
           b.units / sum(b.units) OVER w                   AS menu_mix_pct,
           count(*) OVER w                                 AS items_in_category,
           sum(b.sales - b.food_cost) OVER w / sum(b.units) OVER w AS avg_cm_category
    FROM base b JOIN dim_menu_items mi USING (item_id)
    WINDOW w AS (PARTITION BY b.location_id, mi.category)
)
SELECT *,
       0.7 / items_in_category AS popularity_threshold,
       CASE WHEN menu_mix_pct >= 0.7 / items_in_category AND cm_per_unit >= avg_cm_category THEN 'Star'
            WHEN menu_mix_pct >= 0.7 / items_in_category THEN 'Plowhorse'
            WHEN cm_per_unit >= avg_cm_category THEN 'Puzzle'
            ELSE 'Dog' END AS menu_class
FROM m;
