-- ============================================================================
-- 01_staging.sql  ·  raw parquet -> typed staging views + calendar
-- ============================================================================
CREATE OR REPLACE VIEW dim_locations      AS SELECT * FROM read_parquet('{RAW}/dim_locations.parquet');
CREATE OR REPLACE VIEW dim_tables         AS SELECT * FROM read_parquet('{RAW}/dim_tables.parquet');
CREATE OR REPLACE VIEW dim_menu_items     AS SELECT * FROM read_parquet('{RAW}/dim_menu_items.parquet');
CREATE OR REPLACE VIEW dim_employees      AS SELECT * FROM read_parquet('{RAW}/dim_employees.parquet');
CREATE OR REPLACE VIEW fact_checks        AS SELECT * FROM read_parquet('{RAW}/fact_checks.parquet');
CREATE OR REPLACE VIEW fact_check_items   AS SELECT * FROM read_parquet('{RAW}/fact_check_items.parquet');
CREATE OR REPLACE VIEW fact_lost_demand   AS SELECT * FROM read_parquet('{RAW}/fact_lost_demand.parquet');
CREATE OR REPLACE VIEW fact_labor_shifts  AS SELECT * FROM read_parquet('{RAW}/fact_labor_shifts.parquet');
CREATE OR REPLACE VIEW fact_purchases     AS SELECT * FROM read_parquet('{RAW}/fact_purchases.parquet');
CREATE OR REPLACE VIEW fact_waste_log     AS SELECT * FROM read_parquet('{RAW}/fact_waste_log.parquet');
CREATE OR REPLACE VIEW fact_operating_expenses AS SELECT * FROM read_parquet('{RAW}/fact_operating_expenses.parquet');

CREATE OR REPLACE TABLE dim_calendar AS
SELECT d::DATE                                   AS cal_date,
       date_trunc('month', d)::DATE               AS month,
       year(d)                                    AS year,
       month(d)                                   AS month_num,
       dayname(d)                                 AS weekday_name,
       isodow(d)                                  AS iso_dow,          -- 1 = Mon
       isodow(d) IN (5, 6)                        AS is_fri_sat,
       CASE WHEN month(d) IN (1,2,3,4) THEN 'Peak season'
            WHEN month(d) IN (6,7,8,9) THEN 'Off season'
            ELSE 'Shoulder' END                   AS fl_season
FROM range(DATE '{START}', DATE '{END}' + INTERVAL 1 DAY, INTERVAL 1 DAY) t(d);

-- Operating hour grid: 11:00-22:00 daily, plus the 22:00 hour on Fri/Sat
CREATE OR REPLACE TABLE dim_open_hours AS
SELECT s.location_id, s.business_date, h.hr
FROM (SELECT DISTINCT location_id, business_date FROM fact_checks) s
CROSS JOIN (SELECT unnest(range(11, 23)) AS hr) h
WHERE h.hr < 22 OR isodow(s.business_date) IN (5, 6);
