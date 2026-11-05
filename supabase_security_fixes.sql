-- Fixes for what Supabase's Security Advisor flags. Run once in the SQL editor
-- (Supabase dashboard -> SQL Editor -> New query -> paste -> Run). Safe to run
-- again: every statement is idempotent.
--
-- supabase_kv_schema.sql and supabase_sync_schema.sql carry the same changes,
-- so a new project set up from them starts out fixed.

-- 1. "Function Search Path Mutable: public.kv_store_touch"
--    Pin the function to no search path, so a table someone creates with the
--    same name in another schema can never be picked up instead. It only
--    touches NEW, so it needs nothing from any schema.
alter function public.kv_store_touch() set search_path = '';

-- 2. "Public Bucket Allows Listing: storage.avatars"
--    The bucket is public, so profile photos load from their public links
--    with no policy at all. This policy additionally let anyone LIST every
--    file in the bucket, which exposes every user's id (the folder names).
--    Users can still read, upload and replace their own photo through
--    "Users manage their own avatar".
drop policy if exists "Public read access for avatars" on storage.objects;

-- 3. "Public / Signed-In Users Can Execute SECURITY DEFINER Function:
--    public.reviews_reset_approval()"
--    It's a trigger function: it only ever needs to run from its trigger, and
--    a trigger runs whatever the caller's privileges. Nobody should call it
--    directly.
revoke execute on function public.reviews_reset_approval() from public;
revoke execute on function public.reviews_reset_approval() from anon;
revoke execute on function public.reviews_reset_approval() from authenticated;

-- Left as they are on purpose:
--
-- * "RLS Enabled No Policy" on public.kv_store and public.concrete_quotes.
--   Only the server (service role) reads and writes these. No policy means
--   the key in the web app can't touch them, which is the point.
--
-- * "Leaked Password Protection Disabled" is a setting, not SQL:
--   Authentication -> Sign In / Providers -> Email -> "Prevent use of leaked
--   passwords". It needs a paid Supabase plan.
