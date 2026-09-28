"""Synthetic, isolated day for the early-return browser check."""
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import threading
import time

from conftest import make_scenario
from test_engineer_unavailability import session_with_route, report
from field_router.auth import AccountStore
from field_router.scenario_server import create_server
from field_router.replanning import ScenarioSession


if __name__ == '__main__':
    root = Path(sys.argv[1])
    session = session_with_route(make_scenario.__wrapped__())
    scenario = session.scenario.model_copy(update={'region': 'Восток',
        'engineers': [e.model_copy(update={'region': 'Восток'}) for e in session.scenario.engineers],
        'requests': [r.model_copy(update={'region': 'Восток'}) for r in session.scenario.requests]})
    session = ScenarioSession(scenario, session.plan)
    report(session, engineer_id='e2', end_time='18:00')
    accounts = AccountStore(root/'accounts.sqlite3')
    password = os.environ['FIELD_ROUTER_ADMIN_PASSWORD']
    accounts.bootstrap(password)
    accounts.create_engineer('worker', password, 'east', 'e1')
    server = create_server(session, root/'day.json', port=0, time_limit=.1,
        accounts=accounts, disable_gps=True,
        engineer_clock_now=lambda: datetime(2026, 9, 28, 9, tzinfo=timezone.utc),
        region_sources={'east': {'label': 'Восток', 'state': root/'day.json'}})
    def stop():
        deadline = time.monotonic()+180
        while time.monotonic() < deadline and not (root/'stop').exists():
            time.sleep(.2)
        server.shutdown()
    threading.Thread(target=stop, daemon=True).start()
    print(f'http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
