"""Owned synthetic server for the real browser confirmation workflow."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import sys
import threading
import time
from field_router.auth import AccountStore
from field_router.models import Engineer,TimeWindow
from field_router.gps.test_plan import make_test_day
from field_router.scenario_server import create_server

if __name__=='__main__':
    directory=Path(sys.argv[1])
    clock=datetime.now(timezone(timedelta(hours=3))).replace(second=0,microsecond=0)
    before=clock-timedelta(minutes=2)
    origin={'lat':55.7,'lon':37.7,'accuracy_m':8.,'measured_at_ms':time.time()*1000,'provider':'gps'}
    engineer=Engineer(id='gps-browser-e1',name='Тестовый инженер',region='Восток',
                      skills=['local'],transport='car',shift=TimeWindow(start=60,end=1440))
    session=make_test_day(engineer,origin,before)
    session.record_fact({'version':session.version,'engineer_id':engineer.id,'status':'traveling',
        'request_id':'gps-test-001','location_id':'office','time':session.time,'remaining_minutes':2})
    state=directory/'state.json'
    accounts=AccountStore(directory/'accounts.sqlite3')
    accounts.bootstrap(os.environ['FIELD_ROUTER_ADMIN_PASSWORD'])
    server=create_server(session,state,port=0,time_limit=.1,solver='ortools',accounts=accounts,
                         region_sources={'east':{'label':'Восток','state':state}})
    server.gps.service.ingest_native(engineer.id,'native-browser-quality',origin)
    def monitor():
        stop=Path(os.environ['GPS_TEST_STOP_FILE']);deadline=time.monotonic()+90
        while time.monotonic()<deadline and not stop.exists():time.sleep(.2)
        server.shutdown()
    threading.Thread(target=monitor,daemon=True).start()
    print(f'http://127.0.0.1:{server.server_port}',flush=True)
    try:server.serve_forever()
    finally:server.server_close()
