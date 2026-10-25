-- Bed identifiers are separate from room numbers; do not infer them.
alter table public.patients add column bed text;
