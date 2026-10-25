"""API permission and lifecycle regression checks; no live database writes."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from postgrest.exceptions import APIError
from app import create_app
from app.auth.middleware import CurrentUser

class DB:
    def __init__(self):
        self.rows={
          'patients':[dict(id='p1',org_id='a',unit_id='u1',bed='12',room='412',discharged_at=None),dict(id='p2',org_id='a',unit_id='u1',bed='7',room='407',discharged_at=None),dict(id='pb',org_id='b')],
          'assignments':[dict(id='a1',org_id='a',patient_id='p1',nurse_id='n1',shift_end=None)],
          'profiles':[dict(id='doc1',org_id='a',role='prescriber'),dict(id='doc2',org_id='a',role='prescriber'),dict(id='docb',org_id='b',role='prescriber')],
          'units':[dict(id='u1',org_id='a')],
          'unit_beds':[dict(id='b12',org_id='a',unit_id='u1',room='412',bed='12'),dict(id='b7',org_id='a',unit_id='u1',room='407',bed='7'),dict(id='b21',org_id='a',unit_id='u1',room='421',bed='21'),dict(id='bb',org_id='b',unit_id='ub',room='501',bed='1')],
          'vitals':[dict(id='v1',patient_id='p1',org_id='a',systolic_bp=150,recorded_at='2026-10-24T12:00:00Z')],
          'escalations':[dict(id='e1',patient_id='p1',org_id='a',status='open',acknowledged_at=None,assessed_at=None),dict(id='e2',patient_id='p2',org_id='a',status='open',acknowledged_at=None,assessed_at=None),dict(id='eb',patient_id='pb',org_id='b',status='open')],
          'organizations':[dict(id='a',medication_sla_minutes=15)],
          'medication_reviews':[],'audit_log':[]}
    def table(self,name):return Query(self,name)
class Query:
    def __init__(self,db,name):self.db=db;self.name=name;self.filters=[];self.mode='select';self.payload={};self.maximum=None
    def select(self,*args,**kw):return self
    def eq(self,k,v):self.filters.append(lambda r:r.get(k)==v);return self
    def is_(self,k,v):self.filters.append(lambda r:r.get(k) is None);return self
    def in_(self,k,values):self.filters.append(lambda r:r.get(k) in values);return self
    def neq(self,k,v):self.filters.append(lambda r:r.get(k)!=v);return self
    def lt(self,k,v):self.filters.append(lambda r:r.get(k) is not None and r.get(k)<v);return self
    def or_(self,expr):
        # Minimal PostgREST or-filter: "col.eq.value,col.eq.value". Only eq is
        # parsed because that is all the app sends; anything else should fail
        # loudly here rather than silently matching every row.
        terms=[]
        for term in expr.split(','):
            col,op,value=term.split('.',2)
            assert op=='eq',f'fake Query.or_ only supports eq, got {op}'
            terms.append((col,value))
        self.filters.append(lambda r:any(r.get(c)==v for c,v in terms));return self
    def order(self,*args,**kw):return self
    def limit(self,n):self.maximum=n;return self
    def insert(self,payload):self.mode='insert';self.payload=deepcopy(payload);return self
    def update(self,payload):self.mode='update';self.payload=deepcopy(payload);return self
    def execute(self):
        rows=[r for r in self.db.rows[self.name] if all(f(r) for f in self.filters)]
        if self.maximum is not None:rows=rows[:self.maximum]
        if self.mode=='insert':
            row={'id':f'{self.name}-{len(self.db.rows[self.name])+1}',**self.payload}
            if self.name=='medication_reviews':row.setdefault('status','pending')
            self.db.rows[self.name].append(row);rows=[row]
        elif self.mode=='update':
            for row in rows:
                p={**row,**self.payload}
                if self.name=='patients' and p.get('bed') and any(o['id']!=row['id'] and o.get('org_id')==p['org_id'] and o.get('unit_id')==p.get('unit_id') and o.get('bed')==p['bed'] and o.get('discharged_at') is None for o in self.db.rows['patients']):
                    raise APIError({'code':'23505','message':'occupied','details':None,'hint':None})
                row.update(self.payload)
        return SimpleNamespace(data=deepcopy(rows))
@pytest.fixture
def env():
    db=DB();identity={'id':'n1','role':'nurse'}
    def user(*args):return CurrentUser(id=identity['id'],role=identity['role'],org_id='a',email='test@example.com',unit_id='u1',full_name='Test')
    with patch('app.auth.middleware.verify_token',return_value={'sub':'test'}),patch('app.auth.middleware._load_profile',side_effect=user),patch('app.auth.middleware.supabase',return_value=db),patch('app.api.mobile.supabase',return_value=db),patch('app.api.escalations.supabase',return_value=db),patch('app.api.patients.supabase',return_value=db),patch('app.services.escalation.supabase',return_value=db),patch('app.services.medication.supabase',return_value=db):
        c=create_app().test_client();c.environ_base['HTTP_AUTHORIZATION']='Bearer test';yield c,db,identity

def send(c,**kw):return c.post('/api/v1/medication/reviews',json={'patient_id':'p1','prescriber_id':'doc1','condition':'blood_pressure','request_note':'Please review current medication',**kw})

def test_inventory_returns_occupancy_without_unassigned_patient_identity(env):
    c,_,_=env;rows=c.get('/api/v1/beds').get_json()['beds']
    assert {r['id']:r['occupied'] for r in rows}=={'b12':True,'b7':True,'b21':False}
    assert all('patient_id' not in r for r in rows)

@pytest.mark.parametrize('action',['acknowledge','assess'])
def test_nurse_response_is_assignment_and_org_scoped(env,action):
    c,_,_=env
    assert c.post('/api/v1/escalations/e2/'+action,json={}).status_code==403
    assert c.post('/api/v1/escalations/eb/'+action,json={}).status_code==404
    r=c.post('/api/v1/escalations/e1/'+action,json={'note':'Checked at bedside'})
    assert r.status_code==200
    assert r.get_json()['escalation']['acknowledged_at']

def test_assessment_retries_preserve_timestamps_and_original_note(env):
    c,_,_=env;url='/api/v1/escalations/e1/assess'
    a=c.post(url,json={'note':'Checked'}).get_json()['escalation'];b=c.post(url,json={'note':'Retry'}).get_json()['escalation']
    assert a['assessed_at']==b['assessed_at'] and b['resolution_note']=='Checked'
    assert c.post(url,json={'assessed_at':'fake'}).status_code==422

@pytest.mark.parametrize('role',['nurse','prescriber'])
def test_bed_editor_is_charge_or_admin_only(env,role):
    c,_,who=env;who['role']=role
    assert c.patch('/api/v1/patients/p1/location',json={'bed_id':'b21'}).status_code==403
    assert c.post('/api/v1/beds',json={'unit_id':'u1','room':'422','bed':'22'}).status_code==403

def test_bed_assignment_rejects_conflicts_and_foreign_ids(env):
    c,_,who=env;who['role']='charge_nurse'
    assert c.patch('/api/v1/patients/p1/location',json={'bed_id':'b7'}).status_code==409
    assert c.patch('/api/v1/patients/p1/location',json={'bed_id':'bb'}).status_code==404
    assert c.patch('/api/v1/patients/pb/location',json={'bed_id':'b21'}).status_code==404
    assert c.patch('/api/v1/patients/p1/location',json={'bed_id':'b21'}).get_json()['patient']['bed']=='21'
    assert c.patch('/api/v1/patients/p1/location',json={}).status_code==422
    assert c.patch('/api/v1/patients/p1/location',json={'bed_id':None}).get_json()['patient']['bed'] is None

def test_request_validates_scope_recipient_and_server_snapshot(env):
    c,_,_=env
    assert send(c,patient_id='p2').status_code==403
    assert send(c,patient_id='pb').status_code==404
    assert send(c,prescriber_id='docb').status_code==404
    assert send(c,request_note='   ').status_code==422
    assert send(c,vitals_snapshot={'systolic_bp':1}).status_code==422
    row=send(c).get_json()['review'];assert row['requested_by']=='n1' and row['vitals_snapshot']['systolic_bp']==150

def test_only_assigned_prescriber_can_complete_and_closed_requests_are_immutable(env):
    c,_,who=env;row=send(c).get_json()['review'];url='/api/v1/medication/reviews/'+row['id']
    assert c.patch(url,json={'action':'review','note':'Reviewed'}).status_code==403
    who.update(id='doc2',role='prescriber')
    assert c.patch(url,json={'action':'review','note':'Reviewed'}).status_code==403
    who['id']='doc1';r=c.patch(url,json={'action':'review','note':'Review completed; follow existing orders'})
    assert r.status_code==200 and r.get_json()['review']['reviewed_by']=='doc1' and r.get_json()['review']['reviewed_at']
    assert c.patch(url,json={'action':'dismiss','note':'Overwrite'}).status_code==409

def test_nurse_can_dismiss_with_reason(env):
    c,_,_=env;row=send(c).get_json()['review'];url='/api/v1/medication/reviews/'+row['id']
    assert c.patch(url,json={'action':'dismiss','note':''}).status_code==422
    assert c.patch(url,json={'action':'dismiss','note':'Entered in error'}).get_json()['review']['status']=='dismissed'

def test_prescriber_queue_is_recipient_scoped(env):
    c,_,who=env;send(c);send(c,prescriber_id='doc2');who.update(id='doc1',role='prescriber')
    rows=c.get('/api/v1/medication/reviews').get_json()['reviews'];assert len(rows)==1 and rows[0]['prescriber_id']=='doc1'
    assert c.post('/api/v1/escalations/e1/acknowledge',json={}).status_code==403


def test_unassigned_patient_detail_is_not_exposed_to_nurse(env):
    c,_,_=env
    assert c.get('/api/v1/patients/p2').status_code==403
    assert c.get('/api/v1/patients/pb').status_code==404


def test_request_carries_an_sla_deadline(env):
    c,_,_=env
    assert send(c).get_json()['review']['sla_deadline']

def test_escalation_widens_a_stale_request_to_the_prescriber_pool(env):
    """The second level: doc2 cannot touch doc1's request until it breaches."""
    from app.services.medication import sweep_medication_review_breaches
    c,db,who=env;row=send(c).get_json()['review'];url='/api/v1/medication/reviews/'+row['id']
    who.update(id='doc2',role='prescriber')
    assert c.patch(url,json={'action':'approve','note':'Mine now'}).status_code==403
    assert c.get('/api/v1/medication/reviews').get_json()['reviews']==[]

    db.rows['medication_reviews'][0]['sla_deadline']='2000-01-01T00:00:00+00:00'
    escalated=sweep_medication_review_breaches()
    assert len(escalated)==1 and escalated[0]['status']=='escalated'
    # A second sweep must not re-escalate what it already moved.
    assert sweep_medication_review_breaches()==[]

    assert len(c.get('/api/v1/medication/reviews').get_json()['reviews'])==1
    r=c.patch(url,json={'action':'approve','note':'Agreed, start the class'})
    assert r.status_code==200 and r.get_json()['review']['status']=='approved'
    assert r.get_json()['review']['decided_by']=='doc2'

