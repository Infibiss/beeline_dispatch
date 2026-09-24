"""Shared dispatcher/engineer demo; loopback by default, explicit LAN binding."""
import argparse
from html import escape
from copy import deepcopy, copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ipaddress
from pathlib import Path
import socket
import ssl
import threading
from urllib.parse import parse_qs, urlsplit
from .regions import load_session, UnknownRegion



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


def local_ipv4_addresses():
    addresses = {'127.0.0.1'}
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = item[4][0]
            if not ipaddress.ip_address(address).is_unspecified:
                addresses.add(address)
    except OSError:
        pass
    return sorted(addresses)


def create_server(session,state_path,*,port=8766,time_limit=1,address_service=None,explainer=None,host='127.0.0.1',
                  gps_config=None,gps_model=None,disable_gps=False,tls_context=None,region_sources=None,accounts=None,regions=None,solver='hybrid',
                  morning_time_limit=None):
    # Windows spawn imports this module in the model child. Keep OR-Tools and
    # rendering imports inside the server entrypoints, outside that cold path.
    from .dispatcher import render_dispatcher
    from .engineer import engineer_index, engineer_state, record_engineer_fact
    from .completion import confirm_completion
    from .models import Scenario, Plan
    from .report import render_report
    from .gps.http import GpsApi
    from .crew import CrewService
    from .regions import RegionRegistry
    from .solvers.selection import get_solver
    get_solver(solver)  # Validate before opening the optional services.
    # Morning crew/stock planning may search longer than day events; None keeps the day budget.
    morning_time_limit=time_limit if morning_time_limit is None else morning_time_limit
    # Keep an explicit Host allowlist rather than trusting any supplied private IP
    # or hostname. This also preserves the previous foreign-Origin protection.
    address = ipaddress.IPv4Address(host)
    allowed_names = {'127.0.0.1','localhost'}
    if address.is_unspecified:
        allowed_names.update(local_ipv4_addresses())
    else:
        allowed_names.add(str(address))
    address_lock=threading.Lock()
    class SharedAddressClient:
        def get(self,url):
            # Keep the existing provider throttle/cache atomic across regions.
            with address_lock:return address_service.client.get(url)
    def context_factory(current, path, primary):
        addresses = copy(address_service) if address_service is not None else None
        if addresses is not None:
            addresses.client = SharedAddressClient()
            if not primary:addresses.candidates = {}  # Never inherit another region's selection tokens.
        return {'holder': {'session': current}, 'path': path, 'lock': threading.Lock(),
                'crews': CrewService(addresses), 'address_service': addresses,
                'gps': GpsApi(current,config=gps_config,model=gps_model,enabled=not disable_gps)}
    if regions is not None:
        if region_sources is not None or 'current' in regions:
            raise ValueError('Укажите один каталог регионов; current зарезервирован')
        region_sources={'current':{'label':session.scenario.region,'state':state_path},
                        **{key:{'label':other.scenario.region,'state':other_path,'session':other}
                           for key,(other,other_path) in regions.items()}}
    regions=RegionRegistry(session,state_path,context_factory,region_sources)
    from .auth_http import AuthHttp
    auth=AuthHttp(accounts, regions, _STATIC, bool(tls_context))
    scheme='https' if tls_context else 'http'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass

        def context(self):
            self.region_key,self.prefix,self.path,current=regions.resolve(self.path)
            return (current[k] for k in ('holder','path','lock','crews','gps','address_service'))

        def handle(self):
            try:
                super().handle()
            except (ssl.SSLError, ConnectionResetError, BrokenPipeError):
                # Browsers may reject an untrusted development certificate or
                # cancel an in-flight GPS request. Neither is an app exception.
                pass

        def reply(self,status,value,kind='application/json; charset=utf-8',*,rendered=False):
            body=json.dumps(value,ensure_ascii=False,allow_nan=False).encode() if kind.startswith('application/json') else value
            if kind.startswith('text/html'):
                document=body.decode('utf-8')
                prefix=getattr(self,'prefix','')
                if urlsplit(self.path).path in ('/','/crew','/engineer','/accounts'):
                    # Loaded before application scripts; it scopes local URLs and
                    # displays the region selector. No global fetch interception.
                    document=document.replace('<head>', '<head><script src="/app-context.js"></script><link rel="stylesheet" href="/app-context.css">', 1)
                document=document.replace('href="/',f'href="{prefix}/').replace('src="/',f'src="{prefix}/')
                if getattr(self,'identity',None) and self.identity['role']=='engineer':
                    document=document.replace('class="dispatcher-link"', 'class="dispatcher-link" hidden')
                    document=document.replace('Демо-профиль инженера', 'Мой рабочий профиль')
                    document=document.replace('<select id="engineer-select"', '<select hidden id="engineer-select"')
                    document=document.replace('<div class="identity-select">', '<div class="identity-select"><strong id="personal-name"></strong>')
                body=document.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            # OSM requires a real Referer. Cross-origin requests reveal only our
            # origin, never employee IDs, page paths or query parameters.
            self.send_header('Referrer-Policy','strict-origin-when-cross-origin')
            for key,value in getattr(self,'extra_headers',{}).items():self.send_header(key,value)
            inline = " 'unsafe-inline'" if rendered else ''
            tiles = ' https://tile.openstreetmap.org' if rendered or urlsplit(self.path).path in ('/engineer','/') else ''
            self.send_header('Content-Security-Policy',f"default-src 'self'; style-src 'self'{inline}; script-src 'self'{inline}; img-src 'self' data:{tiles}; connect-src 'self'{tiles}; frame-ancestors 'self'; object-src 'none'; base-uri 'self'")
            self.end_headers();self.wfile.write(body)

        def trusted(self):
            hosts={f'{name}:{self.server.server_port}' for name in allowed_names}
            if self.server.server_port==(443 if tls_context else 80):hosts.update(allowed_names)
            authority=self.headers.get('Host','').lower()
            if authority not in hosts:return False
            origin=self.headers.get('Origin')
            return origin is None or origin == scheme+'://'+authority

        def do_GET(self):
            if not self.trusted():return self.reply(403,{'error':'Адрес сервера или источник запроса не разрешён'})
            if auth.before(self):return
            try:holder,path,lock,crews,gps,address_service=self.context()
            except UnknownRegion:return self.reply(404,{'error':'Неизвестный район'})
            except (ValueError,OSError,KeyError,TypeError):return self.reply(400,{'error':'Район недоступен: проверьте набор и отдельный файл состояния'})
            route=urlsplit(self.path)
            if route.path=='/api/regions':
                manifest=regions.manifest(self.region_key)
                if self.identity and self.identity['role']=='engineer':
                    manifest['regions']=[r for r in manifest['regions'] if r['id']==self.identity['region']]
                return self.reply(200,manifest)
            if route.path=='/api/risk':
                try:
                    from .risk_dashboard import dashboard
                    with lock: result=dashboard(holder['session'])
                    return self.reply(200,result)
                except Exception:
                    return self.reply(503,{'error':'Показатели риска временно недоступны. Планирование продолжает работать.'})
            if route.path == '/api/gps' or route.path.startswith('/api/gps/'):
                return self.reply(*gps.request('GET',route.path,route.query))
            if route.path in ('/api/engineers','/api/engineer','/api/revision'):
                try:
                    query=parse_qs(route.query,keep_blank_values=True)
                    with lock:
                        session=holder['session']
                        if route.path=='/api/engineer':
                            if set(query)!={'engineer_id'} or len(query['engineer_id'])!=1:
                                raise ValueError('Укажите одного инженера')
                            result=engineer_state(session,query['engineer_id'][0])
                        else:
                            if query:raise ValueError('Неизвестные параметры запроса')
                            result=engineer_index(session) if route.path=='/api/engineers' else {'version':session.version}
                            if route.path=='/api/engineers' and self.identity and self.identity['role']=='engineer':
                                result['engineers']=[e for e in result['engineers'] if e['id']==self.identity['engineer_id']]
                    return self.reply(200,result)
                except (ValueError,KeyError,TypeError) as exc:
                    return self.reply(400,{'error':str(exc)})
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
            if self.path=='/api/crew':
                with lock:self.reply(200,crews.settings(holder['session']))
                return
            if route.path in ('/crew-plan','/crew-report'):
                try:
                    query=parse_qs(route.query)
                    with lock:candidate=crews.selected(holder['session'],{'id':query['id'][0],'version':int(query['version'][0])})
                    preview=candidate.workbench();preview['operations']['preview']=True
                    if route.path=='/crew-plan':
                        report_url='/crew-report?id='+query['id'][0]+'&amp;version='+str(int(query['version'][0]))
                        document=render_dispatcher(preview).replace('href="report.html"','href="'+report_url+'"',1)
                    else:document=render_report(candidate.scenario,[candidate.plan])
                    return self.reply(200,document.encode(),'text/html; charset=utf-8',rendered=True)
                except (ValueError,KeyError,TypeError) as exc:return self.reply(400,{'error':str(exc)})
            if self.path=='/api/state':
                with lock:self.reply(200,holder['session'].state())
                return
            files={'/':('scenarios.html','text/html; charset=utf-8'),'/scenarios.js':('scenarios.js','text/javascript; charset=utf-8'),'/scenarios.css':('scenarios.css','text/css; charset=utf-8')}
            files.update({'/app-context.js':('app-context.js','text/javascript; charset=utf-8'),'/app-context.css':('app-context.css','text/css; charset=utf-8')})
            files.update({'/risk-dashboard.js':('risk-dashboard.js','text/javascript; charset=utf-8'),'/risk-dashboard.css':('risk-dashboard.css','text/css; charset=utf-8')})
            files.update({'/crew':('crew.html','text/html; charset=utf-8'),'/crew.js':('crew.js','text/javascript; charset=utf-8'),'/crew.css':('crew.css','text/css; charset=utf-8')})
            files.update({'/engineer':('engineer.html','text/html; charset=utf-8'),
                          '/engineer.js':('engineer.js','text/javascript; charset=utf-8'),
                          '/engineer.css':('engineer.css','text/css; charset=utf-8')})
            files.update({f'/{name}':(name,kind) for name,kind in (
                ('gps-engineer.js','text/javascript; charset=utf-8'),
                ('gps-dispatcher.js','text/javascript; charset=utf-8'),
                ('gps.css','text/css; charset=utf-8'))})
            if route.path.startswith('/assets/'):
                name=route.path[len('/assets/'):]
                if name not in _ASSETS:return self.reply(404,{'error':'Не найдено'})
                return self.reply(200,(_STATIC/'vendor'/name).read_bytes(),_ASSETS[name])
            if route.path not in files:return self.reply(404,{'error':'Не найдено'})
            name,kind=files[route.path]
            self.reply(200,(_STATIC/name).read_bytes(),kind)

        def do_POST(self):
            if not self.trusted():return self.reply(403,{'error':'Запрос с другого сайта запрещен'})
            if auth.before(self):return
            try:holder,path,lock,crews,gps,address_service=self.context()
            except (ValueError,OSError,KeyError,TypeError):return self.reply(400,{'error':'Район недоступен: проверьте набор и отдельный файл состояния'})
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':return self.reply(415,{'error':'Ожидается JSON'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                gps_route=urlsplit(self.path)
                is_gps=gps_route.path.startswith('/api/gps/')
                if not 0<length<=(8192 if is_gps and gps_route.path!='/api/gps/replay' else 65536):raise ValueError('Недопустимый размер запроса')
                payload=json.loads(self.rfile.read(length))
                if not isinstance(payload,dict):raise ValueError('Ожидается объект запроса')
                if self.identity and self.identity['role']=='engineer' and payload.get('engineer_id')!=self.identity['engineer_id']:
                    return self.reply(403,{'error':'Можно отправлять только свои статусы и координаты'})
                # Telemetry has its own lock; the solver may be busy for seconds.
                if is_gps:return self.reply(*gps.request('POST',gps_route.path,gps_route.query,payload))
                if self.path in ('/api/explain','/api/recommend'):
                    with lock:
                        session=holder['session']
                        if payload.get('version')!=session.version:raise ValueError('Устаревшая версия плана')
                        stored=session.batches.get(payload.get('batch_id'))
                        if stored is None:raise ValueError('Варианты устарели; рассчитайте заново')
                        batch=deepcopy(stored['batch'])
                    if self.path=='/api/explain' and payload.get('candidate_id') is None:raise ValueError('Нужен вариант для объяснения')
                    if explainer is None:raise ValueError('LLM не настроена')
                    result=explainer.recommend(batch) if self.path=='/api/recommend' else explainer.explain(batch,payload.get('candidate_id'))
                    return self.reply(200,result)
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
                        temporary.replace(path);regions.publish_session(self.region_key,candidate)
                        gps.update(candidate)
                    elif self.path=='/api/crew/preview':
                        result=crews.preview(holder['session'],payload,time_limit=morning_time_limit,solver=solver)
                    elif self.path=='/api/crew/apply':
                        candidate=crews.selected(holder['session'],payload)
                        path.parent.mkdir(parents=True,exist_ok=True)
                        temporary=path.with_suffix(path.suffix+'.tmp')
                        temporary.write_text(json.dumps(candidate.dump(),ensure_ascii=False,indent=2),encoding='utf-8')
                        temporary.replace(path);regions.publish_session(self.region_key,candidate);crews.draft=None
                        gps.update(candidate)
                        result=candidate.state()
                    elif self.path=='/api/preview':
                        result={**holder['session'].preview(payload,time_limit=time_limit,solver=solver),'llm_enabled':explainer is not None}
                    elif self.path in ('/api/approve','/api/facts','/api/mode','/api/engineer/facts','/api/completions/confirm'):
                        # Persist the candidate state before publishing it in memory.
                        candidate=deepcopy(holder['session'])
                        if self.path=='/api/engineer/facts':result=record_engineer_fact(candidate,payload)
                        elif self.path=='/api/completions/confirm':result=confirm_completion(candidate,payload)
                        else:result=candidate.approve(payload) if self.path=='/api/approve' else candidate.set_mode(payload) if self.path=='/api/mode' else candidate.record_fact(payload)
                        path.parent.mkdir(parents=True,exist_ok=True)
                        temporary=path.with_suffix(path.suffix+'.tmp')
                        temporary.write_text(json.dumps(candidate.dump(),ensure_ascii=False,indent=2),encoding='utf-8')
                        temporary.replace(path)
                        regions.publish_session(self.region_key,candidate)
                        gps.update(candidate)
                    else:return self.reply(404,{'error':'Не найдено'})
                self.reply(200,result)
            except (ValueError,KeyError,TypeError) as exc:
                self.reply(409 if 'верси' in str(exc) else 400,{'error':str(exc)})
            except OSError:
                self.reply(500,{'error':'Не удалось сохранить локальное состояние; изменения не применены'})

    class Server(ThreadingHTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(10)
            if tls_context:
                # Handshake in the request thread, not the accepting main thread.
                connection = tls_context.wrap_socket(connection, server_side=True, do_handshake_on_connect=False)
            return connection, address

        def server_close(self):
            super().server_close()
            regions.close()

    try:
        server=Server((host,port),Handler)
        server.gps=regions.contexts[regions.default_key]['gps']
        return server
    except Exception:
        regions.close()
        raise


def point_preview(point):
    return ('<!doctype html><html lang="ru"><meta charset="utf-8"><link rel="stylesheet" href="/assets/leaflet.css">'
        '<style>body{margin:0;font:13px system-ui}p{padding:8px;margin:0}#map{height:240px}</style>'
        '<p>'+escape(point.label)+'</p><div id="map"></div><script src="/assets/leaflet.js"></script><script>'
        f'const p=[{point.lat},{point.lon}];const m=L.map("map").setView(p,17);'
        'L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",{referrerPolicy:"strict-origin-when-cross-origin",attribution:"© OpenStreetMap"}).addTo(m);L.marker(p).addTo(m);</script></html>')


def main(argv=None):
    from .address_service import AddressService
    from .geo_http import CachedHttp
    from .models import Scenario, Plan
    from .replanning import ScenarioSession
    parser=argparse.ArgumentParser(description='Локальный Scenario Center: симуляция событий')
    parser.add_argument('comparison',type=Path)
    parser.add_argument('--state',type=Path,default=Path('output/scenario-state.json'))
    parser.add_argument('--regions-dir',type=Path,help='Каталог сравнений регионов; по умолчанию соседние демо-участки')
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--host',default='127.0.0.1',help='IPv4 для прослушивания; 0.0.0.0 открывает доступ с телефона в локальной сети')
    parser.add_argument('--network',action='store_true',help='Разрешить поиск адресов через Photon и координатные запросы OSRM')
    parser.add_argument('--cache',type=Path,default=Path('output/geo-cache'))
    parser.add_argument('--time-limit',type=float,default=3,help='Бюджет пересчёта после события дня, секунды')
    parser.add_argument('--morning-time-limit',type=float,default=10,
                        help='Бюджет утреннего плана (бригады, старты, выдача), секунды; ALNS v2 включается от 2,5 с на вызов')
    parser.add_argument('--solver',choices=['ortools','alns','hybrid'],default='hybrid',help='Оптимизатор; hybrid делит бюджет между OR-Tools и ALNS')
    parser.add_argument('--gps-config',type=Path)
    parser.add_argument('--gps-model',help='Replaceable Python module:class with analyze(job)')
    parser.add_argument('--disable-gps',action='store_true')
    parser.add_argument('--accounts',type=Path,help='База учётных записей; по умолчанию рядом с файлом состояния')
    parser.add_argument('--certfile',type=Path,help='TLS certificate with IP/hostname SAN')
    parser.add_argument('--keyfile',type=Path,help='TLS private key (never served over HTTP)')
    args=parser.parse_args(argv)
    try:ipaddress.IPv4Address(args.host)
    except ValueError:parser.error('--host должен быть IPv4-адресом, например 127.0.0.1 или 0.0.0.0')
    if args.state.resolve()==args.comparison.resolve():parser.error('Файл состояния не должен перезаписывать исходное сравнение')
    session=load_session(args.comparison,args.state)
    tls=None
    if bool(args.certfile)!=bool(args.keyfile):parser.error('Укажите --certfile и --keyfile вместе')
    if args.certfile:
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version=ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(args.certfile,args.keyfile)
    scheme='https' if tls else 'http'
    from .llm import configured
    from .regions import discover_regions
    from .auth import AccountStore
    import os
    accounts=AccountStore(args.accounts or args.state.with_name('accounts.sqlite3'))
    initial_password=accounts.bootstrap(os.environ.get('FIELD_ROUTER_ADMIN_PASSWORD'))
    if initial_password:
        print('Первый вход: admin / '+initial_password+'\nСохраните пароль: он показывается только при создании базы.',flush=True)
    explainer=configured()
    server=create_server(session,args.state,port=args.port,time_limit=args.time_limit,morning_time_limit=args.morning_time_limit,solver=args.solver,explainer=explainer,address_service=AddressService(CachedHttp(args.cache),routed_access=True) if args.network else None,host=args.host,
                         gps_config=args.gps_config,gps_model=args.gps_model,disable_gps=args.disable_gps,tls_context=tls,
                         region_sources=discover_regions(args.comparison,args.state,args.regions_dir),accounts=accounts)
    print('Учётные записи: '+str(accounts.path.resolve()),flush=True)
    addresses=local_ipv4_addresses() if args.host=='0.0.0.0' else [args.host]
    for address in addresses:
        print(f'Диспетчер: {scheme}://{address}:{server.server_port}/',flush=True)
        print(f'Инженер:   {scheme}://{address}:{server.server_port}/engineer',flush=True)
    if args.host!='127.0.0.1' and not tls:print('Для входа и GPS с телефона используйте HTTPS (--certfile/--keyfile или -Https).',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':main()
