import pytest

from conftest import engineer, job
from field_router.models import TimeWindow, Transport
from field_router.risk_engine import (
    LossWeights,
    RISK_MODEL_VERSION,
    RiskEngine,
    RiskEngineConfig,
    ServiceDurationEstimator,
    ServiceDurationPrior,
    UI_RERANKING_CONFIG,
    analyze_plan_risk,
    application_risk_config,
    application_risk_seed,
    application_service_group,
    estimate_service_duration,
)
from field_router.solvers.baseline import BaselineSolver
from field_router.travel import MatrixTravel


def _travel(scenario, minutes=10):
    ids = ["office"] + [request.id for request in scenario.requests]
    matrix = [[0 if left == right else minutes for right in range(len(ids))] for left in range(len(ids))]
    return MatrixTravel(
        ids,
        {transport: matrix for transport in Transport},
        {transport: matrix for transport in Transport},
    )


def _two_visit_plan(make_scenario, *, shift_end=635):
    scenario = make_scenario(
        [
            job("a", start=550, end=590, duration=30),
            job("b", start=600, end=630, duration=30),
        ],
        [engineer(start=540, end=shift_end)],
    )
    plan = BaselineSolver().solve(scenario, _travel(scenario))
    assert [(visit.start, visit.end) for visit in plan.routes[0].visits] == [(550, 580), (600, 630)]
    return scenario, plan


def _emergency_plan(
    make_scenario,
    *,
    priority="urgent",
    provenance=None,
    first_duration=101,
):
    scenario = make_scenario(
        [
            job("first", start=550, end=800, duration=first_duration),
            job(
                "emergency",
                start=540,
                end=900,
                duration=10,
                priority=priority,
                provenance=provenance or {},
            ),
        ],
        [engineer(start=540, end=1_000)],
    )
    plan = BaselineSolver().solve(scenario, _travel(scenario))
    assert plan.routes[0].visits[1].start == 560 + first_duration
    return scenario, plan


def test_service_quantiles_use_nominal_as_p50_and_are_monotonic():
    request = job("a", duration=60)
    estimate = estimate_service_duration(
        request,
        prior=ServiceDurationPrior(coefficient_of_variation=0.25),
    )
    assert estimate.p50_minutes == pytest.approx(60)
    assert estimate.p50_minutes < estimate.p80_minutes < estimate.p90_minutes
    assert estimate.observation_count == 0
    assert estimate.source == "prior"


def test_empirical_observation_hook_shrinks_a_named_group():
    request = job("a", duration=60, bk_type="install")
    estimator = ServiceDurationEstimator(
        prior=ServiceDurationPrior(coefficient_of_variation=0.20, prior_strength=1),
        observations={"install": [90, 90, 90]},
        group_key=lambda item: item.bk_type,
    )
    estimate = estimator.estimate(request)
    assert 60 < estimate.p50_minutes < 90
    assert estimate.observation_count == 3
    assert estimate.group_key == "install"
    assert estimate.source == "prior+empirical"


def test_application_profile_is_versioned_reproducible_and_uses_conditional_loss_units(
    make_scenario,
):
    scenario, plan = _two_visit_plan(make_scenario)
    config = application_risk_config("batch:42")
    assert config.iterations == 600
    assert config.seed == "batch:42"
    assert config.model_version == RISK_MODEL_VERSION
    assert config.loss_unit == "conditional_loss_units"
    assert config.loss_weights == LossWeights(
        late_start=0.25,
        sla_breach=10.0,
        overtime_minute=0.05,
    )
    assert UI_RERANKING_CONFIG.iterations == 600

    first = RiskEngine(config).analyze(plan, scenario)
    second = RiskEngine(application_risk_config("batch:42")).analyze(plan, scenario)
    assert first == second
    payload = first.to_dict()
    assert payload["model_version"] == RISK_MODEL_VERSION
    assert payload["calibrated"] is False
    assert payload["loss_unit"] == "conditional_loss_units"
    assert payload["loss_weights"]["sla_breach"] == 10.0


def test_application_service_group_pools_equivalent_work_not_request_ids():
    first = job("one", bk_type="Install", hd_type="Router")
    second = job("two", bk_type="Install", hd_type="Router")
    different = job("three", bk_type="Repair", hd_type="Router")
    assert application_service_group(first) == application_service_group(second)
    assert application_service_group(first) != application_service_group(different)
    assert "one" not in application_service_group(first)


