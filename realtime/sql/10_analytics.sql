-- ============================================================================
-- 10_analytics.sql · the analytics layer Power BI reads (schema: analytics)
--
--   dimensions   dim_date, dim_month, dim_location, dim_menu_item, dim_hour
--   facts        hourly_floor, daily_location (tables, refreshed incrementally
--                by analytics.refresh(from_date)), item_daily
--   views        fact_orders, monthly_pnl, revenue_flow, pnl_waterfall,
--                menu_engineering, live_* (read straight from the POS tables)
--
-- "Business date" rolls over at 4 am, so late-night views still show the service
-- that just ended.
-- ============================================================================
CREATE SCHEMA IF NOT EXISTS analytics;

-- ---------------------------------------------------------------- clock
CREATE OR REPLACE VIEW analytics.v_clock AS
SELECT sim_clock                                        AS now_local,
       (sim_clock - interval '4 hours')::date           AS business_date,
       last_tick_at,
       extract(epoch FROM now() - last_tick_at)::int    AS seconds_since_last_write,
       mode, speed
FROM pos.stream_status WHERE id = 1;

-- ---------------------------------------------------------------- dimensions
DROP TABLE IF EXISTS analytics.dim_date CASCADE;
CREATE TABLE analytics.dim_date AS
SELECT d::date                                                  AS date,
       date_trunc('month', d)::date                             AS month_start,
       to_char(d, 'Mon YYYY')                                   AS month_label,
       extract(year FROM d)::int                                AS year,
       'Q' || extract(quarter FROM d) || ' ' || extract(year FROM d) AS quarter_label,
       extract(month FROM d)::int                               AS month_num,
       extract(isodow FROM d)::int                              AS weekday_num,       -- 1 = Mon
       to_char(d, 'Dy')                                         AS weekday,
       (date_trunc('week', d))::date                            AS week_start,
       extract(isodow FROM d) IN (5, 6)                         AS is_fri_sat,
       CASE WHEN extract(month FROM d) IN (1,2,3,4) THEN 'Peak season'
            WHEN extract(month FROM d) IN (6,7,8,9) THEN 'Off season'
            ELSE 'Shoulder' END                                 AS fl_season,
       d::date - 364                                            AS same_weekday_last_year
FROM generate_series(DATE '2024-09-01', DATE '2027-12-31', interval '1 day') d;
ALTER TABLE analytics.dim_date ADD PRIMARY KEY (date);

DROP TABLE IF EXISTS analytics.dim_month CASCADE;
CREATE TABLE analytics.dim_month AS
SELECT DISTINCT month_start, month_label, year, month_num, fl_season FROM analytics.dim_date;
ALTER TABLE analytics.dim_month ADD PRIMARY KEY (month_start);

DROP TABLE IF EXISTS analytics.dim_hour CASCADE;
CREATE TABLE analytics.dim_hour AS
SELECT h AS hr, CASE WHEN h < 12 THEN h || ' am' WHEN h = 12 THEN '12 pm' ELSE (h - 12) || ' pm' END AS hour_label,
       CASE WHEN h < 16 THEN 'Lunch' ELSE 'Dinner' END AS daypart
FROM generate_series(11, 22) h;
ALTER TABLE analytics.dim_hour ADD PRIMARY KEY (hr);

CREATE OR REPLACE VIEW analytics.dim_location AS
SELECT location_id, name AS location_name, city, county, open_date, seats, tables, sqft, monthly_rent,
       name || ' (' || city || ')' AS location_label,
       open_date < DATE '2024-09-01' AS is_mature
FROM pos.dim_locations;

CREATE OR REPLACE VIEW analytics.dim_menu_item AS
SELECT item_id, item_name, category,
       CASE WHEN category IN ('Beverage', 'Bar') THEN 'Beverage' ELSE 'Food' END AS item_group,
       base_price, plate_cost
FROM pos.dim_menu_items;

-- ---------------------------------------------------------------- hourly floor & staffing
DROP TABLE IF EXISTS analytics.hourly_floor CASCADE;
CREATE TABLE analytics.hourly_floor (
    location_id text, business_date date, hr int,
    available_seat_min numeric, occupied_seat_min numeric, blocked_seat_min numeric,
    checks int, covers int, dine_parties int, net_sales numeric, dine_sales numeric,
    lost_parties int, lost_covers int, staff_hours numeric, labor_cost numeric,
    PRIMARY KEY (location_id, business_date, hr)
);

