"""Confirmed service-duration evidence derived from the effective fact journal.

The journal is the source of truth.  Planned timestamps, frozen ledger rows and
pending engineer reports are deliberately excluded: none of them proves an
actual service duration.  Interrupted work is counted only as explicit working
segments, and an observation is published only after a canonical ``completed``
fact closes the final segment.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

from .models import Request
from .risk_engine import application_service_group


@dataclass(frozen=True, slots=True)
class ServiceObservationSnapshot:
    observations: dict[str, tuple[float, ...]]
    request_ids: frozenset[str]
    residual_request_ids: frozenset[str]
    observation_count: int
    group_count: int
    pending_confirmation_count: int
    ignored_completion_count: int
    latest_observation_time: int | None

    def metadata(self) -> dict:
        return {
            "status": "empirical_update" if self.observation_count else "prior_only",
            "source": "confirmed_fact_journal",
            "observation_count": self.observation_count,
            "group_count": self.group_count,
            "pending_confirmation_count": self.pending_confirmation_count,
            "ignored_completion_count": self.ignored_completion_count,
            "excluded_residual_count": len(self.residual_request_ids),
            "latest_observation_time": self.latest_observation_time,
        }

    def group_key(self, session) -> Callable[[Request], str]:
        """Return a key safe for the specific remaining-day scenario.

        A completed request can still be present in the approved plan while a
        replan is pending.  It receives a private key so its own duration is not
        fed back into its stale visit.  Likewise, a request whose duration was
        replaced by a factual *remaining* duration must not receive historical
        full-job observations.
        """
        excluded_ids = self.request_ids | self.residual_request_ids

        def key(request: Request) -> str:
            original = session.catalog.get(request.id)
            if request.id in excluded_ids:
                return f"excluded-request-specific-duration|{request.id}"
            if original is not None and original.duration_minutes != request.duration_minutes:
                return f"residual-duration|{request.id}|{request.duration_minutes}"
            return application_service_group(request)

        return key


def confirmed_service_observations(session) -> ServiceObservationSnapshot:
    """Build shrinkage inputs from explicit, confirmed working intervals."""
    states: dict[str, dict] = defaultdict(
        lambda: {
            "active": None,
            "segments": [],
            "invalid": False,
            "completed": False,
            "pending": False,
            "completed_at": None,
        }
    )
    active_by_engineer: dict[str, str] = {}
    grouped: dict[str, list[float]] = defaultdict(list)
    accepted_ids: set[str] = set()
    accepted_times: list[int] = []
    pending = 0
    completed_seen: set[str] = set()

    facts = sorted(
        session.fact_log,
        key=lambda fact: (fact.get("time", -1), fact.get("recorded_at", ""), fact.get("id", "")),
    )
    for fact in facts:
        engineer_id = fact.get("engineer_id")
        request_id = fact.get("request_id")
        status = fact.get("status")
        when = fact.get("time")
        if not isinstance(engineer_id, str) or not isinstance(when, int):
            continue

        if status == "working" and isinstance(request_id, str):
            state = states[request_id]
            current_request = active_by_engineer.get(engineer_id)
            if current_request == request_id and state["active"]:
                # A repeated progress update must not reset the observed start.
                continue
            if current_request is not None:
                states[current_request]["invalid"] = True
                states[current_request]["active"] = None
                state["invalid"] = True
                active_by_engineer.pop(engineer_id, None)
            if state["active"] is not None or state["completed"] or state["pending"]:
                state["invalid"] = True
                continue
            state["active"] = (engineer_id, when, fact.get("id"))
            active_by_engineer[engineer_id] = request_id
            continue

        if status in {"interrupted", "completion_pending", "completed"} and isinstance(request_id, str):
            state = states[request_id]
            current = state["active"]
            valid_close = bool(
                current
                and current[0] == engineer_id
                and active_by_engineer.get(engineer_id) == request_id
                and when > current[1]
            )
            if valid_close:
                state["segments"].append((current[1], when))
            else:
                state["invalid"] = True
            state["active"] = None
            if active_by_engineer.get(engineer_id) == request_id:
                active_by_engineer.pop(engineer_id, None)
            if status == "completion_pending":
                state["pending"] = True
                pending += 1
            elif status == "completed":
                state["completed"] = True
                state["completed_at"] = when
                completed_seen.add(request_id)
            continue

        # A state change away from working without an explicit terminal fact is
        # malformed evidence.  Planning may still use the fact journal, but the
        # duration learner fails closed for that request only.
        current_request = active_by_engineer.pop(engineer_id, None)
        if current_request is not None:
            states[current_request]["invalid"] = True
            states[current_request]["active"] = None

    for request_id, state in states.items():
        if not state["completed"] or state["pending"] or state["invalid"] or state["active"]:
            continue
        duration = sum(end - start for start, end in state["segments"])
        request = session.catalog.get(request_id)
        if request is None or not 0 < duration <= 1440:
            continue
        grouped[application_service_group(request)].append(float(duration))
        accepted_ids.add(request_id)
        accepted_times.append(state["completed_at"])

    observations = {key: tuple(values) for key, values in grouped.items() if values}
    count = sum(len(values) for values in observations.values())
    residual_ids = {
        request_id
        for request_id, state in states.items()
        if not state["completed"]
    }
    return ServiceObservationSnapshot(
        observations=observations,
        request_ids=frozenset(accepted_ids),
        residual_request_ids=frozenset(residual_ids),
        observation_count=count,
        group_count=len(observations),
        pending_confirmation_count=pending,
        ignored_completion_count=len(completed_seen - accepted_ids),
        latest_observation_time=max(accepted_times, default=None),
    )


__all__ = ["ServiceObservationSnapshot", "confirmed_service_observations"]
