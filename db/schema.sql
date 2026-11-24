create table if not exists customers (
  id text primary key,
  name text not null,
  email text not null,
  phone text not null,
  telegram_chat_id text,
  telegram_link_token text not null unique,
  subscription_status text not null,
  created_at timestamptz not null default now()
);

create table if not exists oauth_sessions (
  id text primary key,
  customer_id text not null references customers (id),
  created_at timestamptz not null default now(),
  consumed_at timestamptz
);

create table if not exists sites (
  id text primary key,
  customer_id text not null references customers (id),
  solaredge_site_id text not null,
  portal_name text not null,
  card_name text not null,
  report_label text,
  timezone text not null default 'Asia/Jerusalem',
  latitude double precision,
  longitude double precision,
  installed_kw_dc double precision,
  portal_peak_kw double precision,
  tariff_agorot_per_kwh integer,
  income_basis text not null,
  expected_daily_wh integer,
  soiling_index double precision not null default 0,
  monitor boolean not null default true,
  created_at timestamptz not null default now(),
  unique (customer_id, solaredge_site_id)
);

create table if not exists site_tokens (
  site_id text primary key references sites (id),
  access_ciphertext text not null,
  refresh_ciphertext text not null,
  access_expires_at timestamptz,
  updated_at timestamptz not null default now(),
  last_refresh_error text,
  last_refresh_ok_at timestamptz
);

create table if not exists raw_pulls (
  id text primary key,
  site_id text not null references sites (id),
  customer_id text not null references customers (id),
  pulled_at timestamptz not null,
  local_date date not null,
  kind text not null,
  endpoint text not null,
  credits integer not null,
  ok boolean not null,
  body jsonb
);

create table if not exists daily_summaries (
  id text primary key,
  customer_id text not null references customers (id),
  local_date date not null,
  payload jsonb not null,
  text text not null,
  created_at timestamptz not null default now(),
  sent_at timestamptz,
  unique (customer_id, local_date)
);

create table if not exists alerts (
  id text primary key,
  site_id text not null references sites (id),
  customer_id text not null,
  dedupe_key text not null,
  code text not null,
  serial text,
  opened_at timestamptz not null,
  status text not null,
  detected_at timestamptz not null,
  notified_at timestamptz,
  raw jsonb,
  unique (site_id, dedupe_key)
);

create table if not exists credit_ledger (
  id text primary key,
  customer_id text not null,
  site_id text not null,
  at timestamptz not null,
  credits integer not null,
  endpoint text not null,
  local_date date not null
);

create table if not exists llm_usage (
  id text primary key,
  customer_id text not null,
  at timestamptz not null,
  model text not null,
  input_tokens integer not null,
  output_tokens integer not null
);

create table if not exists payments (
  id text primary key,
  customer_id text not null references customers (id),
  amount_agorot integer not null,
  currency text not null,
  status text not null,
  provider text not null,
  description text not null,
  created_at timestamptz not null default now()
);

create index if not exists credit_ledger_customer_at_idx on credit_ledger (customer_id, at);
create index if not exists raw_pulls_site_day_idx on raw_pulls (site_id, local_date, kind);
create index if not exists llm_usage_customer_at_idx on llm_usage (customer_id, at);