def test_acknowledge_records_a_look_without_closing_the_request(env):
    c,_,who=env;row=send(c).get_json()['review'];url='/api/v1/medication/reviews/'+row['id']
    who.update(id='doc1',role='prescriber')
    r=c.patch(url,json={'action':'acknowledge','note':'On my way'})
    assert r.status_code==200 and r.get_json()['review']['acknowledged_at']
    # Still open, so it still appears as outstanding work.
    assert r.get_json()['review']['status']=='pending'
    assert c.patch(url,json={'action':'acknowledge','note':'Again'}).status_code==409
    assert c.patch(url,json={'action':'reject','note':'Hold off, recheck in an hour'}).status_code==200

def test_a_nurse_cannot_decide_a_request(env):
    c,_,_=env;row=send(c).get_json()['review'];url='/api/v1/medication/reviews/'+row['id']
    for action in ('approve','reject','acknowledge'):
        assert c.patch(url,json={'action':action,'note':'Not my call'}).status_code==403

def test_acknowledging_does_not_exempt_a_request_from_escalation(env):
    """Seen-but-undecided is the exact failure the second level exists to catch."""
    from app.services.medication import sweep_medication_review_breaches
    c,db,who=env;row=send(c).get_json()['review']
    who.update(id='doc1',role='prescriber')
    c.patch('/api/v1/medication/reviews/'+row['id'],json={'action':'acknowledge','note':'Seen'})
    db.rows['medication_reviews'][0]['sla_deadline']='2000-01-01T00:00:00+00:00'
    escalated=sweep_medication_review_breaches()
    assert len(escalated)==1 and escalated[0]['status']=='escalated'
    assert escalated[0]['acknowledged_at']