-- ---------------------------------------------------------------- daily location facts
DROP TABLE IF EXISTS analytics.daily_location CASCADE;
CREATE TABLE analytics.daily_location (
    location_id text, business_date date,
    net_sales numeric, gross_sales numeric, discounts numeric, tips numeric, checks int, covers int,
    dine_in_sales numeric, takeout_sales numeric, delivery_sales numeric,
    dine_in_checks int, takeout_checks int, delivery_checks int, dine_in_covers int, dine_large_parties int,
    lunch_sales numeric, dinner_sales numeric,
    dwell_min_total numeric, parties_waited int, wait_min_total numeric,
    lost_parties int, lost_covers int, lost_large_parties int,
    available_seat_min numeric, occupied_seat_min numeric, open_hours int, seats int, tables int,
    labor_hours numeric, labor_cost numeric, hourly_labor_hours numeric, hourly_labor_cost numeric,
    kitchen_labor_cost numeric, foh_labor_cost numeric, mgmt_labor_cost numeric,
    food_sales numeric, beverage_sales numeric, theoretical_food_cost numeric, theoretical_bev_cost numeric,
    purchases_food numeric, purchases_bev numeric, purchases_packaging numeric,
    waste_cost numeric, delivery_commission_est numeric,
    PRIMARY KEY (location_id, business_date)
);

DROP TABLE IF EXISTS analytics.item_daily CASCADE;
CREATE TABLE analytics.item_daily (
    location_id text, business_date date, item_id text, channel text, daypart text,
    units int, sales numeric, theoretical_cost numeric,
    PRIMARY KEY (location_id, business_date, item_id, channel, daypart)
);

