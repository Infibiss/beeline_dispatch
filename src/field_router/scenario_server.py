"""Loopback Scenario Center; optional geocoding/routing, no LLM calls."""
import argparse
from html import escape
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit

from .dispatcher import render_dispatcher
from .address_service import AddressService
from .geo_http import CachedHttp
from .models import Scenario, Plan
from .replanning import ScenarioSession
from .report import render_report


_STATIC=Path(__file__).with_name('static')
_ASSETS = {'leaflet.js': 'text/javascript; charset=utf-8',
           'leaflet.css': 'text/css; charset=utf-8',
           **{f'images/{name}.png': 'image/png' for name in
              ('layers', 'layers-2x', 'marker-icon', 'marker-icon-2x', 'marker-shadow')}}


def _version(query):
    values = parse_qs(query, keep_blank_values=True)
    if not values:
        return None
    versions = values.get('version', [])
    if set(values) != {'version'} or len(versions) != 1 or not versions[0].isascii() or not versions[0].isdigit() or int(versions[0]) < 1:
        raise ValueError('Ожидается положительный номер версии')
    return int(versions[0])


def create_server(session,state_path,*,port=8766,time_limit=1,address_service=None):
    path=Path(state_path)
    lock=threading.Lock()
    holder={'session':session}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass

        def reply(self,status,value,kind='application/json; charset=utf-8',*,rendered=False):
            body=json.dumps(value,ensure_ascii=False,allow_nan=False).encode() if kind.startswith('application/json') else value
            self.send_response(status)
            self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            inline = " 'unsafe-inline'" if rendered else ''
            tiles = ' https://tile.openstreetmap.org' if rendered else ''
            self.send_header('Content-Security-Policy',f"default-src 'self'; style-src 'self'{inline}; script-src 'self'{inline}; img-src 'self' data:{tiles}; connect-src 'self'{tiles}; frame-ancestors 'self'; object-src 'none'; base-uri 'self'")
            self.end_headers();self.wfile.write(body)

        def trusted(self):
            hosts={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
            if self.headers.get('Host') not in hosts:return False
            origin=self.headers.get('Origin')
            return origin is None or origin in {'http://'+h for h in hosts}

        def do_GET(self):
            if not self.trusted():return self.reply(403,{'error':'Разрешен только локальный доступ'})
            route=urlsplit(self.path)
            if route.path=='/address-preview':
                try:
                    if address_service is None:raise ValueError('Поиск адресов выключен')
                    query=parse_qs(route.query)
                    with lock:point=address_service.selected(holder['session'],{'version':int(query['version'][0]),'candidate_id':query['candidate_id'][0]})
                    document=point_preview(point)
                    return self.reply(200,document.encode(),'text/html; charset=utf-8',rendered=True)
                except (ValueError,KeyError,TypeError):return self.reply(400,{'error':'Точка устарела, повторите поиск'})
            if route.path in ('/dispatcher','/report','/report.html'):
                try:
                    version=_version(route.query)
                    with lock:
                        session=holder['session']
                        payload=session.workbench(version)
                        selected_version=version if version is not None else session.version
                    if route.path=='/dispatcher':
                        document=render_dispatcher(payload).replace('href="report.html"',f'href="/report?version={selected_version}"',1)
                    else:
                        document=render_report(Scenario.model_validate(payload['scenario']),[Plan.model_validate(p) for p in payload['plans']])
                    return self.reply(200,document.encode('utf-8'),'text/html; charset=utf-8',rendered=True)
                except (ValueError,KeyError,TypeError) as exc:
                    return self.reply(400,{'error':str(exc)})
            if self.path=='/api/state':
                with lock:self.reply(200,holder['session'].state())
                return
            files={'/':('scenarios.html','text/html; charset=utf-8'),'/scenarios.js':('scenarios.js','text/javascript; charset=utf-8'),'/scenarios.css':('scenarios.css','text/css; charset=utf-8')}
            if route.path.startswith('/assets/'):
                name=route.path[len('/assets/'):]
                if name not in _ASSETS:return self.reply(404,{'error':'Не найдено'})
                return self.reply(200,(_STATIC/'vendor'/name).read_bytes(),_ASSETS[name])
            if self.path not in files:return self.reply(404,{'error':'Не найдено'})
            name,kind=files[self.path]
            self.reply(200,(_STATIC/name).read_bytes(),kind)

        def do_POST(self):
            if not self.trusted():return self.reply(403,{'error':'Запрос с другого сайта запрещен'})
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':return self.reply(415,{'error':'Ожидается JSON'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=65536:raise ValueError('Недопустимый размер запроса')
                payload=json.loads(self.rfile.read(length))
                if not isinstance(payload,dict):raise ValueError('Ожидается объект запроса')
                with lock:
                    if self.path=='/api/address/search':
                        if address_service is None:raise ValueError('Поиск адресов выключен; запустите сервер с --network')
                        result=address_service.search(holder['session'],payload)
                    elif self.path=='/api/address/confirm':
                        if address_service is None:raise ValueError('Поиск адресов выключен')
                        candidate=deepcopy(holder['session'])
                        result=address_service.confirm(candidate,payload)
                        path.parent.mkdir(parents=True,exist_ok=True)
                        temporary=path.with_suffix(path.suffix+'.tmp')
                        temporary.write_text(json.dumps(candidate.dump(),ensure_ascii=False,indent=2),encoding='utf-8')
                        temporary.replace(path);holder['session']=candidate
                    elif self.path=='/api/preview':
                        result=holder['session'].preview(payload,time_limit=time_limit)
                    elif self.path in ('/api/approve','/api/facts','/api/mode'):
                        # Persist the candidate state before publishing it in memory.
                        candidate=deepcopy(holder['session'])
                        result=candidate.approve(payload) if self.path=='/api/approve' else candidate.set_mode(payload) if self.path=='/api/mode' else candidate.record_fact(payload)
                        path.parent.mkdir(parents=True,exist_ok=True)
                        temporary=path.with_suffix(path.suffix+'.tmp')
                        temporary.write_text(json.dumps(candidate.dump(),ensure_ascii=False,indent=2),encoding='utf-8')
                        temporary.replace(path)
                        holder['session']=candidate
                    else:return self.reply(404,{'error':'Не найдено'})
                self.reply(200,result)
            except (ValueError,KeyError,TypeError) as exc:
                self.reply(409 if 'верси' in str(exc) else 400,{'error':str(exc)})
            except OSError:
                self.reply(500,{'error':'Не удалось сохранить локальное состояние; изменения не применены'})

    return ThreadingHTTPServer(('127.0.0.1',port),Handler)


def point_preview(point):
    return ('<!doctype html><html lang="ru"><meta charset="utf-8"><link rel="stylesheet" href="/assets/leaflet.css">'
        '<style>body{margin:0;font:13px system-ui}p{padding:8px;margin:0}#map{height:240px}</style>'
        '<p>'+escape(point.label)+'</p><div id="map"></div><script src="/assets/leaflet.js"></script><script>'
        f'const p=[{point.lat},{point.lon}];const m=L.map("map").setView(p,17);'
        'L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",{attribution:"© OpenStreetMap"}).addTo(m);L.marker(p).addTo(m);</script></html>')


def main(argv=None):
    parser=argparse.ArgumentParser(description='Локальный Scenario Center: симуляция событий')
    parser.add_argument('comparison',type=Path)
    parser.add_argument('--state',type=Path,default=Path('output/scenario-state.json'))
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--network',action='store_true',help='Разрешить поиск адресов через Photon и координатные запросы OSRM')
    parser.add_argument('--cache',type=Path,default=Path('output/geo-cache'))
    parser.add_argument('--time-limit',type=float,default=1)
    args=parser.parse_args(argv)
    if args.state.resolve()==args.comparison.resolve():parser.error('Файл состояния не должен перезаписывать исходное сравнение')
    if args.state.exists():
        session=ScenarioSession.restore(json.loads(args.state.read_text()))
    else:
        payload=json.loads(args.comparison.read_text())
        scenario=Scenario.model_validate(payload['scenario'])
        plan=next((p for p in payload['plans'] if p['solver']=='ortools'),payload['plans'][0])
        session=ScenarioSession(scenario,Plan.model_validate(plan),geography=payload.get('geography'))
    server=create_server(session,args.state,port=args.port,time_limit=args.time_limit,address_service=AddressService(CachedHttp(args.cache)) if args.network else None)
    print(f'Scenario Center: http://127.0.0.1:{server.server_port} · локальный диспетчер, без GPS',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':main()
