-- ============================================================================
-- 00_schema.sql · operational (POS) schema written by the loader and the live streamer
--   pos.*   = raw operational data, as a restaurant POS / labor / AP system would hold it
--   Times are restaurant-local (America/New_York) wall-clock timestamps.
-- ============================================================================
CREATE SCHEMA IF NOT EXISTS pos;
CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS pos.dim_locations (
    location_id   text PRIMARY KEY,
    name          text NOT NULL,
    city          text,
    county        text,
    open_date     date,
    monthly_rent  numeric(12,2),
    sqft          integer,
    seats         integer,
    tables        integer
);

CREATE TABLE IF NOT EXISTS pos.dim_tables (
    table_id     text PRIMARY KEY,
    location_id  text REFERENCES pos.dim_locations,
    zone         text,
    seats        integer
);

CREATE TABLE IF NOT EXISTS pos.dim_menu_items (
    item_id     text PRIMARY KEY,
    item_name   text,
    category    text,
    base_price  numeric(8,2),
    plate_cost  numeric(8,2)
);

CREATE TABLE IF NOT EXISTS pos.dim_employees (
    employee_id  text PRIMARY KEY,
    location_id  text REFERENCES pos.dim_locations,
    role         text
);

-- one row per guest check; status 'open' while guests are seated / order is being prepared
CREATE TABLE IF NOT EXISTS pos.orders (
    check_id           text PRIMARY KEY,
    location_id        text NOT NULL REFERENCES pos.dim_locations,
    business_date      date NOT NULL,
    channel            text NOT NULL,          -- Dine-in / Takeout / Delivery
    delivery_platform  text,
    table_id           text,
    zone               text,
    covers             integer NOT NULL,
    daypart            text NOT NULL,          -- lunch / dinner
    arrival_ts         timestamp,
    open_ts            timestamp NOT NULL,
    close_ts           timestamp,
    wait_min           numeric(6,1) DEFAULT 0,
    subtotal           numeric(12,2),
    discount           numeric(12,2),
    net_sales          numeric(12,2),
    tax                numeric(12,2),
    tip                numeric(12,2),
    payment_type       text,
    status             text NOT NULL DEFAULT 'closed',
    ingested_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS orders_loc_date ON pos.orders (location_id, business_date);
CREATE INDEX IF NOT EXISTS orders_date ON pos.orders (business_date);
CREATE INDEX IF NOT EXISTS orders_open ON pos.orders (status) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS pos.order_items (
    check_id          text NOT NULL,
    item_id           text NOT NULL,
    quantity          integer NOT NULL,
    unit_price        numeric(8,2),
    line_total        numeric(12,2),
    theoretical_cost  numeric(12,2)
);
CREATE INDEX IF NOT EXISTS order_items_check ON pos.order_items (check_id);

CREATE TABLE IF NOT EXISTS pos.lost_parties (
    lost_id          bigserial PRIMARY KEY,
    location_id      text NOT NULL,
    business_date    date NOT NULL,
    arrival_ts       timestamp,
    party_size       integer,
    quoted_wait_min  numeric(6,1)
);
CREATE INDEX IF NOT EXISTS lost_loc_date ON pos.lost_parties (location_id, business_date);

-- one row per employee shift; live shifts move scheduled -> on_clock -> completed
CREATE TABLE IF NOT EXISTS pos.labor_shifts (
    shift_id          text PRIMARY KEY,
    location_id       text NOT NULL,
    business_date     date NOT NULL,
    role              text,
    daypart           text,
    start_hour        numeric(4,2),
    hours             numeric(5,2),
    hourly_rate       numeric(6,2),
    employee_id       text,
    wage_cost         numeric(10,2),
    burden_cost       numeric(10,2),
    total_labor_cost  numeric(10,2),
    clock_in_ts       timestamp,
    clock_out_ts      timestamp,
    status            text NOT NULL DEFAULT 'completed'
);
CREATE INDEX IF NOT EXISTS labor_loc_date ON pos.labor_shifts (location_id, business_date);

CREATE TABLE IF NOT EXISTS pos.purchases (
    invoice_id         text PRIMARY KEY,
    location_id        text NOT NULL,
    invoice_date       date NOT NULL,
    vendor             text,
    purchase_category  text,
    amount             numeric(12,2)
);

CREATE TABLE IF NOT EXISTS pos.waste_log (
    waste_id       bigserial PRIMARY KEY,
    location_id    text NOT NULL,
    business_date  date NOT NULL,
    waste_cost     numeric(10,2),
    reason         text
);

CREATE TABLE IF NOT EXISTS pos.operating_expenses (
    location_id       text NOT NULL,
    month             date NOT NULL,
    expense_category  text NOT NULL,
    amount            numeric(12,2),
    PRIMARY KEY (location_id, month, expense_category)
);

-- live floor: current state of every table (maintained by the streamer)
CREATE TABLE IF NOT EXISTS pos.table_status (
    table_id        text PRIMARY KEY,
    location_id     text NOT NULL,
    zone            text,
    seats           integer,
    status          text NOT NULL DEFAULT 'free',     -- free / occupied
    check_id        text,
    party_size      integer,
    seated_at       timestamp,
    expected_close  timestamp,
    updated_at      timestamptz DEFAULT now()
);

-- heartbeat so dashboards can show data freshness
CREATE TABLE IF NOT EXISTS pos.stream_status (
    id             integer PRIMARY KEY DEFAULT 1,
    sim_clock      timestamp,           -- restaurant-local time of the last processed event window
    last_tick_at   timestamptz,         -- wall-clock time of the last write
    mode           text,
    speed          numeric,
    orders_written bigint DEFAULT 0
);