def test_request_info_keeps_the_request_open_and_on_the_clock(env):
    """A question to the nurse is not an answer, so the SLA keeps running."""
    from app.services.medication import sweep_medication_review_breaches
    c,db,who=env;row=send(c).get_json()['review'];url='/api/v1/medication/reviews/'+row['id']
    who.update(id='doc1',role='prescriber')
    r=c.patch(url,json={'action':'request_info','note':'What was the BP before the last dose?'})
    assert r.status_code==200
    got=r.get_json()['review']
    assert got['status']=='pending' and got['info_requested_at']
    assert got['info_request_note']=='What was the BP before the last dose?'
    # Asking must not grant an exemption from the second level.
    db.rows['medication_reviews'][0]['sla_deadline']='2000-01-01T00:00:00+00:00'
    assert len(sweep_medication_review_breaches())==1
    # Still decidable afterwards.
    assert c.patch(url,json={'action':'approve','note':'Thanks, go ahead'}).status_code==200

def test_a_nurse_cannot_request_info_on_their_own_request(env):
    c,_,_=env;row=send(c).get_json()['review']
    assert c.patch('/api/v1/medication/reviews/'+row['id'],
                   json={'action':'request_info','note':'asking myself'}).status_code==403


def test_patient_list_and_detail_expose_news_warning_beside_combined_score(env):
    from datetime import datetime, timezone
    c, db, _ = env
    db.rows['risk_scores'] = [dict(org_id='a', patient_id='p1', score=27.17, worry_component=8.42, vitals_component=18.75, trend_component=0, computed_at='2026-10-24T12:00:00Z')]
    db.rows['worry_signals'] = []
    db.rows['nurse_notes'] = []
    db.rows['vitals'][0].update(heart_rate=181, resp_rate=24, spo2=98, systolic_bp=133, temperature_c=36.8, news_score=5, recorded_at=datetime.now(timezone.utc).isoformat())
    db.rows['escalations'][0].update(status='acknowledged', rule='news_high', triggered_at='2026-10-24T12:00:00Z')
    listing = c.get('/api/v1/patients/mine')
    assert listing.status_code == 200
    patient = listing.get_json()['patients'][0]
    assert patient['risk_score'] == 27.17
    assert patient['open_escalation']['status'] == 'acknowledged'
    assert patient['news_alert']['score'] == 5
    assert patient['news_alert']['band'] == 'medium'
    assert patient['news_alert']['single_red_parameters'] == ['heart_rate']
    db.rows['risk_scores'].append(dict(org_id='b', patient_id='p1', score=99, computed_at='2026-10-24T12:01:00Z'))
    detail = c.get('/api/v1/patients/p1')
    assert detail.status_code == 200
    assert detail.get_json()['news_alert'] == patient['news_alert']
    assert len(detail.get_json()['risk_history']) == 1
    assert detail.get_json()['risk_history'][0]['score'] == 27.17


