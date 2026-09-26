"""Do not let a broken alternate provider disable a fresh consistent track."""
from contextlib import closing
import pytest
from field_router.gps.service import GpsService
from test_gps import service,NOW,point,packet,wait_for,codes
from test_gps_quality_workflow import fix
from test_gps_test_plan import worker,POINT,NOW as DAY
from field_router.gps.test_plan import make_test_day
from field_router.gps.context import project_context

def feed(gps,clock):
    # GPS gives impossible outliers; network continues the same real path.
    for seconds in (0,20,40):
        clock[0]=NOW+seconds
        assert gps.ingest(packet(fix(clock[0],provider='network')))['accepted']
        clock[0]+=10
        result=gps.ingest(packet(fix(clock[0],offset=.51)))
        assert not result['accepted']
    return result

def test_repeated_alternate_fixes_do_not_claim_that_the_first_source_is_correct():
    for initial,other in (('network','gps'),('gps','network')):
        clock=[NOW]
        with service(clock=lambda:clock[0]) as gps:
            for seconds in (0,20,40,60):
                clock[0]=NOW+seconds
                gps.ingest(packet(fix(clock[0],provider=initial)))
                clock[0]+=10
                result=gps.ingest(packet(fix(clock[0],provider=other,offset=.51)))
                assert not result['accepted']
            row=gps.snapshot()['rows'][0]
            assert 'quality' in codes(row) and row['analysis'] is None
            assert row['position']['provider']==initial
            assert not row['quality_detail'].get('ignored_alternate')


def test_losing_reference_does_not_refresh_its_age():
    clock=[NOW]
    with service(clock=lambda:clock[0]) as gps:
        feed(gps,clock)
        clock[0]=NOW+80
        result=gps.ingest(packet(fix(clock[0],offset=.51)))
        assert not result['accepted']
        row=gps.snapshot()['rows'][0]
        assert row['quality_reason']=='jump' and row['point_age_seconds']==40
        clock[0]=NOW+140
        assert 'stale' in codes(gps.snapshot()['rows'][0])


def test_assignment_distance_is_not_fix_jump_and_visible_while_working():
    session=make_test_day(worker('car'),POINT,DAY)
    session.record_fact({'version':1,'engineer_id':'test-e1','status':'working','request_id':'gps-test-001',
                         'time':session.time,'remaining_minutes':10})
    from test_gps import StoreOnly
    with closing(StoreOnly(project_context(session),clock=lambda:DAY.timestamp())) as gps:
        gps.ingest({'engineer_id':'test-e1','tracker_id':'phone','state':'active','point':POINT})
        row=gps.snapshot()['rows'][0]
        assert row['assignment']['id']=='gps-test-001'
        assert row['assignment']['distance_m']==pytest.approx(1000,abs=1)