CREATE OR REPLACE FUNCTION analytics.refresh(p_from date) RETURNS void LANGUAGE plpgsql AS $$
DECLARE v_clock timestamp := (SELECT sim_clock FROM pos.stream_status WHERE id = 1);
BEGIN
  -- hourly floor ------------------------------------------------------------
  DELETE FROM analytics.hourly_floor WHERE business_date >= p_from;
  INSERT INTO analytics.hourly_floor
  WITH days AS (
      SELECT DISTINCT location_id, business_date FROM pos.orders WHERE business_date >= p_from
  ),
  grid AS (
      SELECT d.location_id, d.business_date, h.hr,
             d.business_date + make_interval(hours => h.hr)     AS h0,
             d.business_date + make_interval(hours => h.hr + 1) AS h1
      FROM days d CROSS JOIN analytics.dim_hour h
      WHERE h.hr < 22 OR extract(isodow FROM d.business_date) IN (5, 6)
  ),
  dine AS (
      SELECT o.location_id, o.business_date, o.open_ts, coalesce(o.close_ts, v_clock) AS close_ts, o.covers, t.seats
      FROM pos.orders o JOIN pos.dim_tables t USING (table_id)
      WHERE o.channel = 'Dine-in' AND o.business_date >= p_from AND o.status IN ('open', 'closed')
  ),
  occ AS (
      SELECT g.location_id, g.business_date, g.hr,
             sum(extract(epoch FROM least(d.close_ts, g.h1) - greatest(d.open_ts, g.h0)) / 60 * d.covers) AS occ,
             sum(extract(epoch FROM least(d.close_ts, g.h1) - greatest(d.open_ts, g.h0)) / 60 * d.seats)  AS blk
      FROM grid g JOIN dine d
        ON d.location_id = g.location_id AND d.business_date = g.business_date
       AND d.open_ts < g.h1 AND d.close_ts > g.h0
      GROUP BY 1, 2, 3
  ),
  sales AS (
      SELECT location_id, business_date, extract(hour FROM open_ts)::int AS hr,
             count(*) AS checks, sum(covers) AS covers, count(*) FILTER (WHERE channel = 'Dine-in') AS dine_parties,
             sum(net_sales) AS net_sales, sum(net_sales) FILTER (WHERE channel = 'Dine-in') AS dine_sales
      FROM pos.orders WHERE business_date >= p_from AND status IN ('open', 'closed')   -- open checks count as guests
      GROUP BY 1, 2, 3
  ),
  lost AS (
      SELECT location_id, business_date, extract(hour FROM arrival_ts)::int AS hr,
             count(*) AS lost_parties, sum(party_size) AS lost_covers
      FROM pos.lost_parties WHERE business_date >= p_from GROUP BY 1, 2, 3
  ),
  lab AS (
      SELECT g.location_id, g.business_date, g.hr,
             sum(extract(epoch FROM least(coalesce(s.clock_out_ts, v_clock), g.h1) - greatest(s.clock_in_ts, g.h0)) / 3600) AS staff_hours,
             sum(extract(epoch FROM least(coalesce(s.clock_out_ts, v_clock), g.h1) - greatest(s.clock_in_ts, g.h0)) / 3600
                 * s.hourly_rate * 1.12) AS labor_cost
      FROM grid g JOIN pos.labor_shifts s
        ON s.location_id = g.location_id AND s.business_date = g.business_date AND s.status <> 'scheduled'
       AND s.clock_in_ts < g.h1 AND coalesce(s.clock_out_ts, v_clock) > g.h0
      GROUP BY 1, 2, 3
  )
  SELECT g.location_id, g.business_date, g.hr,
         l.seats * 60.0, coalesce(o.occ, 0), coalesce(o.blk, 0),
         coalesce(s.checks, 0), coalesce(s.covers, 0), coalesce(s.dine_parties, 0),
         coalesce(s.net_sales, 0), coalesce(s.dine_sales, 0),
         coalesce(x.lost_parties, 0), coalesce(x.lost_covers, 0),
         coalesce(b.staff_hours, 0), coalesce(b.labor_cost, 0)
  FROM grid g
  JOIN pos.dim_locations l USING (location_id)
  LEFT JOIN occ o USING (location_id, business_date, hr)
  LEFT JOIN sales s USING (location_id, business_date, hr)
  LEFT JOIN lost x USING (location_id, business_date, hr)
  LEFT JOIN lab b USING (location_id, business_date, hr);

  -- item daily --------------------------------------------------------------
  DELETE FROM analytics.item_daily WHERE business_date >= p_from;
  INSERT INTO analytics.item_daily
  SELECT o.location_id, o.business_date, i.item_id, o.channel, o.daypart,
         sum(i.quantity), sum(i.line_total), sum(i.theoretical_cost)
  FROM pos.order_items i JOIN pos.orders o USING (check_id)
  WHERE o.business_date >= p_from AND o.status = 'closed'
  GROUP BY 1, 2, 3, 4, 5;

  -- daily location ----------------------------------------------------------
  DELETE FROM analytics.daily_location WHERE business_date >= p_from;
  INSERT INTO analytics.daily_location
  WITH o AS (
      SELECT location_id, business_date,
             sum(net_sales) AS net_sales, sum(subtotal) AS gross_sales, sum(discount) AS discounts, sum(tip) AS tips,
             count(*) AS checks, sum(covers) AS covers,
             sum(net_sales) FILTER (WHERE channel = 'Dine-in')  AS dine_in_sales,
             sum(net_sales) FILTER (WHERE channel = 'Takeout')  AS takeout_sales,
             sum(net_sales) FILTER (WHERE channel = 'Delivery') AS delivery_sales,
             count(*) FILTER (WHERE channel = 'Dine-in')  AS dine_in_checks,
             count(*) FILTER (WHERE channel = 'Takeout')  AS takeout_checks,
             count(*) FILTER (WHERE channel = 'Delivery') AS delivery_checks,
             sum(covers) FILTER (WHERE channel = 'Dine-in') AS dine_in_covers,
             count(*) FILTER (WHERE channel = 'Dine-in' AND covers >= 5) AS dine_large_parties,
             sum(net_sales) FILTER (WHERE daypart = 'lunch')  AS lunch_sales,
             sum(net_sales) FILTER (WHERE daypart = 'dinner') AS dinner_sales,
             sum(extract(epoch FROM close_ts - open_ts) / 60) FILTER (WHERE channel = 'Dine-in') AS dwell_min_total,
             count(*) FILTER (WHERE wait_min > 0) AS parties_waited,
             sum(wait_min) FILTER (WHERE wait_min > 0) AS wait_min_total
      FROM pos.orders WHERE business_date >= p_from AND status = 'closed' GROUP BY 1, 2
  ),
  f AS (
      SELECT location_id, business_date, sum(available_seat_min) AS avail, sum(occupied_seat_min) AS occ, count(*) AS open_hours
      FROM analytics.hourly_floor WHERE business_date >= p_from GROUP BY 1, 2
  ),
  lost AS (
      SELECT location_id, business_date, count(*) AS lost_parties, sum(party_size) AS lost_covers,
             count(*) FILTER (WHERE party_size >= 5) AS lost_large
      FROM pos.lost_parties WHERE business_date >= p_from GROUP BY 1, 2
  ),
  lab AS (
      SELECT location_id, business_date,
             sum(coalesce(hours, extract(epoch FROM v_clock - clock_in_ts) / 3600)) AS hrs,
             sum(coalesce(total_labor_cost, extract(epoch FROM v_clock - clock_in_ts) / 3600 * hourly_rate * 1.12)) AS cost,
             sum(coalesce(hours, extract(epoch FROM v_clock - clock_in_ts) / 3600)) FILTER (WHERE role <> 'Manager') AS h_hrs,
             sum(coalesce(total_labor_cost, extract(epoch FROM v_clock - clock_in_ts) / 3600 * hourly_rate * 1.12)) FILTER (WHERE role <> 'Manager') AS h_cost,
             sum(coalesce(total_labor_cost, extract(epoch FROM v_clock - clock_in_ts) / 3600 * hourly_rate * 1.12)) FILTER (WHERE role IN ('Line Cook','Prep Cook','Dishwasher')) AS k_cost,
             sum(coalesce(total_labor_cost, extract(epoch FROM v_clock - clock_in_ts) / 3600 * hourly_rate * 1.12)) FILTER (WHERE role IN ('Server','Bartender','Host')) AS foh_cost,
             sum(coalesce(total_labor_cost, extract(epoch FROM v_clock - clock_in_ts) / 3600 * hourly_rate * 1.12)) FILTER (WHERE role = 'Manager') AS m_cost
      FROM pos.labor_shifts WHERE business_date >= p_from AND status <> 'scheduled' GROUP BY 1, 2
  ),
  it AS (
      SELECT d.location_id, d.business_date,
             sum(d.sales) FILTER (WHERE m.category NOT IN ('Beverage','Bar')) AS food_sales,
             sum(d.sales) FILTER (WHERE m.category IN ('Beverage','Bar'))     AS bev_sales,
             sum(d.theoretical_cost) FILTER (WHERE m.category NOT IN ('Beverage','Bar')) AS theo_food,
             sum(d.theoretical_cost) FILTER (WHERE m.category IN ('Beverage','Bar'))     AS theo_bev
      FROM analytics.item_daily d JOIN pos.dim_menu_items m USING (item_id)
      WHERE d.business_date >= p_from GROUP BY 1, 2
  ),
  -- purchases are accrued evenly over the days each delivery covers (Mon: 3 days, Thu: 4, weekly: 7),
  -- so short periods are not distorted by which weekday an invoice happened to land on
  pu AS (
      SELECT p.location_id, (p.invoice_date + g.k)::date AS business_date,
             sum(p.amount / p.cover_days) FILTER (WHERE p.purchase_category IN ('Protein','Produce','Dairy','Dry Goods')) AS food,
             sum(p.amount / p.cover_days) FILTER (WHERE p.purchase_category IN ('Beverage','Alcohol')) AS bev,
             sum(p.amount / p.cover_days) FILTER (WHERE p.purchase_category = 'Packaging') AS pack
      FROM (SELECT *, CASE extract(isodow FROM invoice_date) WHEN 1 THEN 3 WHEN 4 THEN 4 ELSE 7 END AS cover_days
            FROM pos.purchases WHERE invoice_date >= p_from - 7) p
      CROSS JOIN LATERAL generate_series(0, p.cover_days - 1) g(k)
      WHERE p.invoice_date + g.k >= p_from
      GROUP BY 1, 2
  ),
  w AS (SELECT location_id, business_date, sum(waste_cost) AS waste FROM pos.waste_log WHERE business_date >= p_from GROUP BY 1, 2)
  SELECT o.location_id, o.business_date,
         o.net_sales, o.gross_sales, o.discounts, o.tips, o.checks, o.covers,
         coalesce(o.dine_in_sales, 0), coalesce(o.takeout_sales, 0), coalesce(o.delivery_sales, 0),
         o.dine_in_checks, o.takeout_checks, o.delivery_checks, coalesce(o.dine_in_covers, 0), o.dine_large_parties,
         coalesce(o.lunch_sales, 0), coalesce(o.dinner_sales, 0),
         coalesce(o.dwell_min_total, 0), o.parties_waited, coalesce(o.wait_min_total, 0),
         coalesce(lost.lost_parties, 0), coalesce(lost.lost_covers, 0), coalesce(lost.lost_large, 0),
         coalesce(f.avail, 0), coalesce(f.occ, 0), coalesce(f.open_hours, 0), l.seats, l.tables,
         coalesce(lab.hrs, 0), coalesce(lab.cost, 0), coalesce(lab.h_hrs, 0), coalesce(lab.h_cost, 0),
         coalesce(lab.k_cost, 0), coalesce(lab.foh_cost, 0), coalesce(lab.m_cost, 0),
         coalesce(it.food_sales, 0), coalesce(it.bev_sales, 0), coalesce(it.theo_food, 0), coalesce(it.theo_bev, 0),
         coalesce(pu.food, 0), coalesce(pu.bev, 0), coalesce(pu.pack, 0),
         coalesce(w.waste, 0), coalesce(o.delivery_sales, 0) * 0.22
  FROM o
  JOIN pos.dim_locations l USING (location_id)
  LEFT JOIN f USING (location_id, business_date)
  LEFT JOIN lost USING (location_id, business_date)
  LEFT JOIN lab USING (location_id, business_date)
  LEFT JOIN it USING (location_id, business_date)
  LEFT JOIN pu USING (location_id, business_date)
  LEFT JOIN w USING (location_id, business_date);
