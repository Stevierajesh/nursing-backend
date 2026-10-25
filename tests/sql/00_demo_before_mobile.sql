-- Representative pre-existing synthetic project data before migration 010.
insert into organizations (id,name,slug) values
 ('99999999-0000-0000-0000-000000000001','Demo hospital','mercy-general');
insert into units (id,org_id,name) values
 ('99999999-1111-0000-0000-000000000001','99999999-0000-0000-0000-000000000001','4 West');
insert into patients (id,org_id,unit_id,mrn_hash,display_name,room) values
 ('99999999-2222-0000-0000-000000000001','99999999-0000-0000-0000-000000000001','99999999-1111-0000-0000-000000000001','demo-robert','Robert Ellis','412');