def test_application_seed_tracks_operational_inputs_not_scenario_lineage(make_scenario):
    scenario, plan = _two_visit_plan(make_scenario)
    renamed = scenario.model_copy(update={"id": "same-day/opaque-runtime-id"})
    renamed_plan = plan.model_copy(update={"scenario_id": renamed.id})
    seed = application_risk_seed(plan, scenario, context={"surface": "approved-plan"})
    assert seed == application_risk_seed(
        renamed_plan, renamed, context={"surface": "approved-plan"}
    )
    changed = renamed.model_copy(update={"requests": [
        request.model_copy(update={"duration_minutes": request.duration_minutes + 1})
        if request.id == "a" else request
        for request in renamed.requests
    ]})
    assert seed != application_risk_seed(
        renamed_plan, changed, context={"surface": "approved-plan"}
    )


def test_zero_variation_replays_the_finished_plan_without_inventing_risk(make_scenario):
    scenario, plan = _two_visit_plan(make_scenario, shift_end=630)
    config = RiskEngineConfig(
        iterations=25,
        seed="exact",
        travel_coefficient_of_variation=0,
        service_prior=ServiceDurationPrior(coefficient_of_variation=0),
    )
    risk = analyze_plan_risk(plan, scenario, config=config)
    assert risk.p_late_start == 0
    assert risk.p_sla_breach == 0
    assert risk.p_overtime == 0
    assert risk.expected_loss == 0
    assert risk.expected_affected_customers == 0
    assert risk.customers_at_risk == ()
    assert [
        (
            visit.service_duration.p50_minutes,
            visit.service_duration.p80_minutes,
            visit.service_duration.p90_minutes,
        )
        for visit in risk.visits
    ] == [(30, 30, 30), (30, 30, 30)]


def test_monte_carlo_is_reproducible_read_only_and_propagates_route_delay(make_scenario):
    scenario, plan = _two_visit_plan(make_scenario)
    scenario_before = scenario.model_dump()
    plan_before = plan.model_dump()
    config = RiskEngineConfig(
        iterations=1_500,
        seed=417,
        travel_coefficient_of_variation=0.35,
        service_prior=ServiceDurationPrior(coefficient_of_variation=0.35),
        loss_weights=LossWeights(late_start=2, sla_breach=10, overtime_minute=0.5),
    )

    first = RiskEngine(config).analyze(plan, scenario)
    second = RiskEngine(config).analyze(plan, scenario)

    assert first == second
    assert scenario.model_dump() == scenario_before
    assert plan.model_dump() == plan_before
    route = first.routes[0]
    assert 0 < route.p_late_start <= 1
    assert 0 < route.p_sla_breach < 1
    assert 0 < route.p_overtime < 1
    assert route.p_late_start >= max(visit.p_late_start for visit in route.visits)
    assert route.p_sla_breach >= max(visit.p_sla_breach for visit in route.visits)
    assert route.expected_loss == pytest.approx(sum(visit.expected_loss for visit in route.visits))
    assert first.expected_affected_customers == pytest.approx(
        sum(visit.p_sla_breach for visit in first.visits)
    )
    assert first.affected_customer_count == len(first.customers_at_risk)
    assert first.to_dict()["iterations"] == 1_500


def test_later_declared_window_can_absorb_a_delay_without_sla_breach(make_scenario):
    request = job("a", start=550, end=550, duration=10).model_copy(
        update={"windows": [TimeWindow(start=550, end=550), TimeWindow(start=560, end=900)]}
    )
    scenario = make_scenario([request], [engineer(start=540, end=950)])
    plan = BaselineSolver().solve(scenario, _travel(scenario))
    config = RiskEngineConfig(
        iterations=400,
        seed="two-windows",
        travel_coefficient_of_variation=0.50,
        service_prior=ServiceDurationPrior(coefficient_of_variation=0),
    )
    visit = analyze_plan_risk(plan, scenario, config=config).visits[0]
    assert visit.p_late_start > 0
    assert visit.p_sla_breach == 0
    assert visit.planned_sla_deadline == 550


