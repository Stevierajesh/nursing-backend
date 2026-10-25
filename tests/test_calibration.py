"""Baseline calibration accuracy, reproducibility, provenance and API coverage."""
import csv
from statistics import fmean
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app import create_app
from app.auth.middleware import CurrentUser
from app.schemas.simulation import BaselineVitals, SimulationConfig
from app.services.calibration import CURRENT_MODEL, load_model, sample_baseline
from app.services.simulation import observation
from scripts.calibrate_simulator import fit


def test_shipped_artifact_audit_and_excluded_labels():
    m=load_model();a=m['audit']
    assert a['rows']==200020
    assert a['accepted_rows']==sum(g['count'] for g in m['groups'].values())
    assert a['repeated_patient_ids']==0 and not a['temporal_model_fitted']
    assert 'Risk Category' not in m['fit']['fields']
    assert set(m['fit']['fields'])==set(BaselineVitals.model_fields)
    assert all(len(g['samples'])==256 for g in m['groups'].values())
    assert a['derived_value_checks']['map_mismatches']==0
    assert a['derived_value_checks']['pulse_pressure_mismatches']==0


def test_valid_joint_tuples_and_reproducibility():
    m=load_model()
    for seed in range(1000):
        s=sample_baseline(seed)
        assert s==sample_baseline(seed)
        assert list(s['baseline'].values()) in m['groups'][s['calibration']['selected_group']]['samples']
        BaselineVitals.model_validate(s['baseline'])
        b,d=s['baseline'],s['derived']
        assert d['pulse_pressure']==b['systolic_bp']-b['diastolic_bp']
        assert d['mean_arterial_pressure']==round(b['diastolic_bp']+d['pulse_pressure']/3,1)
    assert len({tuple(sample_baseline(i)['baseline'].values()) for i in range(100)})>80


def test_sampling_approximates_full_source_means():
    m=load_model();samples=[sample_baseline(seed)['baseline'] for seed in range(6000)]
    tolerances={'heart_rate':1,'resp_rate':0.3,'spo2':0.2,'systolic_bp':0.8,'diastolic_bp':0.6,'temperature_c':0.08}
    for field,mean in zip(m['fit']['fields'],m['overall']['mean']):
        assert abs(fmean(s[field] for s in samples)-mean)<tolerances[field],field


@pytest.mark.parametrize('age',['18-39','40-64','65-79','80+'])
@pytest.mark.parametrize('sex',['M','F'])
def test_conditional_sampling_and_config_roundtrip(age,sex):
    s=sample_baseline(42,age,sex)
    assert s['calibration']['selected_group']==f'{age}|{sex}'
    c=SimulationConfig(baseline=s['baseline'],calibration=s['calibration'])
    assert c.calibration.model_version==CURRENT_MODEL
    assert observation(c,0)==observation(SimulationConfig.model_validate(c.model_dump()),0)


@pytest.mark.parametrize('change',['seed','baseline','provenance'])
def test_provenance_cannot_mismatch_saved_baseline(change):
    s=sample_baseline(42);c=dict(baseline=s['baseline'],calibration=s['calibration'],seed=42)
    if change=='seed': c['seed']=43
    elif change=='baseline': c['baseline']={**c['baseline'],'heart_rate':120}
    else: c['calibration']={**c['calibration'],'selected_group':'made-up'}
    with pytest.raises(ValidationError): SimulationConfig.model_validate(c)
    c['calibration']=None
    SimulationConfig.model_validate(c)


@pytest.mark.parametrize('kw',[{'seed':True},{'seed':-1},{'age_band':'child'},{'sex':'X'},{'version':'../../secret'}])
def test_invalid_sampler_inputs(kw):
    with pytest.raises(ValueError): sample_baseline(**{'seed':42,**kw})


def test_legacy_manual_configs_unchanged():
    c=SimulationConfig()
    assert c.calibration is None and c.baseline.heart_rate==78
    assert observation(c,0)['news_score']==0


def make_csv(path,labels=('High Risk','Low Risk')):
    fields=['Patient ID','Timestamp','Heart Rate','Respiratory Rate','Oxygen Saturation','Systolic Blood Pressure','Diastolic Blood Pressure','Body Temperature','Age','Gender','Risk Category','Derived_Pulse_Pressure','Derived_MAP']
    with path.open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for i,(hr,sbp,dbp,label) in enumerate(zip([70,90],[120,140],[80,90],labels),1):
            w.writerow(dict(zip(fields,[i,'2024-01-01',hr,16,98,sbp,dbp,36.8,67,'Male',label,sbp-dbp,dbp+(sbp-dbp)/3])))


def test_fitter_learns_covariance_not_labels(tmp_path):
    p=tmp_path/'data.csv';make_csv(p)
    first=fit(p,capacity=2)
    assert first==fit(p,capacity=2)
    g=first['groups']['65-79|M']
    assert g['mean'][0]==80 and g['covariance'][0][3]==200
    make_csv(p,labels=('arbitrary','relabelled'));second=fit(p,capacity=2)
    assert first['groups']==second['groups']
    assert first['source']['sha256']!=second['source']['sha256']
    assert first['audit']['derived_value_checks']['map_mismatches']==0


def test_fitter_rejects_invalid_rows_and_columns(tmp_path):
    p=tmp_path/'data.csv';make_csv(p)
    p.write_text(p.read_text().replace(',98,',',nan,',1))
    m=fit(p,capacity=2)
    assert m['audit']['rejected_rows']==1 and m['audit']['accepted_rows']==1
    p.write_text('HR\n70\n')
    with pytest.raises(ValueError,match='Missing columns'): fit(p)


@pytest.mark.parametrize('role',['admin','charge_nurse','nurse','prescriber'])
def test_api_permissions_and_preview(role):
    u=CurrentUser('u','test@example.com','org',role,None,'Test')
    with patch('app.auth.middleware.verify_token',return_value={'sub':'u'}),patch('app.auth.middleware._load_profile',return_value=u):
        c=create_app().test_client();c.environ_base['HTTP_AUTHORIZATION']='Bearer test'
        r=c.post('/api/v1/simulations/calibration/sample',json={'seed':42,'age_band':'65-79','sex':'M'})
        meta=c.get('/api/v1/simulations/calibration')
        if role in ('nurse','prescriber'):
            assert r.status_code==meta.status_code==403
            return
        assert r.status_code==meta.status_code==200
        assert meta.get_json()['version']==CURRENT_MODEL
        s=r.get_json();config=SimulationConfig(baseline=s['baseline'],calibration=s['calibration'])
        preview=c.post('/api/v1/simulations/preview',json=config.model_dump())
        assert preview.status_code==200
        assert preview.get_json()['observations'][0]=={'elapsed_seconds':0,**observation(config,0)}
        assert c.post('/api/v1/simulations/calibration/sample',json={'seed':1.1}).status_code==422
        assert c.post('/api/v1/simulations/calibration/sample',json={'model_version':'unknown'}).status_code==422
