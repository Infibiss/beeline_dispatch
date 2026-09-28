"""Browser fixture with a deterministic Moscow clock, synthetic people and plan."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import threading
import time

from conftest import make_scenario
from test_engineer_unavailability import session_with_route
from field_router.auth import AccountStore
from field_router.replanning import ScenarioSession
from field_router.scenario_server import create_server


if __name__ == '__main__':
    root = Path(sys.argv[1])
    clock_file = root / 'clock-minute'
    clock_file.write_text('540', encoding='ascii')
    session = session_with_route(make_scenario.__wrapped__())
    scenario = session.scenario.model_copy(update={'region': 'Восток',
        'engineers': [e.model_copy(update={'region': 'Восток'}) for e in session.scenario.engineers],
        'requests': [r.model_copy(update={'region': 'Восток'}) for r in session.scenario.requests]})
    session = ScenarioSession(scenario, session.plan)
    midnight = datetime.fromisoformat(scenario.date).replace(tzinfo=timezone(timedelta(hours=3)))
    def clock():
        return midnight + timedelta(minutes=int(clock_file.read_text(encoding='ascii')))
    accounts = AccountStore(root / 'accounts.sqlite3')
    password = os.environ['FIELD_ROUTER_ADMIN_PASSWORD']
    accounts.bootstrap(password)
    accounts.create_engineer('worker', password, 'east', 'e1')
    server = create_server(session, root / 'day.json', port=0, time_limit=.1,
        accounts=accounts, disable_gps=True, engineer_clock_now=clock,
        region_sources={'east': {'label': 'Восток', 'state': root / 'day.json'}})
    def stop():
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline and not (root / 'stop').exists():
            time.sleep(.2)
        server.shutdown()
    threading.Thread(target=stop, daemon=True).start()
    print(f'http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