def test_emergency_response_sla_uses_traceable_received_at_and_combines_with_window_sla(
    make_scenario,
):
    scenario, plan = _emergency_plan(
        make_scenario,
        provenance={"received_at": "540"},
    )
    config = RiskEngineConfig(
        iterations=20,
        seed="emergency-boundary",
        travel_coefficient_of_variation=0,
        service_prior=ServiceDurationPrior(coefficient_of_variation=0),
        loss_weights=LossWeights(sla_breach=7),
    )
    risk = RiskEngine(config).analyze(plan, scenario)
    visit = risk.visits[1]
    assert visit.emergency_response_sla_deadline == 660
    assert visit.p_window_sla_breach == 0
    assert visit.p_emergency_response_sla_breach == 1
    assert visit.p_sla_breach == 1
    assert visit.expected_loss == 7
    assert risk.routes[0].p_emergency_response_sla_breach == 1
    assert risk.p_emergency_response_sla_breach == 1
    assert risk.emergency_response_sla_visit_count == 1
    assert risk.expected_affected_customers == 1
    assert risk.customers_at_risk == ("emergency",)


def test_emergency_response_exact_120_minute_boundary_meets_sla(make_scenario):
    scenario, plan = _emergency_plan(
        make_scenario,
        provenance={"received_at": "540"},
        first_duration=100,
    )
    risk = RiskEngine(
        RiskEngineConfig(
            iterations=10,
            travel_coefficient_of_variation=0,
            service_prior=ServiceDurationPrior(coefficient_of_variation=0),
        )
    ).analyze(plan, scenario)
    visit = risk.visits[1]
    assert visit.planned_start == visit.emergency_response_sla_deadline == 660
    assert visit.p_emergency_response_sla_breach == 0
    assert visit.p_sla_breach == 0


@pytest.mark.parametrize(
    ("priority", "provenance"),
    [
        ("urgent", {}),
        ("normal", {"received_at": "540"}),
    ],
)
def test_emergency_response_sla_is_absent_without_both_urgent_and_received_at(
    make_scenario,
    priority,
    provenance,
):
    scenario, plan = _emergency_plan(
        make_scenario,
        priority=priority,
        provenance=provenance,
    )
    risk = RiskEngine(
        RiskEngineConfig(
            iterations=10,
            travel_coefficient_of_variation=0,
            service_prior=ServiceDurationPrior(coefficient_of_variation=0),
        )
    ).analyze(plan, scenario)
    visit = risk.visits[1]
    assert visit.emergency_response_sla_deadline is None
    assert visit.p_emergency_response_sla_breach is None
    assert visit.p_sla_breach == 0
    assert risk.p_emergency_response_sla_breach is None
    assert risk.emergency_response_sla_visit_count == 0


def test_malformed_emergency_received_at_fails_closed(make_scenario):
    scenario, plan = _emergency_plan(
        make_scenario,
        provenance={"received_at": "not-a-minute"},
    )
    with pytest.raises(ValueError, match="invalid provenance.received_at"):
        RiskEngine(RiskEngineConfig(iterations=10)).analyze(plan, scenario)


def test_empirical_service_data_changes_both_quantiles_and_downstream_risk(make_scenario):
    scenario, plan = _two_visit_plan(make_scenario)
    config = RiskEngineConfig(
        iterations=1_000,
        seed=22,
        travel_coefficient_of_variation=0,
        service_prior=ServiceDurationPrior(coefficient_of_variation=0.05, prior_strength=1),
    )
    baseline = RiskEngine(config).analyze(plan, scenario)
    empirical = RiskEngine(
        config,
        service_observations={"a": [75, 80, 90]},
    ).analyze(plan, scenario)
    assert empirical.visits[0].service_duration.p50_minutes > baseline.visits[0].service_duration.p50_minutes
    assert empirical.visits[1].p_sla_breach > baseline.visits[1].p_sla_breach


def test_unknown_plan_reference_fails_closed(make_scenario):
    scenario, plan = _two_visit_plan(make_scenario)
    broken_visit = plan.routes[0].visits[0].model_copy(update={"request_id": "missing"})
    broken_route = plan.routes[0].model_copy(update={"visits": [broken_visit]})
    broken_plan = plan.model_copy(update={"routes": [broken_route]})
    with pytest.raises(ValueError, match="unknown request"):
        RiskEngine(RiskEngineConfig(iterations=10)).analyze(broken_plan, scenario)


@pytest.mark.parametrize(
    "config",
    [
        RiskEngineConfig,
        lambda: RiskEngineConfig(iterations=0),
        lambda: RiskEngineConfig(travel_coefficient_of_variation=-0.1),
    ],
)
def test_config_validation_smoke(config):
    if config is RiskEngineConfig:
        assert config().iterations > 0
    else:
        with pytest.raises(ValueError):
            config()
