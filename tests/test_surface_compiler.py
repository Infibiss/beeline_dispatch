import csv,io,json,subprocess,sys,zipfile
import pytest

@pytest.mark.parametrize("start,count,boardable", [(540,25,False),(510,29,True)])
def test_compiler_keeps_loop_and_service_window(tmp_path,start,count,boardable):
    files={}
    def table(name,fields,rows):
        f=io.StringIO();writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows);files[name+'.txt']=f.getvalue()
    table('calendar',['service_id','monday','start_date','end_date'],[dict(service_id='s',monday=1,start_date='20260101',end_date='20261231')])
    table('routes',['route_id','route_type','route_short_name'],[dict(route_id='r',route_type=3,route_short_name='1')])
    table('stops',['stop_id','stop_name','stop_lat','stop_lon'],[dict(stop_id=k,stop_name=k,stop_lat=55,stop_lon=37+i*.01) for i,k in enumerate('ab')])
    trips=[];times=[]
    for i in range(count):
        trips.append(dict(trip_id=str(i),route_id='r',service_id='s',direction_id=0))
        for seq,stop in enumerate('aba'):
            minute=start+i*30+seq*2;t=f'{minute//60:02}:{minute%60:02}:00'
            times.append(dict(trip_id=str(i),stop_id=stop,stop_sequence=seq,arrival_time=t,departure_time=t))
    table('trips',['trip_id','route_id','service_id','direction_id'],trips)
    table('stop_times',['trip_id','stop_id','stop_sequence','arrival_time','departure_time'],times)
    archive=tmp_path/'feed.zip';output=tmp_path/'result.json'
    with zipfile.ZipFile(archive,'w') as z:
        for name,body in files.items():z.writestr(name,body)
    subprocess.run([sys.executable,'scripts/build_surface.py',str(archive),str(output)],check=True,capture_output=True)
    result=json.loads(output.read_text())
    occurrences=[s for s in result['stations'] if s['name']=='a' and not s.get('access',True)]
    assert len(occurrences)==2
    # The last departure is 21:04: it must not be boardable throughout a day ending at 22:00.
    waits=[e['minutes'] for e in result['edges'] if e['mode']=='wait']
    assert bool(waits)==boardable
    if boardable:
        assert set(waits)=={30}  # Full headway, not a misleading half-interval.
        assert {e['minutes'] for e in result['edges'] if e['mode']=='bus'}=={2}
