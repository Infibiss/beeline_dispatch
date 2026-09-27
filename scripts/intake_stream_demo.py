"""Isolated six-request live intake sandbox; arrivals start from its UI button.

Run from the repository root: python scripts/intake_stream_demo.py
The state is intentionally separate from the regular day and the 8767 demo.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path

from field_router.regions import load_session
from field_router.scenario_server import create_server

from intake_demo import build_stream_baseline, demo_requests


def main(argv=None):
    parser=argparse.ArgumentParser(description='Поток заявок каждые 10 секунд')
    parser.add_argument('--comparison',type=Path,default=Path('data/demo-road/Восток.json'))
    parser.add_argument('--state',type=Path,default=Path('output/local/demo-stream.json'))
    parser.add_argument('--port',type=int,default=8768)
    args=parser.parse_args(argv)
    if args.state.resolve()==args.comparison.resolve():
        parser.error('Демо-состояние не должно совпадать с исходными данными')
    baseline=build_stream_baseline(args.comparison)
    if args.state.exists():
        session=load_session(args.comparison,args.state)
        print('Продолжаем существующую потоковую песочницу; файл не перезаписан:',args.state,flush=True)
    else:
        session=deepcopy(baseline)
        args.state.parent.mkdir(parents=True,exist_ok=True)
        temporary=args.state.with_suffix(args.state.suffix+'.tmp')
        temporary.write_text(json.dumps(session.dump(),ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(args.state)
        print('Создана потоковая песочница:',args.state,flush=True)
    from field_router.llm import configured
    server=create_server(session,args.state,port=args.port,host='127.0.0.1',
                         time_limit=3,solver='hybrid',disable_gps=True,
                         explainer=configured(),accounts=None,
                         demo_stream_requests=demo_requests(baseline,prefix='demo-stream'),
                         demo_reset_session=baseline)
    print(f'Откройте http://127.0.0.1:{server.server_port}/ и нажмите «Запустить поток».',flush=True)
    print('Шесть заявок по одной каждые 10 секунд; режим меняется в интерфейсе до запуска или во время потока.',flush=True)
    print('«Начать заново» возвращает 09:00, исходный план и пустую историю; предыдущий прогон архивируется.',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':
    main()
