-- Bid Caller Pro / CurbCall — shared data tables for cross-device sync.
-- Run this once in Supabase Dashboard -> SQL Editor -> New query -> Run.
-- Row Level Security ensures each signed-in user can only ever see/edit
-- their own rows — the anon key + a user's access token is all either
-- client needs; no service-role key required for normal app operation.

create table if not exists saved_bids (
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
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
  user_id uuid primary key default auth.uid() references auth.users(id) on delete cascade,
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
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  location text not null,
  radius integer not null,
  created_at timestamptz default now()
);
alter table saved_searches enable row level security;
drop policy if exists "Users manage their own saved searches" on saved_searches;
create policy "Users manage their own saved searches" on saved_searches
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- Profile picture support: a column to hold the public URL, and a public
-- Storage bucket to hold the actual image files. Safe to re-run this whole
-- script any time — every statement below is idempotent.
alter table company_profiles add column if not exists avatar_url text default '';

insert into storage.buckets (id, name, public)
values ('avatars', 'avatars', true)
on conflict (id) do nothing;

-- Anyone can view avatar images — they're meant to be public profile pictures.
drop policy if exists "Public read access for avatars" on storage.objects;
create policy "Public read access for avatars" on storage.objects
  for select using (bucket_id = 'avatars');

-- A user can only upload/replace/delete their OWN avatar — enforced by
-- requiring the file path's first folder segment to be their own user id
-- (the app uploads to "{user_id}/avatar.<ext>").
drop policy if exists "Users manage their own avatar" on storage.objects;
create policy "Users manage their own avatar" on storage.objects
  for all using (bucket_id = 'avatars' and (storage.foldername(name))[1] = auth.uid()::text)
  with check (bucket_id = 'avatars' and (storage.foldername(name))[1] = auth.uid()::text);

-- The three scan feeds, so they follow the account instead of the browser.
-- Starred bids, the company profile and saved searches already synced; the
-- feeds did not, so signing in from a different browser showed an empty Bids
-- tab on the same account.
--
-- One row per user with each feed as jsonb, rather than a row per bid: a feed
-- is read and replaced wholesale on every scan and never queried
-- field-by-field, so per-bid rows would add churn and joins and buy nothing.
-- (saved_bids above stays per-row precisely because those ARE addressed
-- individually — starred, given a pipeline status, annotated.)
-- lead_status is here too: a Leads feed without the statuses set on it is
-- half the information.
create table if not exists user_feeds (
  user_id uuid primary key default auth.uid() references auth.users(id) on delete cascade,
  bids jsonb default '{}'::jsonb,
  upcoming jsonb default '{}'::jsonb,
  leads jsonb default '{}'::jsonb,
  lead_status jsonb default '{}'::jsonb,
  updated_at timestamptz default now()
);
alter table user_feeds enable row level security;
drop policy if exists "Users manage their own feeds" on user_feeds;
create policy "Users manage their own feeds" on user_feeds
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
