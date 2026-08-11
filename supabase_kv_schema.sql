-- Durable key/value storage for Bid Caller Pro.
--
-- Run this ONCE in the Supabase SQL Editor (Dashboard -> SQL Editor -> New
-- query -> paste -> Run). It is safe to run again; nothing is dropped.
--
-- Why this exists: Render's filesystem is wiped on every deploy and every time
-- a free-tier instance spins down, so anything the server writes to a local
-- file is lost almost immediately. That includes the licence database, the
-- portal directory the scanner builds up as it learns, and the geocode cache.
-- This table is where those live instead.

create table if not exists public.kv_store (
    key        text primary key,
    value      jsonb       not null,
    updated_at timestamptz not null default now()
);

-- Keep updated_at honest, so it's obvious when a value last changed.
create or replace function public.kv_store_touch()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists kv_store_touch on public.kv_store;
create trigger kv_store_touch
    before update on public.kv_store
    for each row execute function public.kv_store_touch();

-- RLS on with NO policies is deliberate and is the whole security model here.
--
-- This table holds the licence database: issued keys, who owns them, and trial
-- records. The service-role key bypasses RLS, so the server can read and write
-- it; the anon key that ships in the web app cannot, because no policy grants
-- it anything. Adding a permissive policy here would publish your licence keys
-- to anyone who opened the app and looked at the network tab.
alter table public.kv_store enable row level security;

-- Nothing below is required; it just makes the table easier to inspect by hand
-- in the dashboard.
comment on table public.kv_store is
    'Durable server state: licence db, portal directory, geo + scan caches. '
    'Service-role access only — RLS is enabled with no policies on purpose.';
