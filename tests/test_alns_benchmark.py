"""Benchmark integrity checks; these do not run time-limited solver searches."""
from collections import Counter
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def benchmark():
    path = Path(__file__).resolve().parents[1] / "scripts/benchmark_alns_upgrade.py"
    assert path.exists(), "The isolated comparison harness must exist"
    spec = importlib.util.spec_from_file_location("benchmark_alns_upgrade", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fleet_variants_are_repeatable_and_preserve_input_distributions(benchmark):
    scenario = {
        "region": "test",
        "requests": [{"duration_minutes": 80, "equipment": {"router": 1}}],
        "points": {"office": {"x_km": 0, "y_km": 0}},
        "metadata": {"engineers_source": "synthetic"},
        "engineers": [
            {"id": str(i), "skills": ["local"] if i % 2 else ["local", "emergency"],
             "transport": "car" if i % 3 else "public", "start_location": "office",
             "start_address": None, "shift": {"start": 540 + 60 * (i % 2), "end": 1080},
             "stock": {"router": i, "tv_box": 0}}
            for i in range(15)
        ],
    }
    original = deepcopy(scenario)
    first = benchmark.fleet_variant(scenario, 3)
    assert first == benchmark.fleet_variant(scenario, 3)
    assert first != benchmark.fleet_variant(scenario, 4)
    assert first != original
    assert scenario == original
    assert {k: v for k, v in first.items() if k != "engineers"} == {
        k: v for k, v in original.items() if k != "engineers"}
    for fields in (("skills",), ("transport",), ("start_location", "start_address", "shift")):
        def marginal(items):
            return Counter(json.dumps([e[f] for f in fields], sort_keys=True) for e in items)
        assert marginal(first["engineers"]) == marginal(original["engineers"])
    for old, new in zip(original["engineers"], first["engineers"]):
        assert old["id"] == new["id"]
        assert old["stock"] == new["stock"]


def test_supplied_fleet_is_not_silently_randomized(benchmark):
    with pytest.raises(ValueError, match="synthetic"):
        benchmark.fleet_variant({"metadata": {"engineers_source": "provided_json"}}, 0)


def test_jobs_pair_code_roots_and_do_not_repeat_ortools_by_seed(benchmark):
    roots = [{"label": "before"}, {"label": "after"}]
    jobs = benchmark.build_jobs([{"id": "case"}], roots, [3, 10],
                                ["ortools", "alns", "hybrid"], [0, 1, 2], ["cost"])
    assert len(jobs) == 2 * 2 * 7
    for first, second in zip(jobs[::2], jobs[1::2]):
        assert {first["code_label"], second["code_label"]} == {"before", "after"}
        assert {k: v for k, v in first.items() if k not in ("id", "code_label")} == {
            k: v for k, v in second.items() if k not in ("id", "code_label")}
    assert all(j["seed"] is None for j in jobs if j["solver"] == "ortools")
    assert len({j["id"] for j in jobs}) == len(jobs)


def test_canonical_hash_and_atomic_result_save(benchmark, tmp_path):
    assert benchmark.digest({"a": 1, "b": 2}) == benchmark.digest({"b": 2, "a": 1})
    assert benchmark.digest({"a": 1}) != benchmark.digest({"a": 2})
    target = tmp_path / "nested/results.json"
    benchmark.atomic_json(target, {"rows": [{"valid": True}]})
    assert json.loads(target.read_text()) == {"rows": [{"valid": True}]}
    benchmark.atomic_json(target, {"rows": []})
    assert json.loads(target.read_text()) == {"rows": []}
    assert list(target.parent.iterdir()) == [target]


def test_diagnostic_iteration_counts_are_kept_separately(benchmark):
    diagnostics = {"alns": {"search": {"alns": {"iterations": 13},
                                      "polish_iterations": 5, "other": "x"}}}
    assert benchmark.iteration_counts(diagnostics) == {
        "alns.search.alns.iterations": 13,
        "alns.search.polish_iterations": 5,
    }


def test_preparation_keeps_original_inputs_and_issued_stock(benchmark, tmp_path):
    from field_router.models import Scenario, Plan
    from field_router.geo_models import GeographyBundle
    from field_router.replanning import ScenarioSession

    source = Path(__file__).resolve().parents[1] / "data/demo-road/Восток.json"
    raw = source.read_bytes()
    payload = json.loads(raw)
    cases = benchmark.prepare_cases([source], ["morning", "issued"], [0], "saved", tmp_path)
    assert len(cases) == 4
    assert source.read_bytes() == raw
    saved = next(p for p in payload["plans"] if p["solver"] == "ortools")
    issued = ScenarioSession(Scenario.model_validate(payload["scenario"]),
                             Plan.model_validate(saved),
                             GeographyBundle.model_validate(payload["geography"]).model_dump()).scenario
    for case in cases:
        frozen = json.loads(Path(case["frozen_path"]).read_text())
        assert benchmark.digest(frozen["scenario"]) == case["effective_scenario_sha256"]
        assert frozen["reference"] == saved
        assert frozen["scenario"]["requests"] == json.loads(
            Path(cases[0]["frozen_path"]).read_text())["scenario"]["requests"]
        if case["stock_mode"] == "issued":
            assert [e["stock"] for e in frozen["scenario"]["engineers"]] == [
                e.stock.model_dump() for e in issued.engineers]


def test_worker_rejects_changed_code_before_importing_or_searching(benchmark, tmp_path):
    root = tmp_path / "code"
    module = root / "src/field_router/__init__.py"
    module.parent.mkdir(parents=True)
    module.write_text("raise AssertionError('Must not import changed code')\n")
    spec = {"root": {"root": str(root), "source_sha256": "wrong"},
            "case": {}, "job": {"id": "changed-code"}}
    spec_path, result_path = tmp_path / "spec.json", tmp_path / "result.json"
    benchmark.atomic_json(spec_path, spec)
    completed = subprocess.run([sys.executable, benchmark.__file__, "--worker",
                                str(spec_path), str(result_path)], capture_output=True, text=True)
    assert completed.returncode == 0
    result = json.loads(result_path.read_text())
    assert result["status"] == "worker_error"
    assert "Source code changed" in result["error"]
    assert result["valid"] is False


@pytest.mark.parametrize("profiles", [None, ["morning", "cost", "sla", "stable"]])
def test_cli_defaults_to_morning_and_accepts_explicit_day_profiles(benchmark, tmp_path, monkeypatch, profiles):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "case.json").write_text("{}")
    output = tmp_path / "manifest.json"
    monkeypatch.setattr(benchmark, "code_metadata", lambda label, root, revision: {"label": label, "root": str(root)})
    monkeypatch.setattr(benchmark, "select_code_root", lambda root: None)
    monkeypatch.setattr(benchmark, "prepare_cases", lambda *args: [{"id": "case"}])
    argv = ["--inputs", str(inputs), "--output", str(output), "--dry-run"]
    if profiles:
        argv += ["--profiles", *profiles, "--reference", "saved"]
    assert benchmark.main(argv) == 0
    manifest = json.loads(output.read_text())
    assert {job["profile"] for job in manifest["jobs"]} == set(profiles or ["morning"])
    assert len(manifest["jobs"]) == 3 * len(profiles or ["morning"])
    assert manifest["rows"] == []


