"""Confirmed 21 Sep rules: replace travel allowance, keep regions isolated."""
import json
from pathlib import Path
import pytest
from conftest import job,engineer
from field_router.models import Skill,Scenario
from field_router.planning import build_route,compatible

RULES=json.loads((Path(__file__).parents[1]/'config/demo.json').read_text())['work_rules']

@pytest.mark.parametrize('kind,total,priority',[
    ('Подключение',90,'high'),('Дозаказ',40,'normal'),
    ('Локальная заявка',50,'normal'),('Глобальная проблема',100,'urgent')])
@pytest.mark.parametrize('travel_minutes',[5,30,120])
def test_actual_travel_replaces_twenty_minute_allowance(kind,total,priority,travel_minutes):
    rule=RULES[kind]
    assert rule['duration_minutes']==total-20
    assert rule['priority']==priority
    request=job('a',start=540,end=1320,duration=rule['duration_minutes'],skill=Skill(rule['skill']),priority=priority)
    e=engineer(start=540,end=1320,skills=[request.skill])
    class Travel:
        def minutes(self,*args):return travel_minutes
        def metres(self,*args):return 1000
    route=build_route(e,[request],Travel())
    assert route is not None
    assert route.visits[0].end-e.shift.start==total-20+travel_minutes


def test_same_anonymized_name_does_not_allow_cross_region_assignment(make_scenario):
    request=job('a');local=engineer().model_copy(update={'name':'Каушан'})
    foreign=local.model_copy(update={'region':'other'})
    assert compatible(request,local)
    assert not compatible(request,foreign)
    with pytest.raises(ValueError,match='region'):
        Scenario.model_validate({**make_scenario([request]).model_dump(),'engineers':[foreign.model_dump()]})


def test_shipped_demos_use_confirmed_work_times_priorities_and_own_crews():
    for path in (Path(__file__).parents[1]/'data/demo-road').glob('*.json'):
        s=Scenario.model_validate(json.loads(path.read_text())['scenario'])
        assert all(e.region==s.region for e in s.engineers)
        for r in s.requests:
            assert r.region==s.region
            assert r.duration_minutes==RULES[r.bk_type]['duration_minutes']
            assert r.priority==RULES[r.bk_type]['priority']
