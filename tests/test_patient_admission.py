"""Admission request validation, authorization, and conflict handling."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from postgrest.exceptions import APIError

from app import create_app
from app.auth.middleware import CurrentUser

UNIT = '00000000-0000-4000-8000-000000000001'
BED = '00000000-0000-4000-8000-000000000002'

@pytest.fixture
def admission():
    identity = {'role': 'nurse'}
    def profile(*args):
        return CurrentUser('nurse', 'demo@example.com', 'org', identity['role'], UNIT, 'Demo Nurse')
    with patch('app.auth.middleware.verify_token', return_value={'sub': 'nurse'}), \
         patch('app.auth.middleware._load_profile', side_effect=profile), \
         patch('app.api.patients.supabase') as database:
        client = create_app().test_client()
        client.environ_base['HTTP_AUTHORIZATION'] = 'Bearer test'
        database.return_value.rpc.return_value.execute.return_value = SimpleNamespace(data={'id': 'new-patient'})
        yield client, database, identity

@pytest.mark.parametrize('role', ['nurse', 'charge_nurse', 'admin'])
def test_admission_uses_server_identity_and_trimmed_name(admission, role):
    client, database, identity = admission
    identity['role'] = role
    response = client.post('/api/v1/patients', json={'display_name': '  New Patient  ', 'unit_id': UNIT, 'bed_id': BED})
    assert response.status_code == 201
    assert response.get_json()['patient']['id'] == 'new-patient'
    database.return_value.rpc.assert_called_once_with('admit_patient', {
        'p_org': 'org', 'p_actor': 'nurse', 'p_name': 'New Patient', 'p_unit': UNIT, 'p_bed': BED,
    })

@pytest.mark.parametrize('extra', [{'display_name': '   '}, {'unit_id': 'invalid'}, {'bed_id': 'invalid'}, {'org_id': 'other-org'}, {'nurse_id': 'other-nurse'}])
def test_invalid_or_spoofed_admission_is_rejected(admission, extra):
    client, database, _ = admission
    response = client.post('/api/v1/patients', json={'display_name': 'Patient', 'unit_id': UNIT, **extra})
    assert response.status_code == 422
    database.return_value.rpc.assert_not_called()

def test_patient_can_be_added_without_bed(admission):
    client, database, _ = admission
    assert client.post('/api/v1/patients', json={'display_name': 'Patient', 'unit_id': UNIT}).status_code == 201
    assert database.return_value.rpc.call_args.args[1]['p_bed'] is None

def test_prescriber_cannot_admit(admission):
    client, database, identity = admission
    identity['role'] = 'prescriber'
    assert client.post('/api/v1/patients', json={'display_name': 'Patient', 'unit_id': UNIT}).status_code == 403
    database.return_value.rpc.assert_not_called()

@pytest.mark.parametrize('code, status', [('23505', 409), ('P0002', 404), ('42501', 403)])
def test_admission_reports_occupied_and_foreign_beds(admission, code, status):
    client, database, _ = admission
    database.return_value.rpc.return_value.execute.side_effect = APIError({'code': code, 'message': 'Unavailable', 'details': None, 'hint': None})
    assert client.post('/api/v1/patients', json={'display_name': 'Patient', 'unit_id': UNIT, 'bed_id': BED}).status_code == status
