import json
from pathlib import Path
import pytest

from field_router.importer import import_csv
from field_router.scenario import prepare_scenario
from field_router.models import Scenario

ROOT = Path(__file__).resolve().parents[1]


def test_preparation_is_reproducible_and_marks_synthetic_geography():
    source = import_csv(ROOT / "data/Обезличивание/Восток Синтетические данные.csv")
    config = json.loads((ROOT / "config/demo.json").read_text())
    a = prepare_scenario(source, "Восток", config, synthetic_geography=True)
    b = prepare_scenario(source, "Восток", config, synthetic_geography=True)
    assert a == b
    assert len(a.engineers) == 15
    assert sum(e.transport.value == "public" for e in a.engineers) > 7
    assert len({e.transport for e in a.engineers}) == 4
    assert a.metadata["geography"] == "synthetic_cartesian_km"
    assert a.requests[0].address == source.requests[0].address
    assert all(r.provenance["duration"] for r in a.requests)
    assert all(r.duration_minutes == 80 for r in a.requests if r.bk_type == "Глобальная проблема")
    assert Scenario.model_validate_json(a.model_dump_json()) == a


def test_synthetic_coordinates_require_explicit_choice():
    import pytest
    source = import_csv(ROOT / "data/Обезличивание/Восток Синтетические данные.csv")
    config = json.loads((ROOT / "config/demo.json").read_text())
    with pytest.raises(ValueError, match="географ"):
        prepare_scenario(source, "Восток", config)


@pytest.mark.parametrize("section,key,value", [
    ("equipment","router_probability",2), ("equipment","tv_box_probability",float("nan")),
    ("geography","radius_km",-8), ("engineers","early_count",1.5),
    ("engineers","skill_sets",[]), ("engineers","count",True),
])
def test_invalid_configuration_is_rejected(section,key,value):
    source = import_csv(ROOT / "data/Обезличивание/Восток Синтетические данные.csv")
    config = json.loads((ROOT / "config/demo.json").read_text())
    config[section][key] = value
    with pytest.raises(ValueError):
        prepare_scenario(source,"Восток",config,synthetic_geography=True)


def test_supplied_engineers_keep_actual_provenance_without_default_roster_claims():
    source = import_csv(ROOT / "data/Обезличивание/Восток Синтетические данные.csv")
    config = json.loads((ROOT / "config/demo.json").read_text())
    generated = prepare_scenario(source, "Восток", config, synthetic_geography=True)
    supplied = [generated.engineers[0]]
    scenario = prepare_scenario(source, "Восток", config, synthetic_geography=True, engineers=supplied)
    assert scenario.engineers == supplied
    assert scenario.metadata["engineers_source"] == "provided_json"
    assert scenario.metadata["generation_config"] == config
    assert not any("15 синтетических инженеров" in item for item in scenario.assumptions)


@pytest.mark.parametrize("region", ["Восток", "Юго-восток", "Югоцентр"])
def test_priority_policy_preserves_every_original_csv_window_and_order(region):
    source = import_csv(ROOT / f"data/Обезличивание/{region} Синтетические данные.csv")
    config = json.loads((ROOT / "config/demo.json").read_text())
    scenario = prepare_scenario(source, region, config, synthetic_geography=True)
    priorities = {"Подключение":"high", "Глобальная проблема":"urgent",
                  "Дозаказ":"normal", "Локальная заявка":"normal"}
    assert [(r.id,r.windows) for r in scenario.requests] == [(r.id,[r.window]) for r in source.requests]
    assert all(r.priority == priorities[r.bk_type] for r in scenario.requests)
    assert all("config.work_rules" in r.provenance["priority"] for r in scenario.requests)


def test_work_rule_priority_is_revisable_and_defaults_to_normal():
    from field_router.config import WorkRule
    assert WorkRule(skill="connection",duration_minutes=70).priority == "normal"
    source = import_csv(ROOT / "data/Обезличивание/Восток Синтетические данные.csv")
    config = json.loads((ROOT / "config/demo.json").read_text())
    config["work_rules"]["Подключение"]["priority"] = "normal"
    config["work_rules"]["Локальная заявка"]["priority"] = "high"
    scenario = prepare_scenario(source,"Восток",config,synthetic_geography=True)
    assert all(r.priority=="normal" for r in scenario.requests if r.bk_type=="Подключение")
    assert all(r.priority=="high" for r in scenario.requests if r.bk_type=="Локальная заявка")
