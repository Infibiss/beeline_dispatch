"""Explicit, reproducible enrichment; source addresses are never geocoded here."""
import hashlib
import json
import math
import random

from .importer import ImportResult
from .config import DemoConfig
from .models import Engineer, Equipment, Point, Request, Scenario, TimeWindow


def _rng(seed: int, key: str) -> random.Random:
    digest = hashlib.sha256(f"{seed}:{key}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def prepare_scenario(source: ImportResult, region: str, config: dict, *, synthetic_geography=False,
                     engineers: list[Engineer] | None = None) -> Scenario:
    config = DemoConfig.model_validate(config).model_dump(mode="json")
    engineers_source = "synthetic" if engineers is None else "provided_json"
    if source.errors:
        raise ValueError("Импорт содержит ошибки; исправьте строки перед планированием")
    if not source.requests or not source.office_address:
        raise ValueError("Нужны заявки и адрес офиса")
    if not synthetic_geography:
        raise ValueError("Нет географии. Синтетическую географию нужно выбрать явно")
    seed = config["seed"]
    points = {"office": Point(x_km=0, y_km=0)}
    requests = []
    for raw in source.requests:
        if raw.bk_type not in config["work_rules"]:
            raise ValueError(f"Нет правила норматива: строка {raw.source_line}, {raw.bk_type}")
        rule = config["work_rules"][raw.bk_type]
        rng = _rng(seed, f"{region}:{raw.id}:equipment")
        equipment = Equipment(router=int(rng.random() < config["equipment"]["router_probability"]),
                              tv_box=int(rng.random() < config["equipment"]["tv_box_probability"]))
        if "роутер" in raw.hd_type.lower():
            equipment = equipment.model_copy(update={"router":1})
        if "приставк" in raw.hd_type.lower():
            equipment = equipment.model_copy(update={"tv_box":1})
        center = _rng(seed, f"{region}:{raw.locality}:center")
        angle = center.random() * 2 * math.pi
        radius = center.random() * config["geography"]["radius_km"]
        offset = _rng(seed, f"{region}:{raw.address}:point")
        spread = config["geography"]["cluster_radius_km"]
        points[raw.id] = Point(x_km=round(math.cos(angle)*radius+offset.uniform(-spread,spread),4),
                              y_km=round(math.sin(angle)*radius+offset.uniform(-spread,spread),4))
        requests.append(Request(id=raw.id, region=region, address=raw.address, locality=raw.locality,
            date=raw.date, bk_type=raw.bk_type, hd_type=raw.hd_type, windows=[raw.window],
            duration_minutes=rule["duration_minutes"], skill=rule["skill"], equipment=equipment,
            priority=rule["priority"],
            source_line=raw.source_line, provenance={"source":source.source,
                "duration":"A01/A02/I03: config.work_rules, XLSX technical + documents",
                "equipment":"A09/I04: seeded synthetic demand",
                "coordinates":"A12: synthetic Cartesian plane, not actual address location",
                "priority":f"I01: config.work_rules[{raw.bk_type}].priority={rule['priority']}; high and urgent share one tier",
                "windows":"Original CSV start/end preserved; priority does not alter customer windows",
                "order":"I01: CSV row order"}))
    if engineers is None:
        cfg = config["engineers"]
        if cfg["count"] != len(cfg["transports"]) or not 0 <= cfg["early_count"] <= cfg["count"]:
            raise ValueError("Некорректное распределение инженеров")
        engineers = []
        for i, mode in enumerate(cfg["transports"]):
            start, end = cfg["early_shift"] if i < cfg["early_count"] else cfg["late_shift"]
            engineers.append(Engineer(id=f"{region}-{i+1:02}",name=f"Инженер {i+1:02}",region=region,
                shift=TimeWindow(start=start,end=end),skills=cfg["skill_sets"][i % len(cfg["skill_sets"])],
                transport=mode))
    for e in engineers:
        if e.start_location not in points:
            raise ValueError(f"Нет координат стартовой точки {e.start_location}")
    return Scenario(id=f"{region}-{source.requests[0].date}",region=region,date=source.requests[0].date,
        office_address=source.office_address, requests=requests, engineers=engineers, points=points,
        travel_config=config["travel"], assumptions=config["assumptions"],
        metadata={"geography":"synthetic_cartesian_km", "seed":seed,"config_version":config["version"],
                  "config_sha256":hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest(),
                  "source_encoding":source.encoding, "office_rows_separated":True,
                  "engineers_source":engineers_source, "generation_config":config})
