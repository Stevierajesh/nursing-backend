"""Emit the demo seed as plain SQL.

Why SQL rather than the Supabase client: the MCP connection can execute SQL but
has no service-role key, and `seed.py` needs one. This script generates the
same dataset as pure INSERTs so it can be applied over any SQL channel.

NEWS scores are computed here by importing `app.services.news`, so the seeded
values come from the same implementation the API uses. Writing a second NEWS
calculation in SQL would let the two silently diverge, which is exactly the
bug you least want under a demo about scoring.

Auth users are NOT created here -- they must already exist in auth.users, and
profiles are matched to them by email.

    python scripts/gen_seed_sql.py > /tmp/seed.sql
"""

from __future__ import annotations

import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.news import news_score  # noqa: E402

random.seed(42)  # reproducible demos

EMAIL_DOMAIN = "mercygeneral.example"
ORG_SLUG = "mercy-general"

NOW = datetime.now(timezone.utc)


def ts(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def q(value) -> str:
    """Quote a value for SQL, mapping None to NULL."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# people
# ---------------------------------------------------------------------------

USERS = [
    ("admin",  "Dana Whitmore",      "admin",        None),
    ("charge", "Priya Raghunathan",  "charge_nurse", 0),
    ("maria",  "Maria Santos",       "nurse",        0),
    ("james",  "James Okonkwo",      "nurse",        0),
    ("lin",    "Lin Zhao",           "nurse",        1),
]

UNITS = ["4 West — Med/Surg", "6 North — Telemetry"]

# ---------------------------------------------------------------------------
# vitals scenarios
# ---------------------------------------------------------------------------

# The hero case. Each observation is individually unremarkable -- NEWS peaks at
# 4, below the medium-response band -- but the trajectory is unmistakable. This
# is precisely the patient a nurse notices and the score does not.
ROBERT_DRIFT = [
    (360, {"resp_rate": 16, "spo2": 98, "heart_rate": 72}),
    (300, {"resp_rate": 17, "spo2": 97, "heart_rate": 78}),
    (240, {"resp_rate": 18, "spo2": 96, "heart_rate": 84}),
    (180, {"resp_rate": 19, "spo2": 96, "heart_rate": 88}),
    (120, {"resp_rate": 20, "spo2": 95, "heart_rate": 94}),
    (60,  {"resp_rate": 21, "spo2": 95, "heart_rate": 98}),
    (15,  {"resp_rate": 22, "spo2": 94, "heart_rate": 104}),
]

# Flat and boring. Worry on this patient must not page anyone.
STABLE = [(m, {}) for m in (330, 270, 210, 150, 90, 30)]

# Objectively bad right now, nobody has tapped worry.
HENRY_CRASH = [
    (300, {"resp_rate": 18, "spo2": 96}),
    (240, {"resp_rate": 20, "spo2": 95, "heart_rate": 96}),
    (180, {"resp_rate": 22, "spo2": 94, "heart_rate": 108, "systolic_bp": 108}),
    (120, {"resp_rate": 24, "spo2": 92, "heart_rate": 118, "systolic_bp": 98,
           "supplemental_o2": True}),
    (20,  {"resp_rate": 26, "spo2": 91, "heart_rate": 124, "systolic_bp": 92,
           "supplemental_o2": True, "temperature_c": 38.4}),
]

MILD = [
    (300, {}),
    (240, {"resp_rate": 18}),
    (180, {"resp_rate": 19, "heart_rate": 92}),
    (60,  {"resp_rate": 20, "heart_rate": 95}),
    (20,  {"resp_rate": 20, "heart_rate": 96}),
]

PATIENTS = [
    dict(key="p1", name="Robert Ellis", mrn="mrn-hash-0001", room="412",
         age="65-74", sex="M", race="white", lang="English", los=3,
         series=ROBERT_DRIFT, unit=0, nurse="maria"),
    dict(key="p2", name="Yolanda Price", mrn="mrn-hash-0002", room="408",
         age="45-54", sex="F", race="black", lang="English", los=1,
         series=STABLE, unit=0, nurse="maria"),
    dict(key="p3", name="Henry Okafor", mrn="mrn-hash-0003", room="415",
         age="75-84", sex="M", race="black", lang="English", los=5,
         series=HENRY_CRASH, unit=0, nurse="james"),
    dict(key="p4", name="Mei-Ling Chen", mrn="mrn-hash-0004", room="403",
         age="55-64", sex="F", race="asian", lang="Mandarin", los=2,
         series=MILD, unit=0, nurse="maria"),
    dict(key="p5", name="Carlos Represa", mrn="mrn-hash-0005", room="410",
         age="35-44", sex="M", race="hispanic", lang="Spanish", los=1,
         series=STABLE, unit=0, nurse="james"),
    dict(key="p6", name="Dorothy Whitfield", mrn="mrn-hash-0006", room="601",
         age="85+", sex="F", race="white", lang="English", los=7,
         series=MILD, unit=1, nurse="lin"),
    dict(key="p7", name="Aisha Rahman", mrn="mrn-hash-0007", room="604",
         age="25-34", sex="F", race="asian", lang="Urdu", los=1,
         series=STABLE, unit=1, nurse="lin"),
    dict(key="p8", name="Frank Delacroix", mrn="mrn-hash-0008", room="607",
         age="65-74", sex="M", race="white", lang="English", los=4,
         series=MILD, unit=1, nurse="lin"),
]

REASONS = ["breathing_off", "not_himself", "color", "pain", "agitation",
           "output", "just_a_feeling"]

BASE_VITALS = {
    "heart_rate": 74,
    "resp_rate": 16,
    "spo2": 98,
    "systolic_bp": 122,
    "diastolic_bp": 76,
    "temperature_c": 36.7,
    "supplemental_o2": False,
    "consciousness": "A",
}


def emit() -> None:
    out = print

    out("-- Nurse Worry Layer demo seed. Generated by scripts/gen_seed_sql.py.")
    out("-- NEWS scores computed by app/services/news.py -- single source of truth.")
    out("-- Idempotent: deletes the demo org first (domain tables cascade).")
    out("")
    out("begin;")
    out("")
    out(f"delete from organizations where slug = {q(ORG_SLUG)};")
    out("")

    # --- org + units -------------------------------------------------------
    out("with org as (")
    out("  insert into organizations (name, slug, worry_sla_minutes)")
    out(f"  values ('Mercy General Hospital', {q(ORG_SLUG)}, 20)")
    out("  returning id")
    out("), u as (")
    out("  insert into units (org_id, name)")
    out("  select org.id, v.name from org, (values")
    out(",\n".join(f"    ({q(n)})" for n in UNITS))
    out("  ) as v(name)")
    out("  returning id, name")
    out(")")
    out("insert into profiles (id, org_id, unit_id, role, full_name, email)")
    out("select au.id, org.id, u.id, p.role::app_role, p.full_name, au.email")
    out("from org, (values")
    rows = []
    for handle, full_name, role, unit_idx in USERS:
        unit_name = q(UNITS[unit_idx]) if unit_idx is not None else "null"
        rows.append(
            f"  ({q(handle + '@' + EMAIL_DOMAIN)}, {q(full_name)}, "
            f"{q(role)}, {unit_name})"
        )
    out(",\n".join(rows))
    out(") as p(email, full_name, role, unit_name)")
    out("join auth.users au on au.email = p.email")
    out("left join u on u.name = p.unit_name")
    out("cross join (select 1) _;")
    out("")

    # org_id is referenced via a scalar subquery rather than a temp table.
    # `from _ids i, (values ...) left join x on x.org_id = (select id from organizations where slug = 'mercy-general')` is a
    # scoping error in Postgres: the LEFT JOIN binds only to the comma's right
    # operand, so `i` is not visible in the ON clause.

    # --- patients ----------------------------------------------------------
    out("insert into patients (org_id, unit_id, mrn_hash, display_name, room,")
    out("                      age_band, sex, race_code, primary_language, admitted_at)")
    out("select (select id from organizations where slug = 'mercy-general'), un.id, v.mrn, v.name, v.room, v.age, v.sex, v.race,")
    out("       v.lang, v.admitted::timestamptz")
    out("from (values")
    rows = []
    for p in PATIENTS:
        rows.append(
            f"  ({q(p['mrn'])}, {q(p['name'])}, {q(p['room'])}, {q(p['age'])}, "
            f"{q(p['sex'])}, {q(p['race'])}, {q(p['lang'])}, "
            f"{q(ts(p['los'] * 1440))}, {q(UNITS[p['unit']])})"
        )
    out(",\n".join(rows))
    out(") as v(mrn, name, room, age, sex, race, lang, admitted, unit_name)")
    out("left join units un on un.name = v.unit_name and un.org_id = (select id from organizations where slug = 'mercy-general');")
    out("")

    out("update patients set bed = case room when '412' then '12' when '408' then '8' when '415' then '15' when '403' then '3' when '410' then '10' when '601' then '1' when '604' then '4' when '607' then '7' end where org_id=(select id from organizations where slug='mercy-general');")
    out("insert into unit_beds (org_id,unit_id,room,bed) select org_id,unit_id,room,bed from patients where bed is not null and org_id=(select id from organizations where slug='mercy-general') on conflict (org_id,unit_id,bed) do nothing;")

    # --- vitals ------------------------------------------------------------
    out("-- vitals, with news_score precomputed by app/services/news.py")
    out("insert into vitals (org_id, patient_id, recorded_at, heart_rate, resp_rate,")
    out("                    spo2, systolic_bp, diastolic_bp, temperature_c,")
    out("                    supplemental_o2, consciousness, news_score)")
    out("select (select id from organizations where slug = 'mercy-general'), pt.id, v.recorded_at::timestamptz, v.hr, v.rr, v.spo2,")
    out("       v.sbp, v.dbp, v.temp, v.o2, v.loc, v.news")
    out("from (values")
    rows = []
    for p in PATIENTS:
        for minutes_ago, overrides in p["series"]:
            vit = dict(BASE_VITALS)
            vit.update(overrides)
            score = news_score(vit)
            rows.append(
                f"  ({q(p['mrn'])}, {q(ts(minutes_ago))}, {q(vit['heart_rate'])}, "
                f"{q(vit['resp_rate'])}, {q(vit['spo2'])}, {q(vit['systolic_bp'])}, "
                f"{q(vit['diastolic_bp'])}, {q(vit['temperature_c'])}, "
                f"{q(vit['supplemental_o2'])}, {q(vit['consciousness'])}, {score})"
            )
    out(",\n".join(rows))
    out(") as v(mrn, recorded_at, hr, rr, spo2, sbp, dbp, temp, o2, loc, news)")
    out("join patients pt on pt.mrn_hash = v.mrn and pt.org_id = (select id from organizations where slug = 'mercy-general');")
    out("")

    # --- assignments -------------------------------------------------------
    out("insert into assignments (org_id, nurse_id, patient_id)")
    out("select (select id from organizations where slug = 'mercy-general'), pr.id, pt.id")
    out("from (values")
    rows = [
        f"  ({q(p['mrn'])}, {q(p['nurse'] + '@' + EMAIL_DOMAIN)})"
        for p in PATIENTS
    ]
    out(",\n".join(rows))
    out(") as v(mrn, nurse_email)")
    out("join patients pt on pt.mrn_hash = v.mrn and pt.org_id = (select id from organizations where slug = 'mercy-general')")
    out("join profiles pr on pr.email = v.nurse_email and pr.org_id = (select id from organizations where slug = 'mercy-general');")
    out("")

    # --- two weeks of history ---------------------------------------------
    # The response-time spread is intentionally uneven across patient language
    # so the equity audit surfaces a real disparity. A demo where the audit
    # finds nothing teaches the judges nothing -- but this is fabricated, and
    # the README says so.
    out("-- Two weeks of resolved escalations, so the metrics and equity")
    out("-- screens are not empty. Response times are deliberately skewed by")
    out("-- patient language; see README 'Data honesty'.")
    # worry_signal_id is generated here rather than by the database so the
    # escalation rows can reference it directly. An earlier version joined
    # worry signals to escalations on triggered_at equality, which silently
    # fans out into a cartesian product the moment two signals share a
    # timestamp -- a latent bug that only shows up on an unlucky reseed.
    out("create temporary table _hist (")
    out("  worry_id uuid, mrn text, nurse_email text, level smallint, reasons text,")
    out("  triggered timestamptz, ack timestamptz, assessed timestamptz,")
    out("  rule text, status text, breached boolean, outcome_at timestamptz,")
    out("  outcome_type text")
    out(");")
    out("")

    lang_by_mrn = {p["mrn"]: p["lang"] for p in PATIENTS}
    nurse_handles = ["maria", "james", "lin"]
    hist_rows = []

    for day in range(1, 15):
        for _ in range(random.randint(1, 3)):
            p = random.choice(PATIENTS)
            nurse = random.choice(nurse_handles)
            level = random.choice([1, 2, 3, 4, 4])
            trig_min = day * 1440 + random.randint(0, 1200)

            lang = lang_by_mrn[p["mrn"]]
            base_ack = 6 if lang == "English" else 13
            ack_minutes = max(1, int(random.gauss(base_ack, 5)))
            breached = ack_minutes > 20
            assess_minutes = ack_minutes + random.randint(2, 12)

            reasons = random.sample(REASONS, random.randint(1, 2))
            reasons_literal = "{" + ",".join(reasons) + "}"

            outcome_at = None
            outcome_type = None
            if random.random() < 0.22:
                outcome_at = ts(trig_min - random.randint(120, 1200))
                outcome_type = random.choice(
                    ["icu_transfer", "rrt_call", "rrt_call", "code_blue"]
                )

            hist_rows.append(
                f"  ({q(str(uuid.UUID(int=random.getrandbits(128), version=4)))}, "
                f"{q(p['mrn'])}, {q(nurse + '@' + EMAIL_DOMAIN)}, {level}, "
                f"{q(reasons_literal)}, {q(ts(trig_min))}, "
                f"{q(ts(trig_min - ack_minutes))}, "
                f"{q(ts(trig_min - assess_minutes))}, "
                f"{q(random.choice(['very_worried_plus_drift', 'very_worried_plus_news', 'worried_plus_drift_and_news', 'news_high']))}, "
                f"{q('auto_escalated' if breached else 'resolved')}, "
                f"{q(breached)}, {q(outcome_at)}, {q(outcome_type)})"
            )

    out("insert into _hist values")
    out(",\n".join(hist_rows) + ";")
    out("")

    out("-- worry signals carry the id generated above, so escalations can")
    out("-- reference them without joining on a timestamp.")
    out("insert into worry_signals (id, org_id, patient_id, nurse_id, level, reasons,")
    out("                           capture_ms, created_at)")
    out("select h.worry_id, (select id from organizations where slug = 'mercy-general'), pt.id, pr.id, h.level, h.reasons::worry_reason[],")
    out("       1400 + (random() * 2800)::int, h.triggered")
    out("from _hist h")
    out("join patients pt on pt.mrn_hash = h.mrn and pt.org_id = (select id from organizations where slug = 'mercy-general')")
    out("join profiles pr on pr.email = h.nurse_email and pr.org_id = (select id from organizations where slug = 'mercy-general');")
    out("")
    out("insert into escalations (org_id, patient_id, worry_signal_id, rule, status,")
    out("                         triggered_at, sla_deadline, acknowledged_by,")
    out("                         acknowledged_at, assessed_by, assessed_at, resolved_at)")
    out("select (select id from organizations where slug = 'mercy-general'), pt.id, h.worry_id, h.rule, h.status::escalation_status,")
    out("       h.triggered, h.triggered + interval '20 minutes',")
    out("       charge.id, h.ack, charge.id, h.assessed,")
    out("       h.assessed + interval '20 minutes'")
    out("from _hist h")
    out("join patients pt on pt.mrn_hash = h.mrn and pt.org_id = (select id from organizations where slug = 'mercy-general')")
    out(f"join profiles charge on charge.email = {q('charge@' + EMAIL_DOMAIN)}")
    out("                    and charge.org_id = (select id from organizations where slug = 'mercy-general');")
    out("")

    out("insert into outcomes (org_id, patient_id, event_type, occurred_at)")
    out("select (select id from organizations where slug = 'mercy-general'), pt.id, h.outcome_type::outcome_event, h.outcome_at")
    out("from _hist h")
    out("join patients pt on pt.mrn_hash = h.mrn and pt.org_id = (select id from organizations where slug = 'mercy-general')")
    out("where h.outcome_at is not null;")
    out("")

    out("drop table _hist;")
    out("commit;")


if __name__ == "__main__":
    emit()
