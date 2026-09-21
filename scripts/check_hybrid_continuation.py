#!/usr/bin/env python3
"""Check actual incumbent retention on the three frozen morning scenarios.

Each region runs a short solve, then a separate long solve seeded with that plan.
The latter budget includes its own short checkpoint; total experiment time per
region is short + long. This is not a claim about unrelated independent runs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from field_router.geo_models import GeographyBundle
from field_router.geography import attach_geography
from field_router.models import Scenario
from field_router.objectives import objective_key
from field_router.road_travel import bundle_travel
from field_router.solvers.selection import HybridSolver
from field_router.validation import require_valid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, default=ROOT / 'data/demo-road')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--short-seconds', type=float, default=3)
    parser.add_argument('--long-seconds', type=float, default=10)
    args = parser.parse_args()
    rows = []
    for region in ('Восток', 'Юго-восток', 'Югоцентр'):
        path = args.inputs / f'{region}.json'
        raw = path.read_bytes()
        payload = json.loads(raw)
        geography = GeographyBundle.model_validate(payload['geography'])
        scenario = attach_geography(Scenario.model_validate(payload['scenario']), geography)
        travel = bundle_travel(geography)
        short = require_valid(scenario, travel, HybridSolver().solve(
            scenario, travel, time_limit_seconds=args.short_seconds, seed=0))
        long = require_valid(scenario, travel, HybridSolver().solve(
            scenario, travel, time_limit_seconds=args.long_seconds, seed=0, initial_plan=short))
        short_key, long_key = objective_key(short, scenario), objective_key(long, scenario)
        checkpoint_key = tuple(long.diagnostics['checkpoint']['objective_key'])
        assert long_key <= short_key, f'{region}: lost supplied short plan'
        assert long_key <= checkpoint_key, f'{region}: lost internal checkpoint'
        row = {'region': region, 'input_sha256': hashlib.sha256(raw).hexdigest(),
               'valid': True, 'short_key': short_key, 'long_key': long_key,
               'improved': long_key < short_key,
               'short_plan': short.model_dump(mode='json'),
               'long_plan': long.model_dump(mode='json')}
        rows.append(row)
        print(f'{region}: {short_key} → {long_key}; '
              f'{short.elapsed_seconds:.3f}s + {long.elapsed_seconds:.3f}s', flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {'created_utc': datetime.now(timezone.utc).isoformat(),
              'contract': 'long retains supplied short plan and its own short checkpoint',
              'profile': 'morning', 'short_seconds': args.short_seconds,
              'long_seconds': args.long_seconds, 'seed': 0, 'rows': rows}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
