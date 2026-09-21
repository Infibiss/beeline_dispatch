"""Local entry points; never publish a solver result before validation."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

from .importer import import_csv
from .models import Engineer, Scenario
from .geo_models import GeographyBundle
from .geo_http import CachedHttp
from .geography import geocode_scenario, attach_geography
from .road_travel import build_matrices, bundle_travel, route_shapes, current_shapes
from .report import render_report
from .scenario import prepare_scenario
from .solvers.baseline import BaselineSolver
from .solvers.ortools_solver import OrToolsSolver
from .solvers.selection import get_solver
from .travel import synthetic_travel
from .validation import require_valid
from .export import write_dispatcher
from .models import Plan


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


def _prepare(args, input_path=None, region=None):
    source_path = Path(input_path or args.input)
    source = import_csv(source_path)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    engineers = None
    if getattr(args,"engineers",None):
        engineers = [Engineer.model_validate(e) for e in json.loads(Path(args.engineers).read_text(encoding="utf-8"))]
    scenario = prepare_scenario(source,region or args.region,config,
                                synthetic_geography=args.synthetic_geography,engineers=engineers)
    return scenario.model_copy(update={"metadata":{**scenario.metadata,
        "source_sha256":hashlib.sha256(source_path.read_bytes()).hexdigest()}})


def _calculate(scenario, output, time_limit, solver=None, geography=None, shape_client=None):
    if geography is not None:
        geography = geography.model_copy(update={"route_shapes":{}})
        scenario = attach_geography(scenario,geography)
        travel = bundle_travel(geography)
        scenario = scenario.model_copy(update={"travel_config":{"modes":geography.modes}})
    else:
        travel = synthetic_travel(scenario)
    plans = []
    if solver in (None,"baseline"):
        plans.append(BaselineSolver().solve(scenario,travel))
    if solver in (None,"ortools"):
        plans.append(OrToolsSolver().solve(scenario,travel,time_limit_seconds=time_limit))
    if solver in ('alns','hybrid'):
        plans.append(get_solver(solver).solve(scenario,travel,time_limit_seconds=time_limit))
    for plan in plans:
        require_valid(scenario,travel,plan)
    if geography is not None and shape_client is not None:
        geography = route_shapes(geography,scenario,plans,shape_client)
    output = Path(output)
    payload = {"scenario":scenario.model_dump(mode="json"),"plans":[p.model_dump(mode="json") for p in plans],
               "validation":{"valid":True,"violations":[]}}
    if geography is not None:
        payload["geography"] = geography.model_dump(mode="json")
    _write_json(output/"comparison.json",payload)
    (output/"report.html").write_text(render_report(scenario,plans),encoding="utf-8")
    write_dispatcher(payload,output)
    for plan in plans:
        m = plan.metrics
        print(f"{scenario.region} | {plan.solver}: {m.assigned}/{len(scenario.requests)} заявок, "
              f"{m.engineers_used} инженеров, {m.distance_m/1000:.2f} км модели; {plan.status}")
    print(f"Отчет: {(output/'report.html').resolve()}")
    return plans


def parser():
    root = argparse.ArgumentParser(description="Планирование дня: baseline и OR-Tools. Офлайн-география синтетическая.")
    commands = root.add_subparsers(dest="command",required=True)
    inspect = commands.add_parser("inspect",help="Проверить синтетический CSV без планирования")
    inspect.add_argument("input",type=Path)
    prepare = commands.add_parser("prepare",help="Подготовить заменяемый Scenario JSON")
    prepare.add_argument("--input",required=True,type=Path)
    prepare.add_argument("--region",required=True)
    prepare.add_argument("--config",type=Path,default=Path("config/demo.json"))
    prepare.add_argument("--engineers",type=Path,help="JSON-массив инженеров вместо синтетического справочника")
    prepare.add_argument("--synthetic-geography",action="store_true",help="Явно разрешить условную географию вместо реальных координат")
    prepare.add_argument("--output",required=True,type=Path)
    for name in ("compare","solve"):
        cmd = commands.add_parser(name,help="Рассчитать и проверить план, сохранить JSON/HTML")
        cmd.add_argument("scenario",type=Path)
        cmd.add_argument("--output",required=True,type=Path)
        cmd.add_argument("--geography",type=Path,help="Полная география с матрицами; расчет без сетевых запросов")
        cmd.add_argument("--route-shapes",action="store_true",help="Получить геометрию выбранных дорожных маршрутов")
        cmd.add_argument("--network",action="store_true",help="Разрешить внешние запросы; иначе только кэш")
        cmd.add_argument("--cache",type=Path,default=Path("output/geo-cache"))
        cmd.add_argument("--time-limit",type=float,default=5,help="Бюджет поиска на набор, секунды")
        if name=="solve":
            cmd.add_argument("--solver",choices=["baseline","ortools","alns","hybrid"],required=True)
    demo = commands.add_parser("demo",help="Подготовить и сравнить три исходных синтетических набора")
    demo.add_argument("--data-dir",type=Path,default=Path("data/Обезличивание"))
    demo.add_argument("--config",type=Path,default=Path("config/demo.json"))
    demo.add_argument("--synthetic-geography",action="store_true")
    demo.add_argument("--time-limit",type=float,default=5)
    demo.add_argument("--output",type=Path,default=Path("output/demo"))
    geo = commands.add_parser("geocode",help="Геокодировать адреса сценария, сохранить список неоднозначных")
    geo.add_argument("scenario",type=Path)
    geo.add_argument("--output",required=True,type=Path)
    geo.add_argument("--endpoint",default="https://photon.komoot.io/api/")
    geo.add_argument("--overrides",type=Path,help="JSON {location_id: проверенные координаты}")
    geo.add_argument("--network",action="store_true",help="Разрешить передачу адресов геокодеру")
    geo.add_argument("--cache",type=Path,default=Path("output/geo-cache"))
    matrix = commands.add_parser("matrices",help="Получить дорожные матрицы для проверенной географии")
    matrix.add_argument("geography",type=Path)
    matrix.add_argument("--output",required=True,type=Path)
    matrix.add_argument("--endpoints",type=Path,help="JSON {car,walk,bicycle}: URL корней OSRM")
    matrix.add_argument("--allow-public-estimate",action="store_true")
    matrix.add_argument("--network",action="store_true",help="Разрешить передачу координат маршрутизатору")
    matrix.add_argument("--cache",type=Path,default=Path("output/geo-cache"))
    render = commands.add_parser("render",help="Проверить сохраненное сравнение и создать рабочее место без повторного поиска")
    render.add_argument("comparison",type=Path)
    render.add_argument("--output",required=True,type=Path)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command=="render":
            payload = json.loads(args.comparison.read_text(encoding="utf-8"))
            scenario = Scenario.model_validate(payload["scenario"])
            plans = [Plan.model_validate(p) for p in payload["plans"]]
            geo = GeographyBundle.model_validate(payload["geography"]) if payload.get("geography") else None
            if geo is not None:
                scenario = attach_geography(scenario,geo)
                scenario = scenario.model_copy(update={"travel_config":{"modes":geo.modes}})
                travel = bundle_travel(geo)
            else:
                travel = synthetic_travel(scenario)
            for plan in plans: require_valid(scenario,travel,plan)
            payload["scenario"] = scenario.model_dump(mode="json")
            payload["plans"] = [p.model_dump(mode="json") for p in plans]
            if geo is not None:
                geo = current_shapes(geo,scenario,plans)
                payload["geography"] = geo.model_dump(mode="json")
            payload["validation"] = {"valid":True,"violations":[]}
            write_dispatcher(payload,args.output)
            (args.output/"report.html").write_text(render_report(scenario,plans),encoding="utf-8")
            print(f"Рабочее место: {(args.output/'dispatcher.html').resolve()}")
            return 0
        if args.command=="geocode":
            if args.output.resolve()==args.scenario.resolve(): raise ValueError("Не перезаписывайте исходный сценарий")
            scenario = Scenario.model_validate_json(args.scenario.read_text(encoding="utf-8"))
            overrides = json.loads(args.overrides.read_text()) if args.overrides else None
            geo = geocode_scenario(scenario,CachedHttp(args.cache,offline=not args.network),endpoint=args.endpoint,overrides=overrides,
                progress=lambda i,n,key,ok: print(f"Геокодирование {i}/{n}: {key} {'найден' if ok else 'нужна проверка'}",flush=True))
            _write_json(args.output,geo.model_dump(mode="json"))
            print(f"Координаты: {len(geo.locations)}, требуют проверки: {len(geo.unresolved)}; {args.output}")
            return 2 if geo.unresolved else 0
        if args.command=="matrices":
            geo = GeographyBundle.model_validate_json(args.geography.read_text(encoding="utf-8"))
            endpoints = json.loads(args.endpoints.read_text()) if args.endpoints else None
            geo = build_matrices(geo,CachedHttp(args.cache,offline=not args.network),allow_public_estimate=args.allow_public_estimate,endpoints=endpoints,routed_access=True)
            _write_json(args.output,geo.model_dump(mode="json"))
            print(f"Матрицы сохранены: {args.output}")
            return 0
        if args.command=="inspect":
            result = import_csv(args.input)
            print(json.dumps({"source":result.source,"encoding":result.encoding,"requests":len(result.requests),
                "office":result.office_address,"blank_rows":result.blank_rows,
                "errors":[e.model_dump() for e in result.errors]},ensure_ascii=False,indent=2))
            return 2 if result.errors else 0
        if args.command=="prepare":
            if args.output.resolve()==args.input.resolve():
                raise ValueError("Выходной файл не должен совпадать с исходным")
            scenario = _prepare(args)
            _write_json(args.output,scenario.model_dump(mode="json"))
            print(f"Подготовлено {len(scenario.requests)} заявок, {len(scenario.engineers)} инженеров: {args.output}")
        elif args.command in ("compare","solve"):
            scenario = Scenario.model_validate_json(args.scenario.read_text(encoding="utf-8"))
            geo = GeographyBundle.model_validate_json(args.geography.read_text(encoding="utf-8")) if args.geography else None
            if args.route_shapes and geo is None: raise ValueError("Геометрия требует --geography")
            client = CachedHttp(args.cache,offline=not args.network) if args.route_shapes else None
            _calculate(scenario,args.output,args.time_limit,getattr(args,"solver",None),geo,client)
        elif args.command=="demo":
            files = sorted(args.data_dir.glob("*Синтетические данные.csv"))
            if not files:
                raise ValueError("Синтетические CSV не найдены")
            summary = []
            for source in files:
                region = source.name.removesuffix(" Синтетические данные.csv")
                scenario = _prepare(args,source,region)
                target = args.output/region
                _write_json(target/"scenario.json",scenario.model_dump(mode="json"))
                plans = _calculate(scenario,target,args.time_limit)
                summary.append({"region":region,"results":[{"solver":p.solver,**p.metrics.model_dump(),"status":p.status} for p in plans]})
            _write_json(args.output/"summary.json",summary)
            for item in summary:
                target = args.output/item["region"]
                write_dispatcher(json.loads((target/"comparison.json").read_text(encoding="utf-8")),target)
        return 0
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print(f"Ошибка: {exc}",file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())
