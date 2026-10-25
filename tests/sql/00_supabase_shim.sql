-- Minimal Supabase environment shim, so the real migrations can be executed
-- unmodified against a local Postgres 16 and verified.
-- This file is a TEST HARNESS, not part of the project.

create schema if not exists auth;

-- Supabase's auth.users, reduced to what the migrations reference.
create table if not exists auth.users (
  id    uuid primary key default gen_random_uuid(),
  email text unique
);

-- Roles Supabase provides.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role bypassrls;
  end if;
end $$;

-- auth.uid() reads the request JWT claims in Supabase. Here it reads a GUC we
-- can set per-test, which is enough to exercise the RLS policies.
create or replace function auth.uid()
returns uuid
language sql
stable
as $$
  select nullif(current_setting('test.current_user_id', true), '')::uuid
$$;

-- The realtime publication the RLS migration adds tables to.
do $$
begin
  if not exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    create publication supabase_realtime;
  end if;
end $$;

grant usage on schema auth to authenticated;
grant usage on schema public to authenticated;
grant usage on schema public to service_role;
alter default privileges in schema public grant all on tables to service_role;
