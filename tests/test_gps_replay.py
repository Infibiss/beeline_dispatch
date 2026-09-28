from copy import deepcopy
import json
import pytest
from field_router.gps.replay import replay
from field_router.gps.contracts import GpsInputError
from test_gps import service, NOW, packet, point, context


def payload(**changes):
    return {'engineer_id':'e1','request_id':'a','version':1,'minute':550,'format':'csv',
            'text':'seconds,lat,lon,accuracy_m\n0,55.7,37.7,10\n60,55.7,37.7,10\n120,55.7,37.7,10',
            **changes}


def test_replay_isolates_real_stream_and_preserves_intervals():
    with service(clock=lambda:NOW) as gps:
        gps.ingest(packet(point()))
        before=gps.trace('e1','device')
        result=replay(gps,payload())
        assert result['accepted']==3 and result['plan_unchanged']
        assert gps.trace('e1','device')==before
        assert gps.context()==context()
        trace=gps.trace('e1','demo')
        assert len(trace['samples'])==3
        times=[row['point']['measured_at_ms'] for row in trace['samples']]
        assert times==[(NOW-120)*1000,(NOW-60)*1000,NOW*1000]


@pytest.mark.parametrize('changes',[
    {'text':'seconds,lat,lon,accuracy_m\n0,91,37,10'},
    {'text':'seconds,lat,lon,accuracy_m\n0,55,37,10\n0,55,37,10'},
    {'text':'seconds,lat,lon,accuracy_m\n600,55,37,10'},
    {'text':'seconds,lat,lon,accuracy_m\n0,NaN,37,10'},
    {'text':'bad,columns\n1,2'}, {'text':'[]','format':'json'},
    {'text':'[{"seconds":true,"lat":55,"lon":37,"accuracy_m":10}]','format':'json'},
    {'request_id':'other'}, {'engineer_id':'ghost'}, {'version':2}, {'minute':1440},
])
def test_invalid_import_is_atomic(changes):
    with service(clock=lambda:NOW) as gps:
        replay(gps,payload())
        before=deepcopy(gps.trace('e1','demo'))
        with pytest.raises(GpsInputError):replay(gps,payload(**changes))
        assert gps.trace('e1','demo')==before


def test_json_and_bad_accuracy_report_rejections():
    rows=[{'seconds':0,'lat':55.7,'lon':37.7,'accuracy_m':1500}]
    with service(clock=lambda:NOW) as gps:
        result=replay(gps,payload(format='json',text=json.dumps(rows)))
        assert result['accepted']==0 and result['rejected']==1
        assert result['reasons']==['low_accuracy']


def test_conflict_identifies_both_versions_and_never_runs_old_context():
    with service(clock=lambda:NOW) as gps:
        replay(gps,payload())
        updated=deepcopy(context());updated['version']=2
        updated['engineers']['e1']['status']={'status':'available'}
        gps.update_context(updated)
        before=deepcopy(gps.trace('e1','demo'))
        with pytest.raises(GpsInputError) as err:replay(gps,payload())
        assert err.value.status==409
        assert '№ 1' in str(err.value) and '№ 2' in str(err.value)
        assert gps.trace('e1','demo')==before
        assert replay(gps,payload(version=2))['accepted']==3


def test_ui_distinguishes_status_update_from_route_change_and_removed_assignment():
    from pathlib import Path
    import shutil
    import subprocess
    node=shutil.which('node')
    if not node:pytest.skip('Node is required for UI comparison')
    root=Path(__file__).resolve().parents[1]
    script=r"""
const assert=require('node:assert/strict');
const {planComparison}=require('./src/field_router/static/gps-dispatcher.js');
const before={version:1,date:'2026-08-17',engineer:{id:'e1',shift:{start:540,end:1080}},status:null,
  tasks:[{id:'a',point:{lat:55.7,lon:37.7},visit:{start:600,end:630},windows:[{start:540,end:720}],duration_minutes:30,status:'planned'}]};
const after=structuredClone(before);after.version=2;after.status={status:'available',time:540};
after.plan_updates=[{version:2,label:'Recorded status'}];
let diff=planComparison(before,after,'a');assert.equal(diff.selectedUnchanged,true);assert.equal(diff.canReplay,true);
assert.equal(diff.updates[0].label,'Recorded status');
after.tasks[0].visit.start=620;diff=planComparison(before,after,'a');assert.equal(diff.selectedUnchanged,false);
after.tasks=[];assert.equal(planComparison(before,after,'a').canReplay,false);
after.tasks=structuredClone(before.tasks);after.tasks[0].status='completion_pending';
assert.equal(planComparison(before,after,'a').canReplay,false);
"""
    result=subprocess.run([node,'-'],input=script,cwd=root,capture_output=True,text=True,encoding='utf-8')
    assert result.returncode==0,result.stderr