def test_dashboard_news_warning_outranks_combined_score_and_worry(env):
    c, db, identity = env
    identity['role'] = 'charge_nurse'
    decorated = [
        dict(id='p1', risk_score=95, open_escalation=None, latest_worry={'level': 4}, news_alert={'band': 'none'}),
        dict(id='p2', risk_score=20, open_escalation=None, latest_worry={'level': 0}, news_alert={'band': 'high'}),
    ]
    with patch('app.api.dashboard.supabase', return_value=db), patch('app.api.dashboard._decorate', return_value=decorated):
        response = c.get('/api/v1/dashboard/patients')
    assert response.status_code == 200
    assert [patient['id'] for patient in response.get_json()['patients']] == ['p2', 'p1']


def prescription_payload(**overrides):
    return {
        'action': 'approve', 'note': 'Reviewed all sections',
        'prescriptions': [{'medication': 'Demo medication', 'dose': 'Recorded dose',
                           'route': 'Recorded route', 'frequency': 'Recorded frequency',
                           'duration': 'Recorded duration', 'instructions': 'Recorded instruction'}],
        'reviewed_sections': list(range(8)), **overrides,
    }


def test_doctor_prescription_is_saved_with_review_confirmations(env):
    c, db, who = env
    review = send(c).get_json()['review']
    who.update(id='doc1', role='prescriber')
    result = c.patch('/api/v1/medication/reviews/' + review['id'], json=prescription_payload())
    assert result.status_code == 200
    saved = result.get_json()['review']
    assert saved['status'] == 'approved'
    assert saved['prescriptions'][0]['instructions'] == 'Recorded instruction'
    assert saved['reviewed_sections'] == list(range(8))
    assert c.patch('/api/v1/medication/reviews/' + review['id'], json=prescription_payload()).status_code == 409


@pytest.mark.parametrize('overrides', [
    {'reviewed_sections': list(range(7))},
    {'reviewed_sections': [0] * 8},
    {'prescriptions': []},
    {'prescriptions': [{'medication': 'Missing dose and route'}]},
    {'action': 'request_info'},
    {'action': 'dismiss'},
])
def test_prescription_approval_rejects_incomplete_or_wrong_action(env, overrides):
    c, db, who = env
    review = send(c).get_json()['review']
    who.update(id='doc1', role='prescriber')
    assert c.patch('/api/v1/medication/reviews/' + review['id'], json=prescription_payload(**overrides)).status_code == 422
    assert db.rows['medication_reviews'][0]['status'] == 'pending'


def test_nurse_and_other_doctor_cannot_approve_prescriptions(env):
    c, _, who = env
    review = send(c).get_json()['review']
    url = '/api/v1/medication/reviews/' + review['id']
    assert c.patch(url, json=prescription_payload()).status_code == 403
    who.update(id='doc2', role='prescriber')
    assert c.patch(url, json=prescription_payload()).status_code == 403
