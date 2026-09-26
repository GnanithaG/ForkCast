-- ============================================================================
-- 20_evaluation.sql · tables written by realtime/evaluator.py
-- ============================================================================
CREATE TABLE IF NOT EXISTS analytics.kpi_definitions (
    kpi_id text PRIMARY KEY, kpi_name text, category text, unit text, direction text,
    target numeric, warn_limit numeric, min_days int, formula text, why_it_matters text, sort_order int
);

-- current value of every KPI, for every store and the chain, for several periods
CREATE TABLE IF NOT EXISTS analytics.kpi_snapshot (
    snapshot_ts timestamp, scope text, period text, period_sort int, period_start date, period_end date,
    kpi_id text, value numeric, last_year_value numeric, change_vs_ly numeric, target numeric,
    status text, status_sort int,
    PRIMARY KEY (scope, period, kpi_id)
);

CREATE TABLE IF NOT EXISTS analytics.okr_objectives (
    objective_id text PRIMARY KEY, objective text, owner text, cycle text, cycle_start date, cycle_end date
);
CREATE TABLE IF NOT EXISTS analytics.okr_key_results (
    kr_id text PRIMARY KEY, objective_id text, key_result text, kpi_id text, kind text, amount numeric,
    scope text, owner text
);
-- one row per evaluation run, per KR, per scope (history lets Power BI chart progress over time)
CREATE TABLE IF NOT EXISTS analytics.okr_progress (
    evaluated_at timestamp, eval_date date, kr_id text, scope text,
    baseline numeric, target numeric, current_value numeric, progress numeric, expected_progress numeric,
    status text, status_sort int, note text,
    PRIMARY KEY (eval_date, kr_id, scope)
);
CREATE OR REPLACE VIEW analytics.okr_latest AS
SELECT p.*, k.objective_id, k.key_result, k.kpi_id, k.owner, o.objective,
       d.unit, d.direction
FROM analytics.okr_progress p
JOIN analytics.okr_key_results k USING (kr_id)
JOIN analytics.okr_objectives o USING (objective_id)
JOIN analytics.kpi_definitions d ON d.kpi_id = k.kpi_id
WHERE p.eval_date = (SELECT max(eval_date) FROM analytics.okr_progress);

-- pain points: rule-based and statistical detections with estimated $ impact
CREATE TABLE IF NOT EXISTS analytics.alerts (
    alert_key text PRIMARY KEY,           -- rule + scope, so a recurring problem stays one alert
    rule_id text, location_id text, category text, severity text, severity_sort int,
    title text, metric text, value numeric, benchmark numeric, est_annual_impact numeric,
    evidence text, recommended_action text, horizon text,
    first_seen timestamp, last_seen timestamp, status text
);

CREATE TABLE IF NOT EXISTS analytics.forecast_daily (
    run_date date, location_id text, date date, sales_p10 numeric, sales_p50 numeric, sales_p90 numeric,
    covers_p50 numeric, PRIMARY KEY (run_date, location_id, date)
);
CREATE OR REPLACE VIEW analytics.forecast_latest AS
SELECT f.*, d.month_start
FROM analytics.forecast_daily f JOIN analytics.dim_date d ON d.date = f.date
WHERE f.run_date = (SELECT max(run_date) FROM analytics.forecast_daily);

-- forecast vs actual for days that have happened (uses the forecast made before each day)
CREATE OR REPLACE VIEW analytics.forecast_vs_actual AS
WITH f AS (
    SELECT DISTINCT ON (location_id, date) location_id, date, sales_p50, sales_p10, sales_p90, run_date
    FROM analytics.forecast_daily WHERE run_date < date
    ORDER BY location_id, date, run_date DESC
)
SELECT f.location_id, f.date, f.run_date, f.sales_p50 AS forecast, f.sales_p10, f.sales_p90, d.net_sales AS actual,
       abs(d.net_sales - f.sales_p50) / nullif(d.net_sales, 0) AS abs_pct_error,
       d.net_sales BETWEEN f.sales_p10 AND f.sales_p90 AS within_band
FROM f JOIN analytics.daily_location d ON d.location_id = f.location_id AND d.business_date = f.date
WHERE f.date < (SELECT business_date FROM analytics.v_clock);

CREATE TABLE IF NOT EXISTS analytics.evaluator_runs (
    run_at timestamptz PRIMARY KEY, sim_clock timestamp, seconds numeric, kpis int, alerts_open int, note text
);

-- ---------------------------------------------------------------- Power BI convenience views
CREATE OR REPLACE VIEW analytics.scope_labels AS
SELECT 'ALL' AS scope, 'All locations' AS scope_label, 0 AS scope_sort
UNION ALL SELECT location_id, name, row_number() OVER (ORDER BY location_id)::int FROM pos.dim_locations;

CREATE OR REPLACE VIEW analytics.kpi_scorecard AS
SELECT s.*, d.kpi_name, d.category, d.unit, d.direction, d.sort_order, d.formula, l.scope_label, l.scope_sort,
       CASE s.status WHEN 'good' THEN '●' WHEN 'warning' THEN '▲' WHEN 'critical' THEN '■' ELSE '·' END AS status_icon
FROM analytics.kpi_snapshot s
JOIN analytics.kpi_definitions d USING (kpi_id)
JOIN analytics.scope_labels l USING (scope);

CREATE OR REPLACE VIEW analytics.okr_board AS
SELECT o.*, l.scope_label, l.scope_sort
FROM analytics.okr_latest o JOIN analytics.scope_labels l ON l.scope = o.scope;

CREATE OR REPLACE VIEW analytics.okr_history AS
SELECT p.eval_date, p.kr_id, p.scope, l.scope_label, k.key_result, k.objective_id, p.progress, p.status
FROM analytics.okr_progress p
JOIN analytics.okr_key_results k ON k.kr_id = p.kr_id
JOIN analytics.scope_labels l ON l.scope = p.scope;

CREATE OR REPLACE VIEW analytics.alerts_open AS
SELECT a.*, d.name AS location_name
FROM analytics.alerts a JOIN pos.dim_locations d USING (location_id)
WHERE a.status = 'open';

CREATE OR REPLACE VIEW analytics.menu_board AS
SELECT m.*, l.scope_label
FROM analytics.menu_engineering m JOIN analytics.scope_labels l ON l.scope = m.location_id;

CREATE OR REPLACE VIEW analytics.alerts_live AS
SELECT * FROM analytics.alerts_open WHERE horizon IN ('Today', 'Now');
