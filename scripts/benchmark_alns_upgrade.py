#!/usr/bin/env python3
"""Compare code revisions in sequential, isolated processes on frozen inputs.

Defaults are a small morning sweep (3 seconds, seed 0, original fleet). Use
--seconds 3 10 --seeds 0 1 2 --stock morning issued for the full stock sweep.
--fleet-seeds without values adds five variants; explicit values select a subset.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import platform
import random
import subprocess
import sys
import tempfile
from time import perf_counter, process_time
import traceback

ROOT = Path(__file__).resolve().parents[1]
PRIORITIES = ("urgent", "high", "normal")


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def fleet_variant(scenario, seed):
    """Permute observed attributes without changing marginal distributions.

    Equipment stays with engineer IDs. Start location/address/shift travel as a
    bundle, so no new coordinates, shift bounds or start-address mappings arise.
    """
    if scenario.get("metadata", {}).get("engineers_source") != "synthetic":
        raise ValueError("--fleet-seeds requires a fleet labelled synthetic")
    result = deepcopy(scenario)
    engineers = result["engineers"]
    for fields in (("skills",), ("transport",), ("start_location", "start_address", "shift")):
        attributes = [{key: engineer[key] for key in fields} for engineer in engineers]
        rng = random.Random(digest([scenario["region"], seed, fields]))
        rng.shuffle(attributes)
        for engineer, attributes_for_engineer in zip(engineers, attributes):
            engineer.update(attributes_for_engineer)
    return result


def iteration_counts(value, prefix=""):
    counts = {}
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else key
            if "iterations" in key and isinstance(child, (int, float)):
                counts[path] = child
            elif isinstance(child, (dict, list)):
                counts.update(iteration_counts(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            counts.update(iteration_counts(child, f"{prefix}[{index}]"))
    return counts


def build_jobs(cases, roots, budgets, solvers, seeds, profiles):
    jobs = []
    pair_index = 0
    for case in cases:
        for budget in budgets:
            for profile in profiles:
                for solver in solvers:
                    for seed in ([None] if solver == "ortools" else seeds):
                        # Alternate order within pairs to reduce systematic drift.
                        order = roots if pair_index % 2 == 0 else list(reversed(roots))
                        pair_index += 1
                        for root in order:
                            job = {"case_id": case["id"], "code_label": root["label"],
                                   "budget_seconds": budget, "solver": solver,
                                   "seed": seed, "profile": profile}
                            jobs.append({"id": digest(job)[:20], **job})
    return jobs


def source_hash(root):
    source = Path(root) / "src/field_router"
    files = sorted(source.rglob("*.py"))
    if not files:
        raise ValueError(f"No field_router Python sources in {source}")
    return digest({str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in files})


def code_metadata(label, root, claimed_revision=None):
    root = Path(root).resolve()
    git = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=False)
    revision = git.stdout.strip() if git.returncode == 0 else None
    return {"label": label, "root": str(root), "git_revision": revision,
            "claimed_revision": claimed_revision, "source_sha256": source_hash(root)}


def select_code_root(root):
    source = str(Path(root) / "src")
    sys.path.insert(0, source)
    import field_router
    actual = Path(field_router.__file__).resolve()
    if not actual.is_relative_to(Path(root).resolve()):
        raise RuntimeError(f"Wrong module root: requested {root}, imported {actual}")
    return str(actual)


def prepare_cases(sources, stocks, fleet_seeds, reference_mode, case_dir):
    from field_router.models import Plan, Scenario
    from field_router.geo_models import GeographyBundle
    from field_router.geography import attach_geography
    from field_router.replanning import ScenarioSession

    cases = []
    for source in sources:
        raw = source.read_bytes()
        payload = json.loads(raw)
        geography = GeographyBundle.model_validate(payload["geography"])
        original = attach_geography(Scenario.model_validate(payload["scenario"]), geography)
        saved = next((p for p in payload.get("plans", []) if p["solver"] == "ortools"), None)
        if ("issued" in stocks or reference_mode == "saved") and saved is None:
            raise ValueError(f"{source}: saved OR-Tools plan required")
        for stock in stocks:
            scenario = original
            if stock == "issued":
                scenario = ScenarioSession(original, Plan.model_validate(saved),
                                           geography.model_dump()).scenario
            for fleet_seed in [None, *fleet_seeds]:
                effective = scenario.model_dump(mode="json")
                if fleet_seed is not None:
                    effective = fleet_variant(effective, fleet_seed)
                # Validate once during preparation, then reuse these exact bytes.
                effective = Scenario.model_validate(effective).model_dump(mode="json")
                reference = saved if reference_mode == "saved" else None
                frozen = {"scenario": effective, "reference": reference}
                fingerprint = digest(effective)
                case = {"source_path": str(source),
                        "source_sha256": hashlib.sha256(raw).hexdigest(),
                        "geography_sha256": digest(payload["geography"]),
                        "effective_scenario_sha256": fingerprint,
                        "reference_sha256": digest(reference), "region": scenario.region,
                        "stock_mode": stock, "fleet_seed": fleet_seed,
                        "reference_mode": reference_mode,
                        "requests": len(scenario.requests), "engineers": len(scenario.engineers),
                        "fixed_stock": sum(e["stock"] is not None for e in effective["engineers"])}
                case["id"] = digest(case)[:20]
                case["frozen_path"] = str(case_dir / f"{case['id']}.json")
                case["frozen_sha256"] = digest(frozen)
                atomic_json(case["frozen_path"], frozen)
                cases.append(case)
    return cases


def run_worker(spec):
    """Load one version in a fresh process; setup/imports are outside solve time."""
    worker_started, worker_cpu = perf_counter(), process_time()
    root, case, job = spec["root"], spec["case"], spec["job"]
    if source_hash(root["root"]) != root["source_sha256"]:
        raise ValueError("Source code changed after benchmark preparation")
    module_path = select_code_root(root["root"])
    from field_router.models import Plan, Scenario
    from field_router.geo_models import GeographyBundle
    from field_router.road_travel import bundle_travel
    from field_router.solvers.selection import get_solver
    from field_router.solvers.ortools_solver import objective_key
    from field_router.validation import validate_plan
    from field_router._alns import solver as _warm_import  # noqa: F401

    raw = Path(case["source_path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != case["source_sha256"]:
        raise ValueError("Source input changed after benchmark preparation")
    payload = json.loads(raw)
    frozen = json.loads(Path(case["frozen_path"]).read_text(encoding="utf-8"))
    if digest(frozen) != case["frozen_sha256"]:
        raise ValueError("Frozen scenario/reference changed")
    scenario = Scenario.model_validate(frozen["scenario"])
    if digest(scenario.model_dump(mode="json")) != case["effective_scenario_sha256"]:
        raise ValueError("Version changed the effective scenario while loading")
    reference = Plan.model_validate(frozen["reference"]) if frozen["reference"] else None
    travel = bundle_travel(GeographyBundle.model_validate(payload["geography"]))
    solver = get_solver(job["solver"])
    kwargs = {"time_limit_seconds": job["budget_seconds"],
              "profile": job["profile"], "reference": reference}
    if job["seed"] is not None:
        kwargs["seed"] = job["seed"]
    started, cpu = perf_counter(), process_time()
    plan = solver.solve(scenario, travel, **kwargs)
    elapsed, cpu_elapsed = perf_counter() - started, process_time() - cpu
    errors = validate_plan(scenario, travel, plan)
    missing = {u.request_id for u in plan.unassigned}
    row = {**job, **{key: case[key] for key in (
        "region", "source_path", "source_sha256", "effective_scenario_sha256",
        "geography_sha256", "reference_sha256", "stock_mode", "fleet_seed", "reference_mode")},
        "code": root, "module_path": module_path,
        "elapsed_seconds": elapsed, "process_cpu_seconds": cpu_elapsed,
        "solver_elapsed_seconds": plan.elapsed_seconds,
        "valid": not errors, "validation_errors": errors,
        "status": plan.status, "objective_key": list(objective_key(
            plan, scenario, profile=job["profile"], reference=reference)),
        "metrics": plan.metrics.model_dump(), "distance_km": plan.metrics.distance_m / 1000,
        "unassigned": sorted(missing),
        "assigned_by_priority": {p: sum(r.priority == p and r.id not in missing
                                         for r in scenario.requests) for p in PRIORITIES},
        "total_by_priority": {p: sum(r.priority == p for r in scenario.requests)
                              for p in PRIORITIES},
        "iterations": iteration_counts(plan.diagnostics), "diagnostics": plan.diagnostics,
        "plan": plan.model_dump(mode="json"),
        "reference_validation_errors": validate_plan(scenario, travel, reference)
            if reference is not None else None,
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "packages": {name: version(name) for name in ("ortools", "numpy", "pydantic")}},
        "worker_elapsed_seconds": perf_counter() - worker_started,
        "worker_process_cpu_seconds": process_time() - worker_cpu}
    if source_hash(root["root"]) != root["source_sha256"]:
        raise ValueError("Source code changed during a measured run")
    return row


def worker_main(spec_path, result_path):
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    try:
        result = run_worker(spec)
    except Exception as exc:
        result = {**spec["job"], "valid": False, "status": "worker_error",
                  "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
    atomic_json(result_path, result)


def parse_named(values, option):
    result = {}
    for value in values:
        label, sep, content = value.partition("=")
        if not sep or not label or not content or label in result:
            raise ValueError(f"{option}: use unique LABEL=VALUE entries")
        result[label] = content
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-root", action="append", default=[], metavar="LABEL=PATH")
    parser.add_argument("--revision", action="append", default=[], metavar="LABEL=REV",
                        help="Optional provenance for archived roots without Git metadata")
    parser.add_argument("--inputs", type=Path, default=ROOT / "data/demo-road")
    parser.add_argument("--regions", nargs="+", help="Exact region names or JSON file stems")
    parser.add_argument("--seconds", type=float, nargs="+", default=[3])
    parser.add_argument("--seeds", type=int, nargs="+", default=[0])
    parser.add_argument("--solvers", nargs="+", choices=["ortools", "alns", "hybrid"],
                        default=["ortools", "alns", "hybrid"])
    parser.add_argument("--stock", nargs="+", choices=["morning", "issued"], default=["morning"])
    parser.add_argument("--fleet-seeds", type=int, nargs="*", default=None,
                        help="Add modeled-fleet variants; bare flag uses seeds 0 through 4")
    parser.add_argument("--reference", choices=["none", "saved"], default="none")
    parser.add_argument("--profiles", nargs="+", choices=["cost", "sla", "stable"], default=["cost"])
    parser.add_argument("--output", type=Path, default=ROOT / "docs/reviews/alns-upgrade.json")
    parser.add_argument("--resume", action="store_true", help="Resume an identical saved run, including errors")
    parser.add_argument("--dry-run", action="store_true", help="Freeze cases and save job manifest without solving")
    parser.add_argument("--worker", nargs=2, metavar=("SPEC", "RESULT"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        worker_main(*args.worker)
        return 0
    if any(not math.isfinite(seconds) or seconds <= 0 for seconds in args.seconds):
        parser.error("--seconds values must be finite and positive")
    if "stable" in args.profiles and args.reference != "saved":
        parser.error("--profiles stable requires --reference saved")
    for name in ("seconds", "seeds", "solvers", "stock", "profiles"):
        if len(getattr(args, name)) != len(set(getattr(args, name))):
            parser.error(f"--{name} cannot contain duplicate values")
    roots_arg = parse_named(args.code_root or [f"current={ROOT}"], "--code-root")
    revisions = parse_named(args.revision, "--revision")
    if set(revisions) - set(roots_arg):
        parser.error("--revision labels must match --code-root labels")
    roots = [code_metadata(label, path, revisions.get(label)) for label, path in roots_arg.items()]
    fleet_seeds = [] if args.fleet_seeds is None else (args.fleet_seeds or list(range(5)))
    if len(fleet_seeds) != len(set(fleet_seeds)):
        parser.error("--fleet-seeds cannot contain duplicate values")
    sources = sorted(args.inputs.resolve().glob("*.json"))
    if args.regions:
        sources = [p for p in sources if p.stem in args.regions]
        missing = set(args.regions) - {p.stem for p in sources}
        if missing:
            parser.error(f"Unknown regions: {sorted(missing)}")
    if not sources:
        parser.error("No input JSON files selected")
    output = args.output.resolve()
    if output.exists() and not args.resume:
        parser.error("Output exists; use a new --output or --resume")
    select_code_root(roots[0]["root"])
    cases = prepare_cases(sources, args.stock, fleet_seeds, args.reference,
                          output.parent / f"{output.stem}-cases")
    jobs = build_jobs(cases, roots, args.seconds, args.solvers, args.seeds, args.profiles)
    config = {"roots": roots, "cases": cases, "jobs": jobs,
              "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    run = {"format_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
           "configuration_sha256": digest(config), **config,
           "preparation_code_root": roots[0], "rows": [],
           "methodology": {
               "execution": "One fresh subprocess at a time; code order alternates within paired jobs.",
               "timing": "Solve wall and process CPU exclude imports/setup/validation; process CPU includes threads.",
               "objective": "miss urgent, miss urgent+high, miss total, profile terms, engineers, metres",
               "reference": "Same saved OR-Tools plan if requested; no simulated events or completed jobs.",
               "fleets": "Independent permutations preserve skills, modes and start/shift bundle marginals; stock stays with engineer IDs.",
               "issued": "Stock from original published plan, fixed before fleet variants. Variants are a search stress, not an executed workday.",
               "seeds": "OR-Tools once per case/budget/profile; wall-clock stopping prevents bitwise determinism.",
               "bounds": "No optimality claim; each returned plan independently validated.",
           }}
    if args.resume and output.exists():
        previous = json.loads(output.read_text(encoding="utf-8"))
        if previous.get("configuration_sha256") != run["configuration_sha256"]:
            parser.error("Resume configuration/code/inputs differ from saved manifest")
        run = previous
    atomic_json(output, run)
    planned_seconds = sum(j["budget_seconds"] for j in jobs)
    print(f"{len(cases)} cases, {len(jobs)} jobs, {planned_seconds:.0f}s nominal search + setup overhead.", flush=True)
    if args.dry_run:
        return 0
    by_case = {case["id"]: case for case in cases}
    by_root = {root["label"]: root for root in roots}
    done = {row["id"] for row in run["rows"]}
    environment = {**os.environ, "PYTHONHASHSEED": "0", "OMP_NUM_THREADS": "1",
                   "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
    with tempfile.TemporaryDirectory(prefix="alns-benchmark-") as temporary:
        spec_path, result_path = Path(temporary) / "spec.json", Path(temporary) / "result.json"
        for job in jobs:
            if job["id"] in done:
                continue
            case, root = by_case[job["case_id"]], by_root[job["code_label"]]
            atomic_json(spec_path, {"case": case, "root": root, "job": job})
            result_path.unlink(missing_ok=True)
            started = perf_counter()
            try:
                completed = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), "--worker", str(spec_path), str(result_path)],
                    cwd=root["root"], env=environment, capture_output=True, text=True,
                    timeout=job["budget_seconds"] * 4 + 60, check=False)
                if result_path.exists():
                    row = json.loads(result_path.read_text(encoding="utf-8"))
                else:
                    row = {**job, "valid": False, "status": "worker_crash"}
                row.update({"worker_returncode": completed.returncode,
                            "worker_stdout": completed.stdout, "worker_stderr": completed.stderr})
            except subprocess.TimeoutExpired as exc:
                row = {**job, "valid": False, "status": "worker_timeout", "error": str(exc)}
            row.update({"code": root, "region": case["region"], "stock_mode": case["stock_mode"],
                        "fleet_seed": case["fleet_seed"],
                        "effective_scenario_sha256": case["effective_scenario_sha256"],
                        "subprocess_elapsed_seconds": perf_counter() - started})
            run["rows"].append(row)
            run["updated_at"] = datetime.now(timezone.utc).isoformat()
            atomic_json(output, run)
            print(json.dumps({key: row.get(key) for key in (
                "region", "stock_mode", "fleet_seed", "code_label", "solver", "seed",
                "budget_seconds", "profile", "valid", "objective_key", "elapsed_seconds",
                "process_cpu_seconds", "status")}, ensure_ascii=False), flush=True)
    return 0 if all(row["valid"] for row in run["rows"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