@pytest.mark.parametrize("profile,solver_profile", [("morning", None), ("cost", "cost"), ("sla", "sla"), ("stable", "stable")])
def test_worker_translates_morning_to_none_for_solve_and_objective(benchmark, tmp_path, monkeypatch, profile, solver_profile):
    from field_router.models import Plan
    from field_router.solvers import ortools_solver, selection

    source = Path(__file__).resolve().parents[1] / "data/demo-road/Восток.json"
    payload = json.loads(source.read_text())
    saved = Plan.model_validate(next(p for p in payload["plans"] if p["solver"] == "ortools"))
    case = benchmark.prepare_cases([source], ["morning"], [], "saved", tmp_path)[0]
    calls = []

    def solve(scenario, travel, **kwargs):
        calls.append(("solve", kwargs["profile"]))
        return saved

    def objective(plan, scenario, **kwargs):
        calls.append(("objective", kwargs["profile"]))
        return (0, 0, 0)

    monkeypatch.setattr(selection, "get_solver", lambda name: SimpleNamespace(solve=solve))
    monkeypatch.setattr(ortools_solver, "objective_key", objective)
    monkeypatch.setattr(benchmark, "source_hash", lambda root: "unchanged")
    monkeypatch.setattr(benchmark, "select_code_root", lambda root: str(Path(root) / "src/field_router/__init__.py"))
    result = benchmark.run_worker({
        "root": {"root": str(benchmark.ROOT), "source_sha256": "unchanged"},
        "case": case,
        "job": {"id": "profile-contract", "solver": "ortools", "profile": profile, "budget_seconds": 3, "seed": None},
    })
    assert calls == [("solve", solver_profile), ("objective", solver_profile)]
    assert result["profile"] == profile
    assert result["objective_key"] == [0, 0, 0]
