-- Bid Caller Pro / CurbCall — shared data tables for cross-device sync.
-- Run this once in Supabase Dashboard -> SQL Editor -> New query -> Run.
-- Row Level Security ensures each signed-in user can only ever see/edit
-- their own rows — the anon key + a user's access token is all either
-- client needs; no service-role key required for normal app operation.

create table if not exists saved_bids (
  user_id uuid references auth.users(id) on delete cascade,
  bid_id text not null,
  city text default '',
  title text default '',
  scope text default '',
  value text default '',
  deadline text default '',
  contact text default '',
  email text default '',
  phone text default '',
  url text default '',
  status text default '',
  pipeline text default '',
  note text default '',
  saved_at text default '',
  updated_at timestamptz default now(),
  primary key (user_id, bid_id)
);
alter table saved_bids enable row level security;
drop policy if exists "Users manage their own saved bids" on saved_bids;
create policy "Users manage their own saved bids" on saved_bids
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

create table if not exists company_profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  name text default '',
  contact text default '',
  phone text default '',
  email text default '',
  specialty text default '',
  updated_at timestamptz default now()
);
alter table company_profiles enable row level security;
drop policy if exists "Users manage their own company profile" on company_profiles;
create policy "Users manage their own company profile" on company_profiles
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

create table if not exists saved_searches (
  id bigint generated always as identity primary key,
  user_id uuid references auth.users(id) on delete cascade,
  location text not null,
  radius integer not null,
  created_at timestamptz default now()
);
alter table saved_searches enable row level security;
drop policy if exists "Users manage their own saved searches" on saved_searches;
create policy "Users manage their own saved searches" on saved_searches
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