END $$;

-- ---------------------------------------------------------------- row-level orders (for drill-through)
CREATE OR REPLACE VIEW analytics.fact_orders AS
SELECT check_id, location_id, business_date, channel, delivery_platform, table_id, zone, covers, daypart,
       extract(hour FROM open_ts)::int AS hr, open_ts, close_ts,
       round(extract(epoch FROM close_ts - open_ts) / 60)::int AS dwell_min, wait_min,
       subtotal, discount, net_sales, tip, payment_type
FROM pos.orders WHERE status = 'closed';

-- ---------------------------------------------------------------- monthly P&L
-- Opex is posted when a month closes. For the open month it is estimated from the
-- location's opex-to-sales ratio over the previous three closed months (opex_is_estimate).
CREATE OR REPLACE VIEW analytics.monthly_pnl AS
WITH d AS (
    SELECT location_id, date_trunc('month', business_date)::date AS month_start,
           sum(net_sales) AS net_sales, sum(covers) AS covers, sum(checks) AS checks,
           sum(dine_in_sales) AS dine_in_sales, sum(takeout_sales) AS takeout_sales, sum(delivery_sales) AS delivery_sales,
           sum(food_sales) AS food_sales, sum(beverage_sales) AS beverage_sales,
           sum(theoretical_food_cost) AS theoretical_food_cost, sum(theoretical_bev_cost) AS theoretical_bev_cost,
           sum(labor_cost) AS labor_cost, sum(labor_hours) AS labor_hours, sum(kitchen_labor_cost) AS kitchen_labor_cost,
           sum(foh_labor_cost) AS foh_labor_cost, sum(mgmt_labor_cost) AS mgmt_labor_cost, sum(waste_cost) AS waste_cost,
           sum(lost_covers) AS lost_covers, count(*) AS open_days
    FROM analytics.daily_location GROUP BY 1, 2
),
pu AS (   -- COGS straight from invoices (some invoices land on days the store is closed)
    SELECT location_id, date_trunc('month', invoice_date)::date AS month_start,
           sum(amount) FILTER (WHERE purchase_category IN ('Protein','Produce','Dairy','Dry Goods')) AS food_cogs,
           sum(amount) FILTER (WHERE purchase_category IN ('Beverage','Alcohol')) AS beverage_cogs,
           coalesce(sum(amount) FILTER (WHERE purchase_category = 'Packaging'), 0) AS packaging_cogs
    FROM pos.purchases GROUP BY 1, 2
),
ox AS (
    SELECT location_id, month AS month_start,
           sum(amount) AS opex,
           sum(amount) FILTER (WHERE expense_category = 'Rent & CAM') AS rent,
           sum(amount) FILTER (WHERE expense_category = 'Delivery Commissions') AS delivery_commissions,
           sum(amount) FILTER (WHERE expense_category = 'Card Processing Fees') AS card_fees,
           sum(amount) FILTER (WHERE expense_category = 'Utilities') AS utilities,
           sum(amount) FILTER (WHERE expense_category = 'Marketing') AS marketing,
           sum(amount) FILTER (WHERE expense_category NOT IN ('Rent & CAM','Delivery Commissions','Card Processing Fees','Utilities','Marketing')) AS other_opex
    FROM pos.operating_expenses GROUP BY 1, 2
),
j AS (
    SELECT d.*, pu.food_cogs, pu.beverage_cogs, pu.packaging_cogs, ox.opex, ox.rent, ox.delivery_commissions, ox.card_fees, ox.utilities, ox.marketing, ox.other_opex,
           avg(ox.opex / nullif(d.net_sales, 0)) OVER (PARTITION BY d.location_id ORDER BY d.month_start ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING) AS trailing_opex_ratio,
           lag(ox.rent) OVER (PARTITION BY d.location_id ORDER BY d.month_start) AS last_rent
    FROM d LEFT JOIN pu USING (location_id, month_start) LEFT JOIN ox USING (location_id, month_start)
)
SELECT location_id, month_start, net_sales, covers, checks, dine_in_sales, takeout_sales, delivery_sales,
       food_sales, beverage_sales, theoretical_food_cost, theoretical_bev_cost,
       food_cogs, beverage_cogs, packaging_cogs, food_cogs + beverage_cogs + packaging_cogs AS total_cogs,
       labor_cost, labor_hours, kitchen_labor_cost, foh_labor_cost, mgmt_labor_cost, waste_cost, lost_covers, open_days,
       coalesce(opex, trailing_opex_ratio * net_sales)                         AS opex,
       coalesce(rent, last_rent)                                               AS rent,
       coalesce(delivery_commissions, delivery_sales * 0.22)                   AS delivery_commissions,
       coalesce(card_fees, net_sales * 0.027)                                  AS card_fees,
       coalesce(opex, trailing_opex_ratio * net_sales) - coalesce(rent, last_rent)
         - coalesce(delivery_commissions, delivery_sales * 0.22) - coalesce(card_fees, net_sales * 0.027) AS other_opex,
       opex IS NULL                                                            AS opex_is_estimate,
       net_sales - (food_cogs + beverage_cogs + packaging_cogs) - labor_cost
         - coalesce(opex, trailing_opex_ratio * net_sales)                     AS four_wall_ebitda
