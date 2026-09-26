-- ============================================================================
-- 02_floor_ops.sql  ·  floor usage & operational metrics
--   seat utilization, table utilization, RevPASH, table turns, dwell time,
--   wait times, lost (walk-away) demand, party-to-table fit
-- ============================================================================

-- Seat & table minutes occupied in every open hour (a party spanning 7:40-9:05
-- counts 20 min in the 7pm hour, 60 in 8pm, 5 in 9pm)
CREATE OR REPLACE TABLE mart_floor_hourly AS
WITH dine AS (
    SELECT c.location_id, c.business_date, c.open_ts, c.close_ts, c.covers, c.net_sales, t.seats AS table_seats
    FROM fact_checks c JOIN dim_tables t USING (table_id)
    WHERE c.channel = 'Dine-in'
),
overlap AS (
    SELECT g.location_id, g.business_date, g.hr,
           greatest(0, epoch(least(d.close_ts, g.business_date::TIMESTAMP + to_hours(g.hr + 1)))
                     - epoch(greatest(d.open_ts, g.business_date::TIMESTAMP + to_hours(g.hr)))) / 60.0 AS mins,
           d.covers, d.table_seats
    FROM dim_open_hours g
    JOIN dine d
      ON d.location_id = g.location_id AND d.business_date = g.business_date
     AND d.open_ts  < g.business_date::TIMESTAMP + to_hours(g.hr + 1)
     AND d.close_ts > g.business_date::TIMESTAMP + to_hours(g.hr)
),
occ AS (
    SELECT location_id, business_date, hr,
           sum(mins * covers)      AS occupied_seat_min,
           sum(mins * table_seats) AS blocked_seat_min       -- seats held by a seated party (incl. empty chairs)
    FROM overlap GROUP BY ALL
),
rev AS (   -- all sales attributed to the hour the check was opened
    SELECT location_id, business_date, hour(open_ts) AS hr,
           sum(net_sales) AS net_sales,
           sum(CASE WHEN channel = 'Dine-in' THEN net_sales END) AS dine_sales,
           sum(CASE WHEN channel = 'Dine-in' THEN covers END)    AS dine_covers,
           count(*) AS checks
    FROM fact_checks GROUP BY ALL
)
SELECT g.location_id, g.business_date, g.hr,
       l.seats,
       l.seats * 60.0                               AS available_seat_min,
       coalesce(o.occupied_seat_min, 0)             AS occupied_seat_min,
       coalesce(o.blocked_seat_min, 0)              AS blocked_seat_min,
       coalesce(r.net_sales, 0)                     AS net_sales,
       coalesce(r.dine_sales, 0)                    AS dine_sales,
       coalesce(r.dine_covers, 0)                   AS dine_covers,
       coalesce(r.checks, 0)                        AS checks
FROM dim_open_hours g
JOIN dim_locations l USING (location_id)
LEFT JOIN occ o USING (location_id, business_date, hr)
LEFT JOIN rev r USING (location_id, business_date, hr);

-- Daily operating KPIs per location
CREATE OR REPLACE TABLE mart_daily_ops AS
WITH c AS (
    SELECT location_id, business_date,
           sum(net_sales)                                             AS net_sales,
           sum(discount)                                              AS discounts,
           count(*)                                                   AS checks,
           sum(covers)                                                AS covers,
           sum(CASE WHEN channel = 'Dine-in'  THEN net_sales END)     AS dine_in_sales,
           sum(CASE WHEN channel = 'Takeout'  THEN net_sales END)     AS takeout_sales,
           sum(CASE WHEN channel = 'Delivery' THEN net_sales END)     AS delivery_sales,
           sum(CASE WHEN channel = 'Dine-in'  THEN covers END)        AS dine_in_covers,
           count(CASE WHEN channel = 'Dine-in' THEN 1 END)            AS dine_in_parties,
           sum(CASE WHEN daypart = 'lunch'  THEN net_sales END)       AS lunch_sales,
           sum(CASE WHEN daypart = 'dinner' THEN net_sales END)       AS dinner_sales,
           avg(CASE WHEN channel = 'Dine-in' THEN epoch(close_ts - open_ts) / 60 END) AS avg_dwell_min,
           avg(CASE WHEN channel = 'Dine-in' AND wait_min > 0 THEN wait_min END)       AS avg_wait_when_waiting,
           count(CASE WHEN channel = 'Dine-in' AND wait_min > 0 THEN 1 END)            AS parties_waited
    FROM fact_checks GROUP BY ALL
),
f AS (
    SELECT location_id, business_date,
           sum(available_seat_min) AS available_seat_min,
           sum(occupied_seat_min)  AS occupied_seat_min,
           sum(blocked_seat_min)   AS blocked_seat_min,
           count(*)                AS open_hours
    FROM mart_floor_hourly GROUP BY ALL
),
lost AS (
    SELECT location_id, business_date, count(*) AS lost_parties, sum(party_size) AS lost_covers
    FROM fact_lost_demand GROUP BY ALL
)
SELECT c.*, l.seats, l.tables, f.open_hours,
       f.occupied_seat_min / f.available_seat_min                         AS seat_utilization,
       f.blocked_seat_min  / f.available_seat_min                         AS table_seat_blocked_pct,
       c.dine_in_sales / (l.seats * f.open_hours)                         AS revpash,
       c.dine_in_parties::DOUBLE / l.tables                               AS table_turns,
       coalesce(lost.lost_parties, 0)                                     AS lost_parties,
       coalesce(lost.lost_covers, 0)                                      AS lost_covers
FROM c
JOIN dim_locations l USING (location_id)
JOIN f USING (location_id, business_date)
LEFT JOIN lost USING (location_id, business_date);

-- Party size vs table size: how many seats sit empty at seated tables
CREATE OR REPLACE TABLE mart_table_fit AS
SELECT c.location_id, t.zone, t.seats AS table_size, c.covers AS party_size,
       count(*) AS parties,
       sum(t.seats - c.covers) AS empty_chairs
FROM fact_checks c JOIN dim_tables t USING (table_id)
WHERE c.channel = 'Dine-in' AND c.business_date >= DATE '{TTM_START}'
GROUP BY ALL;

-- Zone usage (main room / bar / patio)
CREATE OR REPLACE TABLE mart_zone_usage AS
SELECT c.location_id, t.zone, date_trunc('month', c.business_date)::DATE AS month,
       count(*) AS parties, sum(c.covers) AS covers, sum(c.net_sales) AS net_sales,
       avg(epoch(c.close_ts - c.open_ts) / 60) AS avg_dwell_min,
       sum(c.net_sales) / sum(c.covers) AS sales_per_cover
FROM fact_checks c JOIN dim_tables t USING (table_id)
WHERE c.channel = 'Dine-in'
GROUP BY ALL;
