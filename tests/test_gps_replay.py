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
