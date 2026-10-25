"""Simulation behavior, signal independence, API scope and scheduler wiring."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app import create_app
from app.auth.middleware import CurrentUser
from app.schemas.simulation import BaselineVitals, SimulationConfig
from app.services.news import news_score
from app.services.simulation import observation, tick_patient
from app.services.risk import compute_risk


def test_stable_stays_close_to_baseline_and_never_generates_worry():
    config = SimulationConfig(scenario='stable')
    for elapsed in range(0, 601, 15):
        row = observation(config, elapsed)
        assert abs(row['heart_rate'] - config.baseline.heart_rate) <= 2
        assert abs(row['systolic_bp'] - config.baseline.systolic_bp) <= 2
        assert row['news_score'] == 0
        assert not {'worry', 'worry_level', 'reasons'} & row.keys()
        assert row == observation(config, elapsed)
    assert observation(config, -10) == observation(config, 0)

    # Past the authored arc the patient keeps being observed. The trajectory
    # holds -- still near baseline, still NEWS 0 -- but the readings must keep
    # moving: a monitored bed that emits one identical row forever is a broken
    # monitor, and this used to freeze because the variation clock was clamped
    # to duration_minutes alongside the trajectory clock.
    late = [observation(config, 9999 + i * 15) for i in range(20)]
    assert all(abs(r['heart_rate'] - config.baseline.heart_rate) <= 2 for r in late)
    assert all(r['news_score'] == 0 for r in late)
    assert len({r['heart_rate'] for r in late}) > 1
    assert observation(config, 9999) == observation(config, 9999)


@pytest.mark.parametrize('scenario', ['respiratory_decline', 'circulatory_decline'])
def test_deterioration_has_direction_and_higher_news(scenario):
    config = SimulationConfig(scenario=scenario)
    first, last = observation(config, 0), observation(config, 600)
    assert last['heart_rate'] > first['heart_rate'] + 20
    assert last['resp_rate'] > first['resp_rate']
    assert last['news_score'] > first['news_score']
    if scenario == 'respiratory_decline':
        assert last['spo2'] < first['spo2'] - 5
    else:
        assert last['systolic_bp'] < first['systolic_bp'] - 30


def test_recovery_improves_and_onset_delays_deterioration():
    config = SimulationConfig(scenario='recovery')
    assert observation(config, 0)['news_score'] > observation(config, 600)['news_score']
    declining = SimulationConfig(scenario='respiratory_decline', onset_minutes=5)
    assert observation(declining, 240)['news_score'] == 0


def test_all_scenarios_respect_bounds_pressure_order_and_news():
    for scenario in ('stable', 'respiratory_decline', 'circulatory_decline', 'recovery'):
        config = SimulationConfig(scenario=scenario, baseline=BaselineVitals(systolic_bp=85, diastolic_bp=84, spo2=85))
        for elapsed in range(0, 601, 5):
            v = observation(config, elapsed)
            assert 75 <= v['spo2'] <= 100
            assert 60 <= v['systolic_bp'] <= 220
            assert v['diastolic_bp'] < v['systolic_bp']
            assert v['news_score'] == news_score(v)


@pytest.mark.parametrize('payload', [
    {'scenario':'made_up'}, {'duration_minutes':0}, {'duration_minutes':2,'onset_minutes':2},
    {'baseline':{'systolic_bp':100,'diastolic_bp':110}}, {'baseline':{'temperature_c':float('nan')}},
    {'baseline':{'spo2':101}}, {'worry_level':4}, {'duration_minutes':2.5},
])
def test_bad_configuration_is_rejected(payload):
    with pytest.raises(ValidationError):
        SimulationConfig.model_validate(payload)


def test_worker_retries_risk_later_without_repeating_observations():
    snapshot = {'id':'run', 'revision':7, 'elapsed_seconds':30, 'config':SimulationConfig().model_dump()}
    calls = []
    class Client:
        def rpc(self, name, args):
            calls.append((name, args))
            return SimpleNamespace(execute=lambda:SimpleNamespace(data=snapshot if name == 'simulation_snapshot' else True))
    with patch('app.services.simulation.supabase',return_value=Client()), patch('app.services.escalation.evaluate_patient', side_effect=RuntimeError('offline')) as evaluate:
        assert tick_patient('org','patient')
    assert evaluate.call_count == 1
    assert [name for name,_ in calls] == ['simulation_snapshot','simulation_append']
    assert calls[-1][1]['p_revision'] == 7
    assert calls[-1][1]['p_values']['news_score'] == 0


@pytest.mark.parametrize('role', ['nurse','prescriber'])
def test_only_admin_or_charge_can_manage_simulations(role):
    user = CurrentUser('u','test@example.com','org',role,None,'Test')
    with patch('app.auth.middleware.verify_token',return_value={'sub':'u'}), patch('app.auth.middleware._load_profile',return_value=user):
        client = create_app().test_client()
        client.environ_base['HTTP_AUTHORIZATION'] = 'Bearer test'
        assert client.get('/api/v1/simulations').status_code == 403
        assert client.post('/api/v1/simulations/p/control',json={'action':'start'}).status_code == 403
        assert client.post('/api/v1/simulations/p/configure',json={}).status_code == 403


def test_configuration_cannot_cross_organizations():
    user = CurrentUser('u','test@example.com','org','admin',None,'Test')
    query = SimpleNamespace(select=lambda *a:query,eq=lambda *a:query,limit=lambda *a:query,execute=lambda:SimpleNamespace(data=[]))
    with patch('app.auth.middleware.verify_token',return_value={'sub':'u'}), patch('app.auth.middleware._load_profile',return_value=user), patch('app.auth.middleware.supabase',return_value=SimpleNamespace(table=lambda *a:query)), patch('app.api.simulation.supabase') as database:
        client = create_app().test_client()
        client.environ_base['HTTP_AUTHORIZATION'] = 'Bearer test'
        assert client.post('/api/v1/simulations/foreign/configure',json={}).status_code == 404
        assert client.post('/api/v1/simulations/foreign/control',json={'action':'start'}).status_code == 404
        database.assert_not_called()


def test_stable_simulation_plus_high_worry_does_not_alarm():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    rows = [{**observation(SimulationConfig(),0),'recorded_at':now}]
    result = compute_risk(rows,[{'level':4,'created_at':now}])
    assert not result.should_escalate


def test_the_arc_holds_at_its_end_state_rather_than_resetting():
    """A deteriorated patient must not quietly revert once the scenario ends."""
    config = SimulationConfig(scenario='respiratory_decline', duration_minutes=10, onset_minutes=1)
    end = observation(config, 10 * 60)
    for elapsed in (11 * 60, 30 * 60, 6 * 3600):
        later = observation(config, elapsed)
        # Same deteriorated plateau, within the authored wobble.
        assert abs(later['resp_rate'] - end['resp_rate']) <= 2
        assert abs(later['spo2'] - end['spo2']) <= 2
        assert later['news_score'] >= end['news_score'] - 1
    # And it never drifts back toward the healthy baseline.
    assert observation(config, 6 * 3600)['spo2'] < config.baseline.spo2 - 5