FROM j;

-- ---------------------------------------------------------------- revenue flow (Sankey: source -> target)
CREATE OR REPLACE VIEW analytics.revenue_flow AS
WITH p AS (SELECT * FROM analytics.monthly_pnl),
pc AS (
    SELECT location_id, date_trunc('month', invoice_date)::date AS month_start, purchase_category, sum(amount) AS amt
    FROM pos.purchases GROUP BY 1, 2, 3
)
SELECT location_id, month_start, 1 AS stage, x.source, 'Net sales' AS target, x.amount
FROM p CROSS JOIN LATERAL (VALUES ('Dine-in', p.dine_in_sales), ('Takeout', p.takeout_sales), ('Delivery', p.delivery_sales)) x(source, amount)
UNION ALL
SELECT location_id, month_start, 2, 'Net sales', x.target, x.amount
FROM p CROSS JOIN LATERAL (VALUES ('Food & beverage cost', p.total_cogs), ('Labor', p.labor_cost), ('Rent', p.rent),
                                  ('Delivery commissions', p.delivery_commissions), ('Card fees', p.card_fees),
                                  ('Other operating', p.other_opex), ('Four-wall EBITDA', greatest(p.four_wall_ebitda, 0))) x(target, amount)
UNION ALL
SELECT location_id, month_start, 3, 'Labor', x.target, x.amount
FROM p CROSS JOIN LATERAL (VALUES ('Kitchen staff', p.kitchen_labor_cost), ('Front of house', p.foh_labor_cost), ('Management', p.mgmt_labor_cost)) x(target, amount)
UNION ALL
SELECT location_id, month_start, 3, 'Food & beverage cost', purchase_category, amt FROM pc;

