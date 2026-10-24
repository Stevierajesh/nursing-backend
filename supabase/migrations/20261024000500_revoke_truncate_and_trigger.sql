-- Revoke TRUNCATE, TRIGGER, and REFERENCES from client roles.
--
-- Found by auditing information_schema.role_table_grants after the previous
-- migration: revoking INSERT/UPDATE/DELETE left TRUNCATE in place, because
-- Supabase's default is `grant all on all tables` to anon and authenticated,
-- and "all" includes more than the three DML verbs.
--
-- TRUNCATE is the dangerous one and the reason this migration exists:
--   * it is NOT filtered by row level security -- policies are irrelevant to
--     it, so none of the org-scoping above would have contained it
--   * it is not a row operation, so no USING clause ever evaluates
--   * `truncate escalations` from a browser console would have destroyed every
--     escalation in every organization
--
-- TRIGGER is revoked because the ability to attach a trigger to a clinical
-- table is an arbitrary-code-execution primitive on write paths.
-- REFERENCES is revoked for tidiness; it lets a role create foreign keys
-- against these tables, which can be used to probe for the existence of rows
-- a policy would otherwise hide.

revoke truncate, trigger, references
  on all tables in schema public
  from authenticated, anon;

-- And stop the defaults from re-granting them on future tables.
alter default privileges in schema public
  revoke truncate, trigger, references on tables from authenticated;
alter default privileges in schema public
  revoke truncate, trigger, references on tables from anon;

-- Sequences: a client has no reason to advance or set them. All inserts are
-- server-side, so USAGE/UPDATE here would only ever be abuse.
revoke all on all sequences in schema public from authenticated, anon;
alter default privileges in schema public
  revoke all on sequences from authenticated;
alter default privileges in schema public
  revoke all on sequences from anon;
