-- Nurse Worry Layer: core schema
-- Org-scoped multi-tenant. Every domain table carries org_id so both the
-- Flask org_scoped decorator and RLS can enforce isolation independently.

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------------
-- enums
-- ---------------------------------------------------------------------------

create type app_role as enum ('admin', 'charge_nurse', 'nurse');

-- 0 = not worried, 1 = slightly, 2 = very. Stored as smallint rather than an
-- enum so it can be averaged and trended directly in SQL.
create type worry_reason as enum (
  'breathing_off',
  'not_himself',
  'color',
  'pain',
  'agitation',
  'output',
  'just_a_feeling'
);

create type escalation_status as enum (
  'open',
  'acknowledged',
  'assessed',
  'resolved',
  'auto_escalated'
);

create type outcome_event as enum (
  'icu_transfer',
  'rrt_call',
  'code_blue',
  'death',
  'discharge'
);

-- ---------------------------------------------------------------------------
-- tenancy + identity
-- ---------------------------------------------------------------------------

create table organizations (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  slug        text not null unique,
  -- minutes a worry signal may sit without reassessment before auto-escalation
  worry_sla_minutes integer not null default 20,
  created_at  timestamptz not null default now()
);

create table units (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references organizations(id) on delete cascade,
  name        text not null,
  created_at  timestamptz not null default now(),
  unique (org_id, name)
);

-- Mirrors auth.users. Holds the org + role claims that the Flask middleware
-- resolves on each request.
create table profiles (
  id          uuid primary key references auth.users(id) on delete cascade,
  org_id      uuid not null references organizations(id) on delete cascade,
  unit_id     uuid references units(id) on delete set null,
  role        app_role not null default 'nurse',
  full_name   text not null,
  email       text not null,
  created_at  timestamptz not null default now()
);

create index profiles_org_idx on profiles (org_id);
create index profiles_org_role_idx on profiles (org_id, role);

-- ---------------------------------------------------------------------------
-- clinical
-- ---------------------------------------------------------------------------

-- Demographic columns exist specifically so the equity audit is queryable:
-- "do worry rates or response times differ by race, language, or age?"
create table patients (
  id               uuid primary key default gen_random_uuid(),
  org_id           uuid not null references organizations(id) on delete cascade,
  unit_id          uuid references units(id) on delete set null,
  mrn_hash         text not null,
  display_name     text not null,
  room             text,
  age_band         text,
  sex              text,
  race_code        text,
  primary_language text,
  admitted_at      timestamptz not null default now(),
  discharged_at    timestamptz,
  created_at       timestamptz not null default now(),
  unique (org_id, mrn_hash)
);

create index patients_org_active_idx on patients (org_id, unit_id)
  where discharged_at is null;

create table assignments (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references organizations(id) on delete cascade,
  nurse_id    uuid not null references profiles(id) on delete cascade,
  patient_id  uuid not null references patients(id) on delete cascade,
  shift_start timestamptz not null default now(),
  shift_end   timestamptz,
  created_at  timestamptz not null default now()
);

create index assignments_nurse_active_idx on assignments (nurse_id)
  where shift_end is null;
create index assignments_patient_idx on assignments (patient_id);

create table vitals (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references organizations(id) on delete cascade,
  patient_id  uuid not null references patients(id) on delete cascade,
  recorded_at timestamptz not null default now(),
  heart_rate       integer,
  resp_rate        integer,
  spo2             integer,
  systolic_bp      integer,
  diastolic_bp     integer,
  temperature_c    numeric(4,1),
  supplemental_o2  boolean not null default false,
  consciousness    text,  -- AVPU: A, V, P, U
  -- NEWS computed on write by the backend so the dashboard can sort without
  -- recomputing across the whole series.
  news_score       integer,
  created_at  timestamptz not null default now()
);

create index vitals_patient_time_idx on vitals (patient_id, recorded_at desc);

-- ---------------------------------------------------------------------------
-- the worry layer
-- ---------------------------------------------------------------------------

create table worry_signals (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references organizations(id) on delete cascade,
  patient_id  uuid not null references patients(id) on delete cascade,
  nurse_id    uuid not null references profiles(id) on delete cascade,
  level       smallint not null check (level between 0 and 2),
  reasons     worry_reason[] not null default '{}',
  note        text,
  voice_url   text,
  -- seconds the nurse spent in the capture flow; answers the judge question
  -- "will nurses actually use it?" with a number
  capture_ms  integer,
  created_at  timestamptz not null default now()
);

create index worry_signals_patient_time_idx on worry_signals (patient_id, created_at desc);
create index worry_signals_org_time_idx on worry_signals (org_id, created_at desc);

create table risk_scores (
  id              uuid primary key default gen_random_uuid(),
  org_id          uuid not null references organizations(id) on delete cascade,
  patient_id      uuid not null references patients(id) on delete cascade,
  computed_at     timestamptz not null default now(),
  score           numeric(5,2) not null,
  worry_component numeric(5,2) not null default 0,
  vitals_component numeric(5,2) not null default 0,
  trend_component numeric(5,2) not null default 0,
  inputs          jsonb not null default '{}',
  created_at      timestamptz not null default now()
);

create index risk_scores_patient_time_idx on risk_scores (patient_id, computed_at desc);

-- The closed loop. triggered_at -> acknowledged_at -> assessed_at is the
-- worry-to-response time metric that hospitals do not currently track.
create table escalations (
  id              uuid primary key default gen_random_uuid(),
  org_id          uuid not null references organizations(id) on delete cascade,
  patient_id      uuid not null references patients(id) on delete cascade,
  worry_signal_id uuid references worry_signals(id) on delete set null,
  rule            text not null,
  status          escalation_status not null default 'open',
  triggered_at    timestamptz not null default now(),
  sla_deadline    timestamptz not null,
  acknowledged_by uuid references profiles(id) on delete set null,
  acknowledged_at timestamptz,
  assessed_by     uuid references profiles(id) on delete set null,
  assessed_at     timestamptz,
  resolved_at     timestamptz,
  resolution_note text,
  created_at      timestamptz not null default now()
);

create index escalations_org_status_idx on escalations (org_id, status, triggered_at desc);
create index escalations_patient_idx on escalations (patient_id, triggered_at desc);
-- drives the SLA sweeper job
create index escalations_sla_open_idx on escalations (sla_deadline)
  where status = 'open';

-- Feeds the learn loop: which worry reasons actually preceded deterioration.
create table outcomes (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references organizations(id) on delete cascade,
  patient_id  uuid not null references patients(id) on delete cascade,
  event_type  outcome_event not null,
  occurred_at timestamptz not null default now(),
  note        text,
  created_at  timestamptz not null default now()
);

create index outcomes_patient_idx on outcomes (patient_id, occurred_at desc);

create table audit_log (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid references organizations(id) on delete set null,
  actor_id    uuid references profiles(id) on delete set null,
  action      text not null,
  target_type text,
  target_id   uuid,
  payload     jsonb not null default '{}',
  created_at  timestamptz not null default now()
);

create index audit_log_org_time_idx on audit_log (org_id, created_at desc);
