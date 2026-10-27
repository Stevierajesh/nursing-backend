"""Populate nurse notes and patient history for the demo patients.

Why this is a separate, opt-in script rather than part of seed.py
-----------------------------------------------------------------
Everything written here is **fabricated clinical detail**. The rest of the seed
is synthetic too, but vitals and NEWS scores are numbers a reader will treat as
numbers; a sentence like "COPD with two exacerbations this year" reads as a
clinical fact somebody recorded. That is a different kind of made-up, so it
gets its own command, its own warning, and its own revert.

The content for Bed 12 (Robert Ellis) is transcribed from page 30 of
`Project SALUSUPDATED.pdf` -- COPD plus hypertension, an inhaler, one prior
admission -- so the approval review matches the design doc during a demo
instead of rendering "Not recorded" four times.

Run it deliberately:

    python scripts/seed_history.py            # write
    python scripts/seed_history.py --revert    # put it back to not-recorded

Not idempotent-by-accident: it overwrites whatever is in those columns for the
named patients, and touches nobody else.

Do not present any of this as observed clinical data. It exists so the eight
section ticks in the approval review have something to be about.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.extensions import supabase  # noqa: E402

ORG_SLUG = "mercy-general"

# Keyed by display_name. Every string here is invented.
HISTORY: dict[str, dict[str, str]] = {
    "Robert Ellis": {
        "past_history": (
            "COPD, diagnosed 2019. Hypertension, diagnosed 2016. Ex-smoker, "
            "stopped 2020 after roughly 30 pack-years."
        ),
        "home_medications": (
            "Salbutamol inhaler as needed. Tiotropium inhaler once daily. "
            "Amlodipine 5 mg once daily."
        ),
        "prior_admissions": (
            "One prior admission, March 2026, for a COPD exacerbation. Four "
            "days on the ward, no ICU transfer."
        ),
        "past_responses": (
            "Responded well to nebulised bronchodilators and a short oral "
            "steroid course during the March admission. No documented adverse "
            "drug reactions."
        ),
    },
    "Yolanda Price": {
        "past_history": "No chronic respiratory or cardiac history recorded.",
        "home_medications": "Paracetamol as needed. No regular prescriptions.",
        "prior_admissions": "No prior admissions to this hospital.",
        "past_responses": (
            "Tolerated post-operative analgesia without nausea during this "
            "admission."
        ),
    },
}

# (display_name, author_role, note). The notes are the kind of thing section 4
# exists to carry -- observation in the nurse's own words, not a vitals reading
# restated.
NOTES: list[tuple[str, str, str]] = [
    (
        "Robert Ellis",
        "nurse",
        "Breathing looks harder than this morning. Using accessory muscles, "
        "pausing mid-sentence. Sats are still reading 94 but he does not look "
        "like someone sitting at 94.",
    ),
    (
        "Robert Ellis",
        "nurse",
        "Sat him more upright and he settled a little. Still not right. Low "
        "threshold to call if the respiratory rate climbs again.",
    ),
    (
        "Robert Ellis",
        "charge_nurse",
        "Reviewed at the bedside with Maria. Agree he is working harder. "
        "Watching hourly.",
    ),
    (
        "Yolanda Price",
        "nurse",
        "Comfortable, mobilising to the chair unaided. Wound site clean and "
        "dry. No concerns this shift.",
    ),
]


def _org_id() -> str:
    rows = supabase().table("organizations").select("id").eq("slug", ORG_SLUG).execute().data or []
    if not rows:
        sys.exit(f"Organization '{ORG_SLUG}' not found. Run scripts/seed.py first.")
    return rows[0]["id"]


def _patients(org_id: str) -> dict[str, str]:
    names = sorted(set(HISTORY) | {name for name, _, _ in NOTES})
    rows = (
        supabase().table("patients").select("id, display_name")
        .eq("org_id", org_id).in_("display_name", names).execute().data
    ) or []
    found = {r["display_name"]: r["id"] for r in rows}
    for name in names:
        if name not in found:
            print(f"  ! {name} not found in {ORG_SLUG}; skipping")
    return found


def _author_ids(org_id: str) -> dict[str, str]:
    rows = (
        supabase().table("profiles").select("id, role")
        .eq("org_id", org_id).execute().data
    ) or []
    out: dict[str, str] = {}
    for row in rows:
        out.setdefault(row["role"], row["id"])
    return out


def write() -> None:
    org_id = _org_id()
    patients = _patients(org_id)
    authors = _author_ids(org_id)

    for name, fields in HISTORY.items():
        if name not in patients:
            continue
        supabase().table("patients").update(fields).eq("id", patients[name]).execute()
        print(f"  + history for {name}")

    for name, role, body in NOTES:
        if name not in patients:
            continue
        author_id = authors.get(role) or authors.get("nurse")
        if not author_id:
            print(f"  ! no {role} profile to author a note; skipping {name}")
            continue
        supabase().table("nurse_notes").insert({
            "org_id": org_id, "patient_id": patients[name],
            "author_id": author_id, "body": body,
        }).execute()
        print(f"  + note for {name} ({role})")

    print("\nFabricated demo content. Do not present it as observed data.")
    print("Revert with: python scripts/seed_history.py --revert")


def revert() -> None:
    org_id = _org_id()
    patients = _patients(org_id)
    if not patients:
        return

    ids = list(patients.values())
    # Back to NULL, not to empty strings: "not recorded" is the honest state
    # for a field nobody actually took.
    supabase().table("patients").update({
        "past_history": None, "home_medications": None,
        "prior_admissions": None, "past_responses": None,
    }).in_("id", ids).execute()
    print(f"  - cleared history for {len(ids)} patient(s)")

    # Only the notes this script wrote: matched on exact body, so a real note
    # charted through the app during a demo is not deleted along with them.
    bodies = [body for _, _, body in NOTES]
    supabase().table("nurse_notes").delete().eq("org_id", org_id).in_("body", bodies).execute()
    print(f"  - deleted up to {len(bodies)} seeded note(s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revert", action="store_true", help="clear what this script wrote")
    args = parser.parse_args()
    print("Reverting demo history…" if args.revert else "Writing demo history…")
    revert() if args.revert else write()
