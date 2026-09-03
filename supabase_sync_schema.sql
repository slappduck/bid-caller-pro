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

-- "create table if not exists" above never ALTERS a table that already
-- exists, so a project created against an older revision of this file can be
-- missing the user_id default -- and an insert that omits user_id then stores
-- NULL, which fails the RLS check auth.uid() = user_id with only "violates
-- row-level security policy" to show for it. Idempotent; harmless if already
-- set.
alter table company_profiles alter column user_id set default auth.uid();
alter table saved_bids       alter column user_id set default auth.uid();
alter table saved_searches   alter column user_id set default auth.uid();

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
-- Same repair as above, but it has to come after the table is created.
alter table user_feeds alter column user_id set default auth.uid();

-- Customer reviews, shown as testimonials on the marketing page.
--
-- Nothing appears publicly until approved is flipped to true by hand
-- (Supabase -> Table editor -> reviews). This is the one table the whole
-- internet can read, so it defaults to false and stays there until a human
-- decides otherwise -- a public site that renders whatever a stranger typed
-- is a liability, not a feature.
--
-- One review per user (unique on user_id) so submitting again edits your
-- existing one rather than stacking duplicates. Approval is deliberately
-- reset on edit -- see the trigger below -- so approved text cannot be
-- swapped for something else after the fact.
create table if not exists reviews (
  id bigint generated always as identity primary key,
  user_id uuid unique default auth.uid() references auth.users(id) on delete cascade,
  rating int not null check (rating between 1 and 5),
  quote text default '',
  display_name text default '',
  company text default '',
  approved boolean not null default false,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
alter table reviews enable row level security;

-- Read: anyone at all, signed in or not, may read APPROVED reviews -- the
-- marketing page is a logged-out visitor. Separate policies are OR'd, so a
-- signed-in user can additionally see their own while it waits for approval.
drop policy if exists "Anyone can read approved reviews" on reviews;
create policy "Anyone can read approved reviews" on reviews
  for select using (approved = true);
drop policy if exists "Users can read their own review" on reviews;
create policy "Users can read their own review" on reviews
  for select using (auth.uid() = user_id);

-- Write: only your own row, and only ever for yourself.
drop policy if exists "Users insert their own review" on reviews;
create policy "Users insert their own review" on reviews
  for insert with check (auth.uid() = user_id);
drop policy if exists "Users update their own review" on reviews;
create policy "Users update their own review" on reviews
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- Editing a review sends it back for approval. Without this, a user could
-- get a bland review approved and then rewrite it into anything at all,
-- with the change appearing on the marketing site immediately. The RLS
-- policy above cannot express "you may update every column except this
-- one", so it is enforced here instead.
create or replace function reviews_reset_approval() returns trigger
  language plpgsql security definer set search_path = public as $$
begin
  if new.quote is distinct from old.quote
     or new.rating is distinct from old.rating
     or new.display_name is distinct from old.display_name
     or new.company is distinct from old.company then
    new.approved := false;
  end if;
  new.updated_at := now();
  return new;
end $$;
drop trigger if exists reviews_reset_approval_trg on reviews;
create trigger reviews_reset_approval_trg before update on reviews
  for each row execute function reviews_reset_approval();

-- Onboarding, remembered against the ACCOUNT rather than the browser.
--
-- The two setup questions were marked done in localStorage only. On iOS the
-- installed Home Screen app and Safari keep separate storage, so the same
-- person was asked in each one and reported that the app asks every time; a
-- new phone or a cleared cache did the same. "create table if not exists"
-- above never alters an existing table, so this is a separate ALTER.
--
-- The app degrades without it: accountHasOnboarded() falls back to "does this
-- account have any company details filled in", which covers everyone who
-- answered the second question but not someone who skipped both.
alter table company_profiles
  add column if not exists onboarded boolean default false;
