"""Deterministic, read-only risk estimates for an already constructed plan.

The planner treats request windows as allowed *start* intervals and the engineer
shift end as a hard completion boundary.  This module keeps those semantics but
does not participate in planning or validation.  It treats planned travel and
``Request.duration_minutes`` as P50 values, samples positive log-normal
durations, and propagates delay through every visit in route order.

All monetary/business consequences are deliberately caller supplied through
``LossWeights``.  With the defaults, one SLA breach equals one abstract loss
unit; no currency is implied.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import random
from statistics import NormalDist
from typing import Any

from .models import Engineer, Plan, Request, Route, Scenario


ServiceGroupKey = Callable[[Request], str]

RISK_MODEL_VERSION = "risk-engine-v1.1"
EMERGENCY_RESPONSE_TARGET_MINUTES = 120.0

_NORMAL = NormalDist()
_Z80 = _NORMAL.inv_cdf(0.80)
_Z90 = _NORMAL.inv_cdf(0.90)


def _is_finite_non_negative(value: float) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except (TypeError, ValueError):
        return False


def _is_finite_positive(value: float) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value > 0
    except (TypeError, ValueError):
        return False


def _log_sigma(coefficient_of_variation: float) -> float:
    """Convert a log-normal coefficient of variation to log-space sigma."""
    return math.sqrt(math.log1p(coefficient_of_variation**2))


def _probability(count: int, iterations: int) -> float:
    return count / iterations


@dataclass(frozen=True, slots=True)
class ServiceDurationPrior:
    """Transparent prior for a request's positive service duration.

    ``Request.duration_minutes`` is the prior median (P50).
    ``coefficient_of_variation`` controls prior dispersion.  Observations are
    pooled with the prior in log space, where ``prior_strength`` is the number
    of equivalent observations represented by the prior.  ``min_log_sigma``
    can retain a floor of uncertainty after many identical observations.
    """

    coefficient_of_variation: float = 0.30
    prior_strength: float = 5.0
    min_log_sigma: float = 0.0

    def __post_init__(self) -> None:
        if not _is_finite_non_negative(self.coefficient_of_variation):
            raise ValueError("coefficient_of_variation must be finite and non-negative")
        if not _is_finite_positive(self.prior_strength):
            raise ValueError("prior_strength must be finite and positive")
        if not _is_finite_non_negative(self.min_log_sigma):
            raise ValueError("min_log_sigma must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class LossWeights:
    """Business loss assigned to simulated outcomes, in caller-defined units."""

    late_start: float = 0.0
    sla_breach: float = 1.0
    overtime_minute: float = 0.0

    def __post_init__(self) -> None:
        for name in ("late_start", "sla_breach", "overtime_minute"):
            if not _is_finite_non_negative(getattr(self, name)):
                raise ValueError(f"{name} loss weight must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class RiskEngineConfig:
    """Monte Carlo controls.

    A fresh pseudo-random generator is derived from ``seed`` for each route, so
    repeated analysis of the same plan is reproducible and route ordering does
    not change an individual route's estimates.
    """

    iterations: int = 2_000
    seed: int | str = 0
    model_version: str = RISK_MODEL_VERSION
    calibrated: bool = False
    loss_unit: str = "abstract_loss_units"
    travel_coefficient_of_variation: float = 0.20
    late_start_tolerance_minutes: float = 0.0
    sla_tolerance_minutes: float = 0.0
    overtime_tolerance_minutes: float = 0.0
    emergency_response_target_minutes: float = EMERGENCY_RESPONSE_TARGET_MINUTES
    service_prior: ServiceDurationPrior = field(default_factory=ServiceDurationPrior)
    loss_weights: LossWeights = field(default_factory=LossWeights)

    def __post_init__(self) -> None:
        if isinstance(self.iterations, bool) or not isinstance(self.iterations, int) or self.iterations <= 0:
            raise ValueError("iterations must be a positive integer")
        if not isinstance(self.seed, (int, str)) or isinstance(self.seed, bool):
            raise ValueError("seed must be an int or str")
        if not isinstance(self.model_version, str) or not self.model_version.strip():
            raise ValueError("model_version must be a non-empty string")
        if type(self.calibrated) is not bool:
            raise ValueError("calibrated must be a bool")
        if not isinstance(self.loss_unit, str) or not self.loss_unit.strip():
            raise ValueError("loss_unit must be a non-empty string")
        for name in (
            "travel_coefficient_of_variation",
            "late_start_tolerance_minutes",
            "sla_tolerance_minutes",
            "overtime_tolerance_minutes",
            "emergency_response_target_minutes",
        ):
            if not _is_finite_non_negative(getattr(self, name)):
                raise ValueError(f"{name} must be finite and non-negative")


def application_risk_config(
    seed: int | str,
    iterations: int = 600,
) -> RiskEngineConfig:
    """Return the explicit dispatcher/reranking profile.

    The caller supplies a stable context seed (for example a plan fingerprint),
    so results are repeatable but independent plans need not share one random
    stream.  Loss values are dimensionless trade-off weights, never roubles or
    a claim about observed business impact.
    """
    return RiskEngineConfig(
        iterations=iterations,
        seed=seed,
        model_version=RISK_MODEL_VERSION,
        calibrated=False,
        loss_unit="conditional_loss_units",
        travel_coefficient_of_variation=0.20,
        service_prior=ServiceDurationPrior(
            coefficient_of_variation=0.30,
            prior_strength=5.0,
        ),
        loss_weights=LossWeights(
            late_start=0.25,
            sla_breach=10.0,
            overtime_minute=0.05,
        ),
    )


def application_service_group(request: Request) -> str:
    """Stable application grouping for future empirical service observations."""
    return "|".join(
        (
            request.bk_type.strip() or "-",
            request.hd_type.strip() or "-",
            request.skill.value,
        )
    )


def application_risk_seed(
    plan: Plan,
    scenario: Scenario,
    *,
    context: Mapping[str, Any] | None = None,
) -> str:
    """Fingerprint only operational inputs that can affect a risk run.

    Runtime identifiers, wall-clock timestamps and solver diagnostics are not
    part of the seed.  Repeating the same calculation therefore reuses the
    same Monte Carlo streams, while a changed plan, request, shift or explicit
    event context produces a different fingerprint.
    """
    routes = [
        {
            "engineer_id": route.engineer_id,
            "visits": [
                {
                    key: getattr(visit, key)
                    for key in (
                        "request_id",
                        "arrival",
                        "start",
                        "end",
                        "travel_minutes",
                        "distance_m",
                    )
                }
                for visit in route.visits
            ],
        }
        for route in sorted(plan.routes, key=lambda item: item.engineer_id)
    ]
    payload = {
        "model_version": RISK_MODEL_VERSION,
        "scenario": {
            "region": scenario.region,
            "date": scenario.date,
            "requests": [
                request.model_dump(mode="json")
                for request in sorted(scenario.requests, key=lambda item: item.id)
            ],
            "engineers": [
                {
                    "id": engineer.id,
                    "shift": engineer.shift.model_dump(mode="json"),
                }
                for engineer in sorted(scenario.engineers, key=lambda item: item.id)
            ],
        },
        "plan": {
            "routes": routes,
            "unassigned": sorted(item.request_id for item in plan.unassigned),
        },
        "context": dict(context or {}),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# Convenient fixed profile for UI call sites that do not have a contextual seed.
# Candidate comparison should prefer application_risk_config(shared_batch_seed)
# so every candidate is evaluated with the same deterministic seed.
UI_RERANKING_CONFIG = application_risk_config("field-router-ui-reranking-v1.1")


@dataclass(frozen=True, slots=True)
class DurationEstimate:
    """Analytic service-duration quantiles for one request/group."""

    nominal_minutes: float
    p50_minutes: float
    p80_minutes: float
    p90_minutes: float
    log_sigma: float
    observation_count: int
    source: str
    group_key: str


@dataclass(frozen=True, slots=True)
class VisitRisk:
    request_id: str
    engineer_id: str
    planned_start: int
    planned_sla_deadline: int
    emergency_response_sla_deadline: float | None
    service_duration: DurationEstimate
    p_late_start: float
    p_sla_breach: float
    p_window_sla_breach: float
    p_emergency_response_sla_breach: float | None
    p_overtime: float
    expected_late_minutes: float
    expected_overtime_minutes: float
    expected_incremental_overtime_minutes: float
    expected_loss: float


@dataclass(frozen=True, slots=True)
class RouteRisk:
    engineer_id: str
    visits: tuple[VisitRisk, ...]
    p_late_start: float
    p_sla_breach: float
    p_window_sla_breach: float
    p_emergency_response_sla_breach: float | None
    emergency_response_sla_visit_count: int
    p_overtime: float
    expected_overtime_minutes: float
    expected_loss: float
    expected_affected_customers: float
    # Finite-run diagnostic (estimated P(SLA breach) > 0), not a stable ranking
    # metric.  Use expected_affected_customers for comparisons.
    customers_at_risk: tuple[str, ...]
    affected_customer_count: int


@dataclass(frozen=True, slots=True)
class PlanRisk:
    model_version: str
    calibrated: bool
    loss_unit: str
    loss_weights: LossWeights
    scenario_id: str
    iterations: int
    seed: int | str
    routes: tuple[RouteRisk, ...]
    p_late_start: float
    p_sla_breach: float
    p_window_sla_breach: float
    p_emergency_response_sla_breach: float | None
    emergency_response_sla_visit_count: int
    p_overtime: float
    expected_overtime_minutes: float
    expected_loss: float
    expected_affected_customers: float
    # Finite-run diagnostic (estimated P(SLA breach) > 0), not a stable ranking
    # metric.  Use expected_affected_customers for comparisons.
    customers_at_risk: tuple[str, ...]
    affected_customer_count: int
    assumptions: tuple[str, ...]

    @property
    def visits(self) -> tuple[VisitRisk, ...]:
        """Flattened visit view in plan order."""
        return tuple(visit for route in self.routes for visit in route.visits)

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-ready nested primitives without requiring Pydantic."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _LognormalModel:
    mu: float
    sigma: float
    estimate: DurationEstimate

    def sample(self, rng: random.Random) -> float:
        if self.sigma == 0:
            # Reuse the published median to avoid log/exp round-off turning an
            # exact boundary completion into a microscopic delay.
            return self.estimate.p50_minutes
        return rng.lognormvariate(self.mu, self.sigma)


def _default_group_key(request: Request) -> str:
    return request.id


class ServiceDurationEstimator:
    """Estimate service P50/P80/P90 with optional empirical observations.

    ``observations`` maps a grouping key to positive service durations in
    minutes.  By default the grouping key is ``request.id``.  Supplying, for
    example, ``group_key=lambda r: r.bk_type or r.skill.value`` lets historical
    observations be shared across comparable requests without coupling this
    core to an application-specific taxonomy.
    """

    def __init__(
        self,
        prior: ServiceDurationPrior | None = None,
        observations: Mapping[str, Sequence[float]] | None = None,
        group_key: ServiceGroupKey | None = None,
    ) -> None:
        self.prior = prior or ServiceDurationPrior()
        self._group_key = group_key or _default_group_key
        self._observations: dict[str, tuple[float, ...]] = {}
        for raw_key, raw_values in (observations or {}).items():
            key = str(raw_key)
            if not key:
                raise ValueError("service observation keys must not be empty")
            values = tuple(raw_values)
            if any(not _is_finite_positive(value) for value in values):
                raise ValueError(f"service observations for {key!r} must be finite and positive")
            self._observations[key] = values

    @property
    def observation_count(self) -> int:
        return sum(len(values) for values in self._observations.values())

    @property
    def observation_group_count(self) -> int:
        return sum(bool(values) for values in self._observations.values())

    def estimate(self, request: Request, observations: Iterable[float] = ()) -> DurationEstimate:
        """Return quantiles, optionally adding request-local observations."""
        return self._model(request, observations).estimate

    def _model(self, request: Request, extra_observations: Iterable[float] = ()) -> _LognormalModel:
        key = str(self._group_key(request))
        if not key:
            raise ValueError(f"service group key for request {request.id!r} must not be empty")
        extra = tuple(extra_observations)
        if any(not _is_finite_positive(value) for value in extra):
            raise ValueError("service observations must be finite and positive")
        observed = self._observations.get(key, ()) + extra

        nominal = float(request.duration_minutes)
        prior_mu = math.log(nominal)
        prior_sigma = _log_sigma(self.prior.coefficient_of_variation)
        weight = self.prior.prior_strength
        logged = tuple(math.log(value) for value in observed)
        total_weight = weight + len(logged)

        # Pool first and second log moments.  This is an intentionally simple,
        # inspectable shrinkage rule rather than a claim of a fitted ML model.
        mu = (weight * prior_mu + sum(logged)) / total_weight
        second_moment = (
            weight * (prior_sigma**2 + prior_mu**2) + sum(value**2 for value in logged)
        ) / total_weight
        variance = max(0.0, second_moment - mu**2)
        sigma = max(self.prior.min_log_sigma, math.sqrt(variance))

        p50 = nominal if not observed else math.exp(mu)
        if sigma == 0:
            p80 = p90 = p50
        else:
            p80 = math.exp(mu + sigma * _Z80)
            p90 = math.exp(mu + sigma * _Z90)
        if not all(math.isfinite(value) for value in (p50, p80, p90)):
            raise ValueError("service duration estimate is not finite")
        estimate = DurationEstimate(
            nominal_minutes=nominal,
            p50_minutes=p50,
            p80_minutes=p80,
            p90_minutes=p90,
            log_sigma=sigma,
            observation_count=len(observed),
            source="prior+empirical" if observed else "prior",
            group_key=key,
        )
        return _LognormalModel(mu=mu, sigma=sigma, estimate=estimate)


def estimate_service_duration(
    request: Request,
    *,
    observations: Iterable[float] = (),
    prior: ServiceDurationPrior | None = None,
) -> DurationEstimate:
    """Convenience function for one request without constructing an engine."""
    return ServiceDurationEstimator(prior=prior).estimate(request, observations)


@dataclass(slots=True)
class _RouteSimulation:
    result: RouteRisk
    any_late: list[bool]
    any_sla: list[bool]
    any_window_sla: list[bool]
    any_emergency_response_sla: list[bool]
    overtime: list[bool]


class RiskEngine:
    """Run reproducible Monte Carlo risk analysis over a finished ``Plan``."""

    def __init__(
        self,
        config: RiskEngineConfig | None = None,
        *,
        service_observations: Mapping[str, Sequence[float]] | None = None,
        service_group_key: ServiceGroupKey | None = None,
    ) -> None:
        self.config = config or RiskEngineConfig()
        self.service_estimator = ServiceDurationEstimator(
            prior=self.config.service_prior,
            observations=service_observations,
            group_key=service_group_key,
        )

    def analyze(self, plan: Plan, scenario: Scenario) -> PlanRisk:
        """Analyze without mutating either input.

        Unknown/duplicate plan references fail closed with ``ValueError``.  The
        existing independent plan validator remains the source of truth for all
        other feasibility checks.
        """
        if plan.scenario_id != scenario.id:
            raise ValueError(
                f"plan scenario_id {plan.scenario_id!r} does not match scenario {scenario.id!r}"
            )
        requests = {request.id: request for request in scenario.requests}
        engineers = {engineer.id: engineer for engineer in scenario.engineers}
        seen_requests: set[str] = set()
        seen_engineers: set[str] = set()
        simulations: list[_RouteSimulation] = []

        for route in plan.routes:
            if route.engineer_id not in engineers:
                raise ValueError(f"plan references unknown engineer {route.engineer_id!r}")
            if route.engineer_id in seen_engineers:
                raise ValueError(f"plan contains duplicate route for engineer {route.engineer_id!r}")
            seen_engineers.add(route.engineer_id)
            for visit in route.visits:
                if visit.request_id not in requests:
                    raise ValueError(f"plan references unknown request {visit.request_id!r}")
                if visit.request_id in seen_requests:
                    raise ValueError(f"plan contains duplicate visit for request {visit.request_id!r}")
                seen_requests.add(visit.request_id)
            simulations.append(
                self._simulate_route(route, engineers[route.engineer_id], requests)
            )

        iterations = self.config.iterations
        p_late = _probability(
            sum(any(item.any_late[index] for item in simulations) for index in range(iterations)),
            iterations,
        )
        p_sla = _probability(
            sum(any(item.any_sla[index] for item in simulations) for index in range(iterations)),
            iterations,
        )
        p_window_sla = _probability(
            sum(
                any(item.any_window_sla[index] for item in simulations)
                for index in range(iterations)
            ),
            iterations,
        )
        response_sla_visit_count = sum(
            item.result.emergency_response_sla_visit_count for item in simulations
        )
        p_response_sla = (
            _probability(
                sum(
                    any(item.any_emergency_response_sla[index] for item in simulations)
                    for index in range(iterations)
                ),
                iterations,
            )
            if response_sla_visit_count
            else None
        )
        p_overtime = _probability(
            sum(any(item.overtime[index] for item in simulations) for index in range(iterations)),
            iterations,
        )
        routes = tuple(item.result for item in simulations)
        customers = tuple(
            visit.request_id for route in routes for visit in route.visits if visit.p_sla_breach > 0
        )
        evidence_assumptions = (
            (
                f"{self.service_estimator.observation_count} confirmed execution observations "
                f"are available across {self.service_estimator.observation_group_count} groups; "
                f"compatible service-duration priors use them",
                "probabilities remain uncalibrated until out-of-sample validation on actual outcomes",
            )
            if self.service_estimator.observation_count
            else ("probabilities are synthetic prior-based estimates until calibrated on actual history",)
        )
        return PlanRisk(
            model_version=self.config.model_version,
            calibrated=self.config.calibrated,
            loss_unit=self.config.loss_unit,
            loss_weights=self.config.loss_weights,
            scenario_id=scenario.id,
            iterations=iterations,
            seed=self.config.seed,
            routes=routes,
            p_late_start=p_late,
            p_sla_breach=p_sla,
            p_window_sla_breach=p_window_sla,
            p_emergency_response_sla_breach=p_response_sla,
            emergency_response_sla_visit_count=response_sla_visit_count,
            p_overtime=p_overtime,
            expected_overtime_minutes=sum(route.expected_overtime_minutes for route in routes),
            expected_loss=sum(route.expected_loss for route in routes),
            expected_affected_customers=sum(route.expected_affected_customers for route in routes),
            customers_at_risk=customers,
            affected_customer_count=len(customers),
            assumptions=(
                "planned travel_minutes is the travel-duration P50",
                "request.duration_minutes is the service-duration prior P50",
                "travel and service draws are independent between legs and visits",
                "window SLA breach is an operational proxy: service starts outside every request window",
                "the 120-minute emergency response SLA applies only to urgent requests with provenance.received_at",
                "p_sla_breach is the union of window and applicable emergency response breaches",
                *evidence_assumptions,
                "travel variation is synthetic and does not represent live traffic",
                "loss units are defined solely by LossWeights",
                "expected_affected_customers, not the finite-run at-risk count, is intended for ranking",
            ),
        )

    def _simulate_route(
        self,
        route: Route,
        engineer: Engineer,
        requests: Mapping[str, Request],
    ) -> _RouteSimulation:
        iterations = self.config.iterations
        visit_count = len(route.visits)
        service_models = [self.service_estimator._model(requests[v.request_id]) for v in route.visits]
        planned_deadlines = [
            self._planned_sla_deadline(requests[visit.request_id], visit.start)
            for visit in route.visits
        ]
        response_deadlines = [
            self._emergency_response_sla_deadline(requests[visit.request_id])
            for visit in route.visits
        ]
        late_counts = [0] * visit_count
        sla_counts = [0] * visit_count
        window_sla_counts = [0] * visit_count
        response_sla_counts = [0] * visit_count
        overtime_counts = [0] * visit_count
        late_minute_sums = [0.0] * visit_count
        overtime_minute_sums = [0.0] * visit_count
        incremental_overtime_sums = [0.0] * visit_count
        loss_sums = [0.0] * visit_count
        any_late = [False] * iterations
        any_sla = [False] * iterations
        any_window_sla = [False] * iterations
        any_response_sla = [False] * iterations
        route_overtime = [False] * iterations
        route_overtime_minutes = 0.0
        route_loss = 0.0

        rng = random.Random(self._route_seed(route.engineer_id))
        travel_sigma = _log_sigma(self.config.travel_coefficient_of_variation)
        weights = self.config.loss_weights
        shift_end = float(engineer.shift.end)

        for iteration in range(iterations):
            now = float(engineer.shift.start)
            iteration_loss = 0.0
            for index, (visit, service_model) in enumerate(zip(route.visits, service_models)):
                planned_departure = float(visit.arrival - visit.travel_minutes)
                # An explicit planned gap is an absolute buffer: a delay can
                # consume it, while an early finish still waits for it.
                now = max(now, planned_departure)
                overtime_before_leg = max(0.0, now - shift_end)
                travel_duration = self._sample_travel(float(visit.travel_minutes), travel_sigma, rng)
                arrival = now + travel_duration
                start, window_sla_breached = self._actual_start(
                    arrival, requests[visit.request_id]
                )
                response_deadline = response_deadlines[index]
                response_sla_breached = (
                    response_deadline is not None
                    and start > response_deadline + self.config.sla_tolerance_minutes
                )
                sla_breached = window_sla_breached or response_sla_breached
                duration = service_model.sample(rng)
                end = start + duration

                late_minutes = max(0.0, start - float(visit.start))
                overtime_minutes = max(0.0, end - shift_end)
                incremental_overtime = max(0.0, overtime_minutes - overtime_before_leg)
                late = start > float(visit.start) + self.config.late_start_tolerance_minutes
                overtime = end > shift_end + self.config.overtime_tolerance_minutes
                loss = (
                    (weights.late_start if late else 0.0)
                    + (weights.sla_breach if sla_breached else 0.0)
                    + weights.overtime_minute * incremental_overtime
                )

                late_counts[index] += int(late)
                sla_counts[index] += int(sla_breached)
                window_sla_counts[index] += int(window_sla_breached)
                response_sla_counts[index] += int(response_sla_breached)
                overtime_counts[index] += int(overtime)
                late_minute_sums[index] += late_minutes
                overtime_minute_sums[index] += overtime_minutes
                incremental_overtime_sums[index] += incremental_overtime
                loss_sums[index] += loss
                any_late[iteration] = any_late[iteration] or late
                any_sla[iteration] = any_sla[iteration] or sla_breached
                any_window_sla[iteration] = (
                    any_window_sla[iteration] or window_sla_breached
                )
                any_response_sla[iteration] = (
                    any_response_sla[iteration] or response_sla_breached
                )
                iteration_loss += loss
                now = end

            overtime_minutes = max(0.0, now - shift_end) if route.visits else 0.0
            route_overtime[iteration] = (
                bool(route.visits)
                and now > shift_end + self.config.overtime_tolerance_minutes
            )
            route_overtime_minutes += overtime_minutes
            route_loss += iteration_loss

        visit_risks = tuple(
            VisitRisk(
                request_id=visit.request_id,
                engineer_id=route.engineer_id,
                planned_start=visit.start,
                planned_sla_deadline=planned_deadlines[index],
                emergency_response_sla_deadline=response_deadlines[index],
                service_duration=service_models[index].estimate,
                p_late_start=_probability(late_counts[index], iterations),
                p_sla_breach=_probability(sla_counts[index], iterations),
                p_window_sla_breach=_probability(window_sla_counts[index], iterations),
                p_emergency_response_sla_breach=(
                    _probability(response_sla_counts[index], iterations)
                    if response_deadlines[index] is not None
                    else None
                ),
                p_overtime=_probability(overtime_counts[index], iterations),
                expected_late_minutes=late_minute_sums[index] / iterations,
                expected_overtime_minutes=overtime_minute_sums[index] / iterations,
                expected_incremental_overtime_minutes=incremental_overtime_sums[index] / iterations,
                expected_loss=loss_sums[index] / iterations,
            )
            for index, visit in enumerate(route.visits)
        )
        customers = tuple(visit.request_id for visit in visit_risks if visit.p_sla_breach > 0)
        result = RouteRisk(
            engineer_id=route.engineer_id,
            visits=visit_risks,
            p_late_start=_probability(sum(any_late), iterations),
            p_sla_breach=_probability(sum(any_sla), iterations),
            p_window_sla_breach=_probability(sum(any_window_sla), iterations),
            p_emergency_response_sla_breach=(
                _probability(sum(any_response_sla), iterations)
                if any(deadline is not None for deadline in response_deadlines)
                else None
            ),
            emergency_response_sla_visit_count=sum(
                deadline is not None for deadline in response_deadlines
            ),
            p_overtime=_probability(sum(route_overtime), iterations),
            expected_overtime_minutes=route_overtime_minutes / iterations,
            expected_loss=route_loss / iterations,
            expected_affected_customers=sum(visit.p_sla_breach for visit in visit_risks),
            customers_at_risk=customers,
            affected_customer_count=len(customers),
        )
        return _RouteSimulation(
            result=result,
            any_late=any_late,
            any_sla=any_sla,
            any_window_sla=any_window_sla,
            any_emergency_response_sla=any_response_sla,
            overtime=route_overtime,
        )

    def _route_seed(self, engineer_id: str) -> int:
        payload = (
            f"{self.config.model_version}\0{self.config.seed!s}\0{engineer_id}"
        ).encode("utf-8")
        return int.from_bytes(hashlib.sha256(payload).digest()[:16], "big")

    @staticmethod
    def _sample_travel(nominal: float, sigma: float, rng: random.Random) -> float:
        if nominal < 0 or not math.isfinite(nominal):
            raise ValueError("planned travel duration must be finite and non-negative")
        if nominal == 0 or sigma == 0:
            return nominal
        return rng.lognormvariate(math.log(nominal), sigma)

    def _actual_start(self, arrival: float, request: Request) -> tuple[float, bool]:
        tolerance = self.config.sla_tolerance_minutes
        candidates = [
            max(arrival, float(window.start))
            for window in request.windows
            if max(arrival, float(window.start)) <= float(window.end) + tolerance
        ]
        if candidates:
            return min(candidates), False
        # Work can still happen after a missed window; it is now an SLA breach.
        return arrival, True

    def _emergency_response_sla_deadline(self, request: Request) -> float | None:
        """Return received_at + target only for traceable urgent requests.

        ``received_at`` is stored by the event/replanning flow as minutes from
        midnight in the string-valued provenance map.  An urgent label alone is
        insufficient evidence for a response clock; malformed supplied evidence
        fails closed instead of silently manufacturing or dropping an SLA.
        """
        if request.priority != "urgent" or "received_at" not in request.provenance:
            return None
        raw_received_at = str(request.provenance["received_at"]).strip()
        try:
            received_at = float(raw_received_at)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"urgent request {request.id!r} has invalid provenance.received_at"
            ) from exc
        if not math.isfinite(received_at) or not 0 <= received_at <= 1440:
            raise ValueError(
                f"urgent request {request.id!r} has invalid provenance.received_at"
            )
        return received_at + self.config.emergency_response_target_minutes

    @staticmethod
    def _planned_sla_deadline(request: Request, planned_start: int) -> int:
        deadlines = [
            window.end for window in request.windows if window.start <= planned_start <= window.end
        ]
        if not deadlines:
            raise ValueError(
                f"planned start {planned_start} for request {request.id!r} is outside every window"
            )
        return max(deadlines)


def analyze_plan_risk(
    plan: Plan,
    scenario: Scenario,
    *,
    config: RiskEngineConfig | None = None,
    service_observations: Mapping[str, Sequence[float]] | None = None,
    service_group_key: ServiceGroupKey | None = None,
) -> PlanRisk:
    """Functional facade for the common one-shot analysis case."""
    return RiskEngine(
        config=config,
        service_observations=service_observations,
        service_group_key=service_group_key,
    ).analyze(plan, scenario)


__all__ = [
    "DurationEstimate",
    "EMERGENCY_RESPONSE_TARGET_MINUTES",
    "LossWeights",
    "PlanRisk",
    "RISK_MODEL_VERSION",
    "RiskEngine",
    "RiskEngineConfig",
    "RouteRisk",
    "ServiceDurationEstimator",
    "ServiceDurationPrior",
    "UI_RERANKING_CONFIG",
    "VisitRisk",
    "analyze_plan_risk",
    "application_risk_config",
    "application_risk_seed",
    "application_service_group",
    "estimate_service_duration",
]