-- P&L waterfall steps (native Power BI waterfall: Category = step, Y = amount)
CREATE OR REPLACE VIEW analytics.pnl_waterfall AS
SELECT location_id, month_start, x.step_order, x.step, x.amount
FROM analytics.monthly_pnl p CROSS JOIN LATERAL (VALUES
    (1, 'Net sales', p.net_sales), (2, 'Food & beverage', -p.total_cogs), (3, 'Labor', -p.labor_cost),
    (4, 'Rent', -p.rent), (5, 'Delivery commissions', -p.delivery_commissions), (6, 'Card fees', -p.card_fees),
    (7, 'Other operating', -p.other_opex)) x(step_order, step, amount);

-- ---------------------------------------------------------------- menu engineering (last 365 days and last 90 days)
CREATE OR REPLACE VIEW analytics.menu_engineering AS
WITH clock AS (SELECT business_date AS today FROM analytics.v_clock),
periods AS (SELECT 'Last 365 days' AS period, today - 365 AS d0, today AS d1 FROM clock
            UNION ALL SELECT 'Last 90 days', today - 90, today FROM clock),
base AS (
    SELECT p.period, coalesce(d.location_id, 'ALL') AS location_id, d.item_id,
           sum(d.units) AS units, sum(d.sales) AS sales, sum(d.theoretical_cost) AS food_cost
    FROM analytics.item_daily d JOIN periods p ON d.business_date > p.d0 AND d.business_date <= p.d1
    GROUP BY GROUPING SETS ((p.period, d.location_id, d.item_id), (p.period, d.item_id))
),
m AS (
    SELECT b.*, mi.item_name, mi.category,
           (b.sales - b.food_cost) / b.units AS cm_per_unit,
           b.sales - b.food_cost AS total_cm,
           b.units::numeric / sum(b.units) OVER w AS menu_mix,
           count(*) OVER w AS items_in_category,
           sum(b.sales - b.food_cost) OVER w / sum(b.units) OVER w AS avg_cm_category
    FROM base b JOIN pos.dim_menu_items mi USING (item_id)
    WINDOW w AS (PARTITION BY b.period, b.location_id, mi.category)
)
SELECT *, 0.7 / items_in_category AS popularity_threshold,
       CASE WHEN menu_mix >= 0.7 / items_in_category AND cm_per_unit >= avg_cm_category THEN 'Star'
            WHEN menu_mix >= 0.7 / items_in_category THEN 'Plowhorse'
            WHEN cm_per_unit >= avg_cm_category THEN 'Puzzle'
            ELSE 'Dog' END AS menu_class
