"""Isolated real-data server for the repeatable intake browser regression."""
import argparse
from copy import deepcopy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.intake_demo import build_stream_baseline, demo_requests
from field_router.scenario_server import create_server


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    baseline = build_stream_baseline('data/demo-road/Восток.json')
    server = create_server(deepcopy(baseline), args.state, port=0,
        time_limit=.15, solver='ortools', disable_gps=True,
        demo_reset_session=baseline, demo_stream_requests=demo_requests(baseline, prefix='demo-stream'))
    print(f'http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
