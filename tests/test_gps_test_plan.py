"""Test-day isolation, same employee identity and actual device-channel inference."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import threading

import pytest

from field_router.auth import AccountStore
from field_router.gps.config import GpsConfig
from field_router.gps.context import project_context
from field_router.gps.contracts import distance
from field_router.gps.models import BaselineGpsModel
from field_router.gps.service import GpsService
from field_router.gps.test_plan import make_test_day, prepare, recent_reference
from field_router.models import Engineer, TimeWindow, Transport
from field_router.regions import discover_regions, load_session
from field_router.scenario_server import create_server
from test_auth import Client, PASSWORD
from test_gps import codes, wait_for

NOW = datetime(2026,9,26,15,0,tzinfo=timezone(timedelta(hours=3)))
POINT = {'lat':55.7,'lon':37.7,'accuracy_m':8.,'measured_at_ms':NOW.timestamp()*1000}


def worker(mode='public'):
    return Engineer(id='test-e1', name='Тестовый инженер', region='Восток',
                    skills=['local'], transport=mode, shift=TimeWindow(start=540,end=1080))


def source_files(tmp_path):
    (tmp_path/'config').mkdir()
    (tmp_path/'config/gps.json').write_text('{}',encoding='utf-8')
    state = tmp_path/'output/local/mobile-east.json'
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps(make_test_day(worker(),POINT,NOW-timedelta(days=1)).dump()),encoding='utf-8')
    history = state.with_suffix('.gps.sqlite3')
    with sqlite3.connect(history) as db:
        db.execute('CREATE TABLE streams(engineer TEXT, data TEXT)')
        db.execute('INSERT INTO streams VALUES (?,?)',('test-e1',json.dumps({'last_good':POINT})))
    return state, history


@pytest.mark.parametrize('mode',list(Transport))
def test_today_valid_plan_preserves_identity_and_real_channel_has_no_fake_fix(mode,tmp_path):
    session=make_test_day(worker(mode),POINT,NOW)
    assert session.scenario.date=='2026-09-26'
    assert session.plan.metrics.assigned==1 and not session.plan.unassigned
    assert session.scenario.engineers[0].id=='test-e1'
    assert session.scenario.engineers[0].transport==mode
    assert session.plan.routes[0].visits[0].start==905
    assert not session.fact_log and not session.statuses
    target=project_context(session)['engineers']['test-e1']['tasks'][0]['point']
    assert distance(POINT,target)==pytest.approx(1000,abs=.1)
    with closing(GpsService(project_context(session),clock=lambda:NOW.timestamp(),history_path=tmp_path/'gps.sqlite3')) as gps:
        assert gps.snapshot()['rows'][0]['position'] is None
        assert gps.history_page('test-e1')['samples']==[]
        gps.ingest_native('test-e1','native-test',POINT)
        wait_for(lambda: gps.snapshot()['rows'][0]['analysis'] is not None)
        row=gps.snapshot()['rows'][0]
        assert row['date_matches'] and row['source']=='native'
        assert row['analysis']['target']['id']=='gps-test-001'
        assert row['analysis']['eta']['schedule']['source']=='server'
        if mode==Transport.PUBLIC:
            assert 'departure_due' in codes(row['analysis'])


def test_model_warns_of_stopping_and_late_plan_on_new_test_day():
    session=make_test_day(worker('car'),POINT,NOW)
    engineer=project_context(session)['engineers']['test-e1']
    engineer['status']={'status':'traveling','request_id':'gps-test-001'}
    clock=NOW.timestamp()+6*60
    points=[{**POINT,'measured_at_ms':(clock-age)*1000} for age in (120,60,0)]
    result=BaselineGpsModel().analyze({'engineer':engineer,'date':session.scenario.date,
        'now':clock,'demo_minute':None,'routing':{},'config':GpsConfig().public(),'points':points})
    assert {'plan_late','stopped'} <= codes(result)


def test_isolated_files_round_trip_original_unchanged_and_same_region_login(tmp_path):
    source,history=source_files(tmp_path)
    before=source.read_bytes(),history.read_bytes()
    first=prepare(tmp_path,source,now=NOW)
    second=prepare(tmp_path,source,now=NOW)
    assert first['state']!=second['state']
    assert (source.read_bytes(),history.read_bytes())==before
    assert first['date']=='2026-09-26'
    assert not __import__('pathlib').Path(first['gps_history']).exists()
    session=load_session(first['comparison'],first['state'])
    regions=discover_regions(first['comparison'],first['state'])
    assert list(regions)==['east']
    accounts=AccountStore(source.with_name('accounts.sqlite3'))
    accounts.create_engineer('worker',PASSWORD,'east','test-e1')
    server=create_server(session,first['state'],port=0,disable_gps=True,accounts=accounts,region_sources=regions)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        client=Client(server)
        assert client.login('worker')[0]==200
        status,body,_=client.request('/r/east/api/engineer?engineer_id=test-e1')
        assert status==200 and body['date']=='2026-09-26'
        assert body['tasks'][0]['id']=='gps-test-001'
        assert client.request('/api/state')[0]==403
    finally:
        server.shutdown();server.server_close();thread.join()


def test_stale_imprecise_or_other_worker_fix_not_silently_used(tmp_path):
    _,history=source_files(tmp_path)
    with pytest.raises(ValueError,match='10 минут'):
        recent_reference(history,[worker()],NOW+timedelta(minutes=11))
    with pytest.raises(ValueError,match='отсутствует'):
        recent_reference(history,[worker()],NOW,engineer_id='other')
    with pytest.raises(ValueError,match='10 минут'):
        recent_reference(history,[worker()],NOW,max_accuracy=5)
    assert recent_reference(history,[worker()],NOW)[0].id=='test-e1'


def test_near_midnight_does_not_roll_into_tomorrow():
    with pytest.raises(ValueError,match='сегодняшнем'):
        make_test_day(worker(),POINT,NOW.replace(hour=23,minute=50))


def test_retest_uses_fresh_separate_journal_and_rejects_conflict(tmp_path):
    source,_=source_files(tmp_path)
    first=prepare(tmp_path,source,now=NOW)
    history=__import__('pathlib').Path(first['gps_history'])
    point={**POINT,'measured_at_ms':(NOW+timedelta(minutes=11)).timestamp()*1000}
    with sqlite3.connect(history) as db:
        db.execute('CREATE TABLE streams(engineer TEXT,data TEXT)')
        db.execute('INSERT INTO streams VALUES (?,?)',('test-e1',json.dumps({'last_good':point,'quality':None})))
    result=prepare(tmp_path,source,now=NOW+timedelta(minutes=11))
    assert result['reference_age_seconds']==0
    with sqlite3.connect(history) as db:
        db.execute('UPDATE streams SET data=?',(json.dumps({'last_good':point,'quality':'source_conflict'}),))
    with pytest.raises(ValueError,match='пригодной'):
        prepare(tmp_path,source,now=NOW+timedelta(minutes=11))


def test_resume_reuses_same_files_and_does_not_require_fresh_or_undisputed_gps(tmp_path):
    from pathlib import Path
    from field_router.gps.test_plan import resume_latest
    source, history = source_files(tmp_path)
    manifest = prepare(tmp_path, source, now=NOW)
    # Simulate the issue that prevented creating a new plan: disputed/stale GPS.
    with sqlite3.connect(history) as db:
        db.execute('UPDATE streams SET data=?',(json.dumps({'last_good':POINT,'quality':'jump'}),))
    before = {p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    resumed = resume_latest(tmp_path,source,now=NOW+timedelta(minutes=20))
    assert resumed['state']==manifest['state'] and resumed['gps_history']==manifest['gps_history']
    assert before=={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    for options in ({'now':NOW+timedelta(days=1)}, {'now':NOW,'engineer_id':'another'}):
        with pytest.raises(ValueError, match='Нет сохранённого'):
            resume_latest(tmp_path,source,**options)
    data = dict(manifest, state=str(source))
    Path(manifest['state']).with_name('manifest.json').write_text(json.dumps(data),encoding='utf-8')
    with pytest.raises(ValueError, match='Нет сохранённого'):
        resume_latest(tmp_path,source,now=NOW)


def test_today_gps_plan_keeps_engineers_linked_vehicle_from_new_fleet_contract(tmp_path):
    from pathlib import Path
    source,_=source_files(tmp_path)
    saved=json.loads(source.read_text(encoding='utf-8'))
    saved['scenario']['engineers'][0].update(transport='car',vehicle_id='test-car')
    saved['scenario']['vehicles']=[{'id':'test-car','label':'Test vehicle','odometer_km':1000}]
    source.write_text(json.dumps(saved),encoding='utf-8')
    before=source.read_bytes()
    manifest=prepare(tmp_path,source,now=NOW)
    session=load_session(manifest['comparison'],manifest['state'])
    assert session.scenario.engineers[0].vehicle_id=='test-car'
    assert session.scenario.vehicles[0].id=='test-car'
    assert session.scenario.vehicles[0].odometer_km==1000
    assert source.read_bytes()==before