FROM m;

-- ---------------------------------------------------------------- LIVE views (straight from POS tables)
CREATE OR REPLACE VIEW analytics.live_floor AS
SELECT t.location_id, t.table_id, t.zone, t.seats, t.status, t.party_size, t.seated_at,
       CASE WHEN t.status = 'occupied' THEN round(extract(epoch FROM c.now_local - t.seated_at) / 60)::int END AS minutes_seated,
       CASE WHEN t.status = 'occupied' THEN greatest(0, round(extract(epoch FROM t.expected_close - c.now_local) / 60))::int END AS minutes_to_turn,
       CASE WHEN t.status = 'occupied' THEN t.party_size::numeric / t.seats END AS seat_fill,
       row_number() OVER (PARTITION BY t.location_id, t.zone ORDER BY t.table_id) AS table_no
FROM pos.table_status t CROSS JOIN analytics.v_clock c;

CREATE OR REPLACE VIEW analytics.live_today AS
WITH c AS (SELECT * FROM analytics.v_clock),
o AS (
    SELECT o.location_id,
           sum(o.net_sales) FILTER (WHERE o.status = 'closed') AS sales_today,
           count(*) FILTER (WHERE o.status = 'closed') AS checks_today,
           sum(o.covers) FILTER (WHERE o.status IN ('open','closed')) AS covers_today,
           count(*) FILTER (WHERE o.status = 'open') AS open_checks,
           avg(o.wait_min) FILTER (WHERE o.wait_min > 0) AS avg_wait_min,
           count(*) FILTER (WHERE o.wait_min > 0) AS parties_waited,
           max(o.open_ts) AS last_order_ts
    FROM pos.orders o, c WHERE o.business_date = c.business_date GROUP BY 1
),
f AS (
    SELECT location_id, count(*) FILTER (WHERE status = 'occupied') AS tables_occupied, count(*) AS tables_total,
           coalesce(sum(party_size) FILTER (WHERE status = 'occupied'), 0) AS guests_seated,
           sum(seats) AS seats_total, coalesce(sum(seats) FILTER (WHERE status = 'occupied'), 0) AS seats_blocked
    FROM pos.table_status GROUP BY 1
),
lp AS (SELECT l.location_id, count(*) AS lost_parties_today, sum(party_size) AS lost_covers_today
       FROM pos.lost_parties l, c WHERE l.business_date = c.business_date GROUP BY 1),
