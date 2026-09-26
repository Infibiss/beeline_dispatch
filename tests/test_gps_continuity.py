"""Regressions: renewed tokens, delayed sources and an already corrupted archive."""
from copy import deepcopy
import json
import sqlite3
import pytest
from field_router.gps.contracts import distance
from field_router.gps.service import GpsService
from test_gps import service,NOW,packet,codes,wait_for
from test_gps_quality_workflow import fix


def test_new_tracker_cannot_teleport_employee_and_delayed_good_fix_still_arrives(tmp_path):
    clock=[NOW];path=tmp_path/'gps.sqlite3'
    with service(clock=lambda:clock[0],history_path=path) as gps:
        old=fix(NOW,provider='network')
        gps.ingest(packet(old,tracker='native-before'))
        clock[0]+=1
        gps.ingest(packet(state='off',tracker='native-before'))
        clock[0]=NOW+20
        rejected=gps.ingest_native('e1','native-after',fix(clock[0],offset=.51))
        assert not rejected['accepted']
        assert gps.snapshot()['rows'][0]['position']==old
        # The valid network measurement arrives after a more recent rejected GPS fix.
        clock[0]=NOW+23
        delayed=fix(NOW+18,provider='network')
        assert gps.ingest_native('e1','native-after',delayed)['accepted']
        assert gps.snapshot()['rows'][0]['position']==delayed
        # Retrying a rejected packet does not count as independent evidence.
        clock[0]+=1
        assert not gps.ingest_native('e1','native-after',fix(NOW+20,offset=.51))['accepted']
        assert len(gps.history_page('e1')['samples'])==3
    with service(clock=lambda:clock[0],history_path=path) as gps:
        assert gps.snapshot()['rows'][0]['position']==delayed


def legacy_archive(path):
    """Write the old bug's receipts: a new token accepted an impossible first fix."""
    clock=[NOW]
    with service(clock=lambda:clock[0],history_path=path) as gps:
        gps.ingest(packet(fix(NOW,provider='network'),tracker='native-before'))
    raw=fix(NOW+10,offset=.51)
    # Store the flawed historical decisions verbatim; the repair must not erase them.
    rows=[
        {'point':raw,'received_at':NOW+10,'accepted':True,'reason':None},
        {'point':fix(NOW+8,provider='network'),'received_at':NOW+12,'accepted':False,'reason':'historical_only'},
        {'point':fix(NOW+20,provider='network'),'received_at':NOW+23,'accepted':False,'reason':'jump'},
        {'point':fix(NOW+30,offset=.512),'received_at':NOW+30,'accepted':True,'reason':None},
        {'point':fix(NOW+40,provider='network'),'received_at':NOW+42,'accepted':False,'reason':'jump'},
    ]
    with sqlite3.connect(path) as db:
        metadata=json.loads(db.execute('SELECT data FROM streams').fetchone()[0])
        metadata.update(tracker_id='native-after',last_raw=rows[-1]['point'],last_good=rows[-2]['point'],
                        received_at=NOW+42,quality=None,quality_detail={'ignored_alternate':True})
        metadata.pop('continuity_version',None)
        for sample in rows:
            db.execute('INSERT INTO fixes(engineer,tracker,measured,received,data) VALUES (?,?,?,?,?)',
                       ('e1','native-after',sample['point']['measured_at_ms'],sample['received_at'],json.dumps(sample)))
        db.execute('UPDATE streams SET data=?',(json.dumps(metadata),))
    return rows


def test_legacy_bad_anchor_is_repaired_without_rewriting_history_and_survives_two_restarts(tmp_path):
    path=tmp_path/'gps.sqlite3'
    legacy_archive(path)
    def archive():
        with sqlite3.connect(path) as db:return db.execute('SELECT data FROM fixes ORDER BY id').fetchall()
    before=archive();clock=[NOW+45]
    for step in range(2):
        clock[0]=NOW+45+step
        with service(clock=lambda:clock[0],history_path=path) as gps:
            row=gps.snapshot()['rows'][0]
            assert distance(row['position'],fix(NOW))<1
            assert row['position']['measured_at_ms']==(NOW+40)*1000
            assert row['point_age_seconds']==5+step
            assert row['quality_detail']['continuity_repaired']
            assert 'quality' in codes(row)
            assert archive()==before
            assert all(distance(p,fix(NOW))<1 for p in gps._streams[('device','e1')]['points'])
            # Persist repaired metadata without manufacturing a coordinate.
            gps.native_heartbeat('e1','native-after')
    clock[0]=NOW+105
    with service(GpsService,clock=lambda:clock[0],history_path=path) as gps:
        gps.ingest_native('e1','native-after',fix(clock[0],provider='network'))
        wait_for(lambda:gps.snapshot()['rows'][0]['analysis'] is not None)
        row=gps.snapshot()['rows'][0]
        assert distance(row['analysis']['filtered'],fix(NOW))<1
        assert all(distance(p,fix(NOW))<1 for p in gps._streams[('device','e1')]['points'])


def test_handoff_repair_does_not_relocate_a_plausible_new_track(tmp_path):
    path=tmp_path/'gps.sqlite3'
    clock=[NOW]
    with service(clock=lambda:clock[0],history_path=path) as gps:
        gps.ingest(packet(fix(NOW),tracker='native-before'))
        clock[0]+=1;gps.ingest(packet(state='off',tracker='native-before'))
        clock[0]+=20;gps.ingest(packet(fix(clock[0],offset=.0101),tracker='native-after'))
    with sqlite3.connect(path) as db:
        metadata=json.loads(db.execute('SELECT data FROM streams').fetchone()[0])
        metadata.pop('continuity_version',None)
        db.execute('UPDATE streams SET data=?',(json.dumps(metadata),))
    with service(clock=lambda:clock[0],history_path=path) as gps:
        row=gps.snapshot()['rows'][0]
        assert row['position']['lat']==pytest.approx(fix(NOW,offset=.0101)['lat'])
        assert not row['quality_detail'].get('continuity_repaired')
