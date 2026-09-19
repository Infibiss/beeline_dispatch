import json
from pathlib import Path

from field_router.cli import main

ROOT = Path(__file__).resolve().parents[1]


def test_prepare_compare_and_report_round_trip(tmp_path):
    scenario = tmp_path / "scenario.json"
    assert main(["prepare","--input",str(ROOT/"data/Обезличивание/Югоцентр Синтетические данные.csv"),
                 "--region","Югоцентр","--config",str(ROOT/"config/demo.json"),
                 "--synthetic-geography","--output",str(scenario)]) == 0
    output = tmp_path / "result"
    assert main(["compare",str(scenario),"--output",str(output),"--time-limit","0.2"]) == 0
    result = json.loads((output/"comparison.json").read_text())
    assert len(result["scenario"]["requests"]) == 56
    assert len(result["plans"]) == 2
    assert all(p["metrics"]["assigned"]+p["metrics"]["unassigned"] == 56 for p in result["plans"])
    assert "синтет" in (output/"report.html").read_text().lower()


def test_cli_rejects_control_and_does_not_write_result(tmp_path):
    target = tmp_path/"bad.json"
    assert main(["prepare","--input",str(next((ROOT/"data/Обезличивание").glob("*Контроль*"))),
                 "--region","Восток","--output",str(target),"--synthetic-geography"]) == 2
    assert not target.exists()


def test_compare_with_real_geography_is_offline_and_rejects_incomplete(tmp_path,make_scenario):
    from conftest import job
    from field_router.geography import scenario_signature
    from field_router.geo_models import GeoLocation, GeographyBundle
    from field_router.road_travel import build_matrices
    from test_road_travel import Client
    s=make_scenario([job('a')])
    source=tmp_path/'scenario.json';source.write_text(s.model_dump_json())
    p=GeoLocation(lat=55.7,lon=37.7,quality='manual',source='test fixture')
    geo=GeographyBundle(scenario_signature=scenario_signature(s),locations={'office':p,'a':p})
    geo=build_matrices(geo,Client(),allow_public_estimate=True)
    geo=geo.model_copy(update={'route_shapes':{'ortools':{'e1':{'coordinates':[[0,0],[1,1]],'kind':'road'}}}})
    path=tmp_path/'geography.json';path.write_text(geo.model_dump_json())
    out=tmp_path/'real'
    assert main(['compare',str(source),'--geography',str(path),'--output',str(out),'--time-limit','0.05'])==0
    result=json.loads((out/'comparison.json').read_text())
    assert result['scenario']['metadata']['geography']=='wgs84'
    assert result['geography']['modes']['public']['kind']=='estimate'
    assert result['geography']['route_shapes']=={}
    assert 'Синтетические координаты.' not in (out/'report.html').read_text()
    path.write_text(geo.model_copy(update={'locations':{'office':p}}).model_dump_json())
    assert main(['compare',str(source),'--geography',str(path),'--output',str(tmp_path/'bad')])==2
    assert not (tmp_path/'bad/comparison.json').exists()


def test_render_exports_validated_canonical_payload(tmp_path,make_scenario):
    from conftest import job
    from field_router.geo_models import GeographyBundle,GeoLocation
    from field_router.geography import scenario_signature,attach_geography
    from field_router.road_travel import build_matrices,bundle_travel
    from field_router.solvers.baseline import BaselineSolver
    from test_road_travel import Client
    import re
    s=make_scenario([job('a')])
    point=GeoLocation(lat=55.7,lon=37.7,quality='manual',source='fixture')
    geo=build_matrices(GeographyBundle(scenario_signature=scenario_signature(s),locations={'office':point,'a':point}),Client(),allow_public_estimate=True)
    plan=BaselineSolver().solve(attach_geography(s,geo),bundle_travel(geo))
    raw={'scenario':s.model_dump(mode='json'),'plans':[plan.model_dump(mode='json')],'geography':geo.model_dump(mode='json')}
    raw['geography']['locations']['a']['lat']='55.7'
    path=tmp_path/'comparison.json';path.write_text(json.dumps(raw))
    assert main(['render',str(path),'--output',str(tmp_path/'view')])==0
    html=(tmp_path/'view/dispatcher.html').read_text()
    data=json.loads(re.search(r'<script id="dispatcher-data" type="application/json">(.*?)</script>',html,re.S).group(1))
    assert data['scenario']['metadata']['geography']=='wgs84'
    assert isinstance(data['geography']['locations']['a']['lat'],float)
    assert data['scenario']['travel_config']['modes']['public']['kind']=='estimate'
