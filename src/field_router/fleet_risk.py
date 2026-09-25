"""Transparent fleet readiness score.

The score is deliberately *not* called a breakdown probability: the supplied
hackathon data has no exposure time or labelled failures from which such a
probability could be calibrated.  It only combines factual maintenance and
incident signals supplied for a concrete vehicle.
"""
from datetime import date
import math

from .models import Scenario, Transport, Vehicle


MODEL_VERSION = "fleet-readiness-rules-v1"
_SEVERITY = {"minor": .06, "major": .18, "critical": .35}


def _combine(components):
    remaining = 1.0
    for value in components:
        remaining *= 1.0 - max(0.0, min(.95, value))
    return round(min(.99, 1.0 - remaining), 4)


def score_vehicle(vehicle: Vehicle, on_date: str) -> dict:
    """Return an explainable 0..1 maintenance score or insufficient_data."""
    day = date.fromisoformat(on_date)
    reasons, components, observed = [], [], []

    if not vehicle.active:
        return {"vehicle_id": vehicle.id, "status": "unavailable", "score": 1.0,
                "band": "critical", "measure": "score", "model_version": MODEL_VERSION,
                "reasons": ["Транспорт помечен как недоступный."],
                "recommendation": "Не использовать автомобиль до явного возврата в эксплуатацию.",
                "data_fields": ["active"]}

    if vehicle.odometer_km is not None and vehicle.next_service_km is not None:
        observed += ["odometer_km", "next_service_km"]
        margin = vehicle.next_service_km - vehicle.odometer_km
        if margin < 0:
            components.append(.55 + min(.25, abs(margin) / 8000))
            reasons.append(f"ТО просрочено по пробегу на {abs(margin)} км.")
        elif margin <= 1000:
            components.append(.15 + .25 * (1000 - margin) / 1000)
            reasons.append(f"До ТО осталось {margin} км.")

    if vehicle.odometer_observed_on is not None:
        observed.append("odometer_observed_on")
        age = (day - date.fromisoformat(vehicle.odometer_observed_on)).days
        if age > 30:
            components.append(min(.2, (age - 30) / 365))
            reasons.append(f"Показание одометра не обновлялось {age} дн.")

    if vehicle.next_service_date is not None:
        observed.append("next_service_date")
        margin = (date.fromisoformat(vehicle.next_service_date) - day).days
        if margin < 0:
            components.append(.45 + min(.25, abs(margin) / 180))
            reasons.append(f"ТО просрочено на {abs(margin)} дн.")
        elif margin <= 30:
            components.append(.1 + .25 * (30 - margin) / 30)
            reasons.append(f"До планового ТО {margin} дн.")

    if vehicle.open_defects:
        observed.append("open_defects")
        components.append(min(.7, .2 + .12 * vehicle.open_defects))
        reasons.append(f"Открытых дефектов: {vehicle.open_defects}.")

    if vehicle.failures:
        observed.append("failures")
        for failure in vehicle.failures:
            age = max(0, (day - date.fromisoformat(failure.occurred_on)).days)
            contribution = _SEVERITY[failure.severity] * math.exp(-age / 180)
            if not failure.resolved:
                contribution += .25
            components.append(contribution)
        recent = sum((day - date.fromisoformat(f.occurred_on)).days <= 180 for f in vehicle.failures)
        if recent:
            reasons.append(f"Отказов за последние 180 дней: {recent}.")
        if any(not f.resolved for f in vehicle.failures):
            reasons.append("В истории есть незакрытый отказ.")

    if vehicle.maintenance:
        observed.append("maintenance")

    if not observed:
        return {"vehicle_id": vehicle.id, "status": "insufficient_data", "score": None,
                "band": None, "measure": "score", "model_version": MODEL_VERSION,
                "reasons": ["Нет пробега, срока ТО, дефектов или истории отказов."],
                "recommendation": "Загрузите пробег, ближайшее ТО и историю отказов; до этого вероятность поломки не рассчитывается.",
                "data_fields": []}

    score = _combine(components)
    band = "low" if score < .25 else "medium" if score < .5 else "high" if score < .75 else "critical"
    if not reasons:
        reasons.append("Ближайшее ТО не просрочено, открытых дефектов и недавних отказов нет.")
    action = ("Автомобиль можно учитывать в плане; контролируйте обновление телеметрии."
              if band == "low" else
              "Проверьте автомобиль до смены и подготовьте резервный транспорт."
              if band == "medium" else
              "Не назначайте критичный маршрут без осмотра и подтверждённого резерва.")
    return {"vehicle_id": vehicle.id, "status": "ready", "score": score, "band": band,
            "measure": "score", "model_version": MODEL_VERSION, "reasons": reasons[:5],
            "recommendation": action, "data_fields": sorted(set(observed))}


def fleet_snapshot(scenario: Scenario) -> dict:
    """Describe data coverage and readiness for every car-based engineer."""
    vehicles = {vehicle.id: vehicle for vehicle in scenario.vehicles}
    rows = []
    for engineer in scenario.engineers:
        if engineer.transport != Transport.CAR:
            continue
        vehicle = vehicles.get(engineer.vehicle_id) if engineer.vehicle_id else None
        if vehicle is None:
            assessment = {"status": "not_linked", "score": None, "band": None,
                          "measure": "score", "model_version": MODEL_VERSION,
                          "reasons": ["Для автомобильной бригады не указан vehicle_id."],
                          "recommendation": "Привяжите физический автомобиль и загрузите факты эксплуатации.",
                          "data_fields": []}
        else:
            assessment = score_vehicle(vehicle, scenario.date)
        rows.append({"engineer_id": engineer.id, "engineer_name": engineer.name,
                     "vehicle_id": engineer.vehicle_id, "vehicle_label": vehicle.label if vehicle else None,
                     **assessment})
    scored = [row for row in rows if row["score"] is not None]
    return {"model_version": MODEL_VERSION, "measure": "score", "probability": False,
            "covered": len(scored), "total": len(rows),
            "max_score": max((row["score"] for row in scored), default=None), "rows": rows,
            "notice": "Индекс готовности транспорта — прозрачная эвристика, не вероятность поломки."}
