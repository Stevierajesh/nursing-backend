-- Expand the existing two demo floors to 20 rooms and 30 beds total.
-- Existing rooms, beds, and patient assignments are preserved.
insert into public.unit_beds (org_id, unit_id, room, bed)
select u.org_id, u.id, layout.room, layout.bed
from (values
  ('4 West — Med/Surg', '422', '22A'),
  ('4 West — Med/Surg', '422', '22B'),
  ('4 West — Med/Surg', '423', '23A'),
  ('4 West — Med/Surg', '423', '23B'),
  ('4 West — Med/Surg', '424', '24A'),
  ('4 West — Med/Surg', '424', '24B'),
  ('4 West — Med/Surg', '425', '25A'),
  ('4 West — Med/Surg', '425', '25B'),
  ('4 West — Med/Surg', '426', '26A'),
  ('4 West — Med/Surg', '426', '26B'),
  ('4 West — Med/Surg', '427', '27A'),
  ('4 West — Med/Surg', '427', '27B'),
  ('6 North — Telemetry', '609', '9A'),
  ('6 North — Telemetry', '609', '9B'),
  ('6 North — Telemetry', '610', '10A'),
  ('6 North — Telemetry', '610', '10B'),
  ('6 North — Telemetry', '611', '11A'),
  ('6 North — Telemetry', '611', '11B'),
  ('6 North — Telemetry', '612', '12A'),
  ('6 North — Telemetry', '612', '12B')
) as layout(unit_name, room, bed)
join public.units u on u.name = layout.unit_name
join public.organizations o on o.id = u.org_id
where o.slug = 'mercy-general'
on conflict (org_id, unit_id, bed) do nothing;
