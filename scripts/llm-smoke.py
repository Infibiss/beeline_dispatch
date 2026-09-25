"""Live YandexGPT smoke test using synthetic, non-customer aggregates."""

import json

from field_router.llm import configured


BATCH = {
    "id": "live-smoke",
    "version": 1,
    "candidates": [
        {
            "id": "stable",
            "facts": {
                "assigned": 5,
                "unassigned": 0,
                "emergency_assigned": 1,
                "emergency_total": 1,
                "connection_assigned": 2,
                "connection_total": 2,
                "reassigned": 0,
                "avoidable_reassigned": 0,
                "time_shift_minutes": 15,
                "travel_minutes": 110,
                "distance_m": 72_000,
                "engineers_used": 3,
            },
            "emergency": {"assigned": True, "response_minutes": 28},
            "risk": {
                "status": "ready",
                "affected_clients": 1,
                "summary": {
                    "p_sla_breach": 0.08,
                    "expected_loss": 1.7,
                    "expected_affected_customers": 0.4,
                    "p_overtime": 0.04,
                },
            },
            "explanation": "Безопасное резервное объяснение.",
        },
        {
            "id": "sla",
            "facts": {
                "assigned": 5,
                "unassigned": 0,
                "emergency_assigned": 1,
                "emergency_total": 1,
                "connection_assigned": 2,
                "connection_total": 2,
                "reassigned": 2,
                "avoidable_reassigned": 2,
                "time_shift_minutes": 45,
                "travel_minutes": 125,
                "distance_m": 76_000,
                "engineers_used": 3,
            },
            "emergency": {"assigned": True, "response_minutes": 18},
            "risk": {
                "status": "ready",
                "affected_clients": 2,
                "summary": {
                    "p_sla_breach": 0.05,
                    "expected_loss": 1.2,
                    "expected_affected_customers": 0.3,
                    "p_overtime": 0.09,
                },
            },
            "explanation": "Безопасное резервное объяснение.",
        },
    ],
}


def main() -> int:
    explainer = configured()
    if explainer is None:
        print(json.dumps({"ok": False, "reason": "not_configured"}))
        return 2
    result = explainer.recommend(BATCH)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("source") == "yandexgpt" else 3


if __name__ == "__main__":
    raise SystemExit(main())
