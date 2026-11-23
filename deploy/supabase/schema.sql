-- BasketWise Supabase schema. Run once in the Supabase SQL editor (safe to re-run).
--
-- Supabase is the system of record: every store's grocery catalogue, every price
-- change ever seen, and the latest built API artifact (in Storage). The API reads
-- `stores`, `store_products` and `price_history` for GET /products/{id}/price-trend
-- (backend/routes/price_trend.py). deploy/scripts/sync_supabase.py writes them.
--
-- Sized for the free plan (0.5 GB): marketplace listings are never uploaded, and
-- price_history stores only price *changes*, not one row per product per day.

create table if not exists stores (
  id   bigint generated always as identity primary key,
  code text not null unique,
  name text not null
);

create table if not exists store_products (
  id                  bigint generated always as identity primary key,
  store_id            bigint not null references stores(id) on delete cascade,
  external_product_id text   not null,          -- the retailer's own sku / stockcode
  name                text,
  brand               text,
  description         text,
  image_url           text,
  package_size        text,
  source_category     text,
  is_weighted         boolean,
  quantity            numeric,
  unit                text,
  barcode             text,
  url                 text,
  in_stock            boolean,
  last_seen           timestamptz,
  updated_at          timestamptz not null default now(),
  unique (store_id, external_product_id)
);
create index if not exists idx_store_products_external on store_products (external_product_id);
create index if not exists idx_store_products_barcode  on store_products (barcode) where barcode is not null;

create table if not exists price_history (
  id               bigint generated always as identity primary key,
  store_product_id bigint not null references store_products(id) on delete cascade,
  price            numeric(10,2) not null,
  was_price        numeric(10,2),
  captured_at      timestamptz not null default now(),
  -- makes uploads idempotent: re-syncing the same crawl inserts nothing
  unique (store_product_id, captured_at)
);

-- Row Level Security on, with no policies: the anon/publishable key can read and
-- write nothing. Only the secret (service-role) key used by the EC2 box bypasses
-- RLS. Never put the secret key in the frontend.
alter table stores         enable row level security;
alter table store_products enable row level security;
alter table price_history  enable row level security;

insert into stores (code, name) values
  ('coles', 'Coles'),
  ('woolworths', 'Woolworths'),
  ('aldi', 'ALDI'),
  ('harrisfarm', 'Harris Farm Markets')
on conflict (code) do nothing;

-- Private bucket holding the latest built API artifact (basketwise.db + warm/),
-- so a fresh server can be restored without re-crawling.
insert into storage.buckets (id, name, public)
values ('artifacts', 'artifacts', false)
on conflict (id) do nothing;

-- Handy size check (free plan cap is 0.5 GB):
--   select pg_size_pretty(pg_database_size(current_database()));