lab AS (
    SELECT s.location_id,
           count(*) FILTER (WHERE s.status = 'on_clock') AS staff_on_clock,
           sum(CASE WHEN s.status = 'completed' THEN s.total_labor_cost
                    ELSE extract(epoch FROM c.now_local - s.clock_in_ts) / 3600 * s.hourly_rate * 1.12 END)
               FILTER (WHERE s.status <> 'scheduled') AS labor_cost_today,
           sum(CASE WHEN s.status = 'completed' THEN s.hours ELSE extract(epoch FROM c.now_local - s.clock_in_ts) / 3600 END)
               FILTER (WHERE s.status <> 'scheduled') AS labor_hours_today
    FROM pos.labor_shifts s, c WHERE s.business_date = c.business_date GROUP BY 1
)
SELECT l.location_id, c.business_date, c.now_local,
       coalesce(o.sales_today, 0) AS sales_today, coalesce(o.checks_today, 0) AS checks_today,
       coalesce(o.covers_today, 0) AS covers_today, coalesce(o.open_checks, 0) AS open_checks,
       o.avg_wait_min, coalesce(o.parties_waited, 0) AS parties_waited, o.last_order_ts,
       f.tables_occupied, f.tables_total, f.guests_seated, f.seats_total, f.seats_blocked,
       coalesce(lp.lost_parties_today, 0) AS lost_parties_today, coalesce(lp.lost_covers_today, 0) AS lost_covers_today,
       coalesce(lab.staff_on_clock, 0) AS staff_on_clock, coalesce(lab.labor_cost_today, 0) AS labor_cost_today,
       coalesce(lab.labor_hours_today, 0) AS labor_hours_today
FROM pos.dim_locations l CROSS JOIN c
LEFT JOIN o USING (location_id) LEFT JOIN f USING (location_id) LEFT JOIN lp USING (location_id) LEFT JOIN lab USING (location_id);

-- today's sales by hour next to the expected pace (pacing_today is written by the evaluator)
CREATE TABLE IF NOT EXISTS analytics.pacing_today (
    location_id text, business_date date, hr int, expected_sales numeric, expected_covers numeric,
    expected_cum_sales numeric, PRIMARY KEY (location_id, business_date, hr)
);
CREATE OR REPLACE VIEW analytics.live_hourly_today AS
WITH c AS (SELECT * FROM analytics.v_clock),
s AS (
    SELECT o.location_id, extract(hour FROM o.open_ts)::int AS hr, sum(o.net_sales) AS sales, sum(o.covers) AS covers
    FROM pos.orders o, c WHERE o.business_date = c.business_date AND o.status = 'closed' GROUP BY 1, 2
)
SELECT h.location_id, h.hr, dh.hour_label, coalesce(s.sales, 0) AS sales, coalesce(s.covers, 0) AS covers,
       h.expected_sales, h.expected_covers,
       sum(coalesce(s.sales, 0)) OVER (PARTITION BY h.location_id ORDER BY h.hr) AS cum_sales,
       h.expected_cum_sales,
       h.hr <= extract(hour FROM c.now_local) AS is_elapsed,
       h.hr < extract(hour FROM c.now_local) - 1 AS is_settled      -- checks opened in this hour have closed
FROM analytics.pacing_today h
JOIN c ON h.business_date = c.business_date
JOIN analytics.dim_hour dh ON dh.hr = h.hr
LEFT JOIN s ON s.location_id = h.location_id AND s.hr = h.hr;

-- live staffing vs demand this hour
CREATE OR REPLACE VIEW analytics.live_staffing AS
SELECT s.location_id, s.role, count(*) FILTER (WHERE s.status = 'on_clock') AS on_clock,
       count(*) FILTER (WHERE s.status = 'scheduled') AS still_scheduled,
       count(*) FILTER (WHERE s.status = 'completed') AS completed
FROM pos.labor_shifts s JOIN analytics.v_clock c ON s.business_date = c.business_date
GROUP BY 1, 2;
