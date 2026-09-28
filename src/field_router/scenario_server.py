"""Shared dispatcher/engineer demo; loopback by default, explicit LAN binding."""
import argparse
from datetime import datetime, timezone
from html import escape
from copy import deepcopy, copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hmac
import ipaddress
import os
import re
from pathlib import Path
import socket
import ssl
import threading
from urllib.parse import parse_qs, urlsplit, urlencode
from uuid import uuid4
from .regions import load_session, UnknownRegion

DAY_TIME_LIMIT=30
MORNING_TIME_LIMIT=120


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


def validate_public_origin(value):
    """Explicit external HTTPS origin; forwarding headers never establish trust."""
    if value is None:
        return None
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == 'https' and parsed.hostname and
                 not parsed.username and not parsed.password and
                 not parsed.path and not parsed.query and not parsed.fragment and
                 re.fullmatch(r'[a-z0-9.-]+(?::[0-9]+)?', parsed.netloc) and
                 (parsed.port is None or 1 <= parsed.port <= 65535) and
                 value == 'https://' + parsed.netloc)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError('public origin: укажите HTTPS-адрес без пути, например https://demo.onrender.com')
    return value


def create_server(session,state_path,*,port=8766,time_limit=1,address_service=None,explainer=None,host='127.0.0.1',
                  gps_config=None,gps_model=None,disable_gps=False,tls_context=None,region_sources=None,accounts=None,regions=None,solver='hybrid',
                  morning_time_limit=None,public_origin=None,gps_routing=None,intake_token=None,demo_stream_requests=None,
                  demo_reset_session=None):
    public_origin = validate_public_origin(public_origin)
    # Windows spawn imports this module in the model child. Keep OR-Tools and
    # rendering imports inside the server entrypoints, outside that cold path.
    from .dispatcher import render_dispatcher
    from .engineer import engineer_index, engineer_state, record_engineer_fact
    from .completion import confirm_completion
    from .models import Scenario, Plan
    from .report import render_report
    from .gps.http import GpsApi
    from .gps.eta import load_routing
    routing_config = load_routing(gps_routing)
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
                'gps': GpsApi(current,config=gps_config,model=gps_model,enabled=not disable_gps,
                              history_path=Path(path).with_suffix('.gps.sqlite3'),routing=routing_config)}
    if regions is not None:
        if region_sources is not None or 'current' in regions:
            raise ValueError('Укажите один каталог регионов; current зарезервирован')
        region_sources={'current':{'label':session.scenario.region,'state':state_path},
                        **{key:{'label':other.scenario.region,'state':other_path,'session':other}
                           for key,(other,other_path) in regions.items()}}
    regions=RegionRegistry(session,state_path,context_factory,region_sources)
    demo_baseline=deepcopy(demo_reset_session) if demo_reset_session is not None else None
    if demo_baseline is not None:
        if demo_baseline.scenario.region != session.scenario.region:
            regions.close()
            raise ValueError('Исходное состояние демо принадлежит другому району')
        context=regions.contexts[regions.default_key]
        saved_run_id=None
        if context['path'].exists():
            saved_run_id=json.loads(context['path'].read_text(encoding='utf-8')).get('demo_run_id')
        context['demo_run_id']=(saved_run_id if isinstance(saved_run_id,str) and
            re.fullmatch(r'[a-f0-9]{32}',saved_run_id) else uuid4().hex)

    def demo_fields(region_key):
        primary=region_key==regions.default_key
        return {'demo_stream_enabled':primary and demo_stream_requests is not None,
                'demo_reset_enabled':primary and demo_baseline is not None,
                'demo_run_id':regions.contexts[region_key].get('demo_run_id')}

    def check_demo_run(region_key,payload,*,required=False):
        current=regions.contexts[region_key].get('demo_run_id')
        if current is not None and (required or 'demo_run_id' in payload):
            if payload.get('demo_run_id')!=current:
                raise ValueError('Сценарий уже перезапущен; загрузите текущую версию')
    from .auth_http import AuthHttp
    auth=AuthHttp(accounts, regions, _STATIC, bool(tls_context) or bool(public_origin))
    from .gps.mobile import NativeGpsApi
    native_gps=NativeGpsApi(accounts, regions, bool(tls_context) or bool(public_origin), public_origin=public_origin)
    scheme='https' if tls_context else 'http'
    intake_token = intake_token if intake_token is not None else os.environ.get('FIELD_ROUTER_INTAKE_TOKEN')

    def persist(region_key, context, candidate, *, demo_run_id=None):
        path=context['path']
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix(path.suffix+'.tmp')
        saved=candidate.dump()
        run_id=demo_run_id if demo_run_id is not None else context.get('demo_run_id')
        if run_id is not None:saved['demo_run_id']=run_id
        temporary.write_text(json.dumps(saved,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(path)
        regions.publish_session(region_key,candidate)
        if run_id is not None:context['demo_run_id']=run_id
        context['gps'].update(candidate)

    stop_autopilot=threading.Event()
    wake_autopilot=threading.Event()

    def enrich_decisions(region_key,context,batch,chosen_id,version,automatic,demo_run_id=None):
        """Attach optional LLM wording after the verified decision is durable."""
        if explainer is None:return
        with context['lock']:
            if context.get('demo_run_id')!=demo_run_id:return
        try:
            # The solver has already chosen and applied this candidate. A fresh
            # recommendation can pick another equally eligible plan, which used
            # to discard otherwise valid model output for the applied plan.
            explanation=explainer.explain(batch,chosen_id)
        except Exception:
            explanation={'source':'template','notice':'YandexGPT не ответила; проверенное объяснение сохранено.'}
        model_ready=explanation.get('source')=='yandexgpt'
        candidate=next((item for item in batch['candidates'] if item['id']==chosen_id),None)
        if candidate is None:return
        from .llm import decision_detail
        with context['lock']:
            # A reset can reuse version and candidate IDs while this provider
            # call is in flight. Its wording belongs only to the original run.
            if context.get('demo_run_id')!=demo_run_id:return
            current=context['holder']['session']
            log=deepcopy(current)
            changed=False
            affected=[]
            for entry in log.decision_log:
                if entry['version']==version and entry['candidate_id']==chosen_id:
                    entry['llm_status']='ready' if model_ready else 'unavailable'
                    entry['llm_notice']=(
                        'YandexGPT выбрала акценты для сравнения планов; факты каждого назначения и риски проверены расчётом.'
                        if model_ready else explanation.get('notice','YandexGPT недоступна; проверенное объяснение сохранено.'))
                    if model_ready:
                        entry['plan_explanation']=explanation['text']
                        entry['llm_explanation']=decision_detail(entry,candidate)
                    affected.append(entry['id'])
                    changed=True
            for notice in log.dispatch_notifications:
                if notice.get('version')==version and notice.get('kind') in ('auto_applied','manual_applied'):
                    notice['llm_status']='ready' if model_ready else 'unavailable'
                    notice['llm_notice']=(
                        'Пояснение YandexGPT готово; назначения и риски проверены алгоритмом.'
                        if model_ready else explanation.get('notice','YandexGPT недоступна; проверенное объяснение сохранено.'))
                    if model_ready:notice['plan_explanation']=explanation['text']
                    if explanation.get('card'):notice['explanation_card']=explanation['card']
                    changed=True
            if changed:
                if model_ready:
                    log.dispatch_notifications.append({'id':uuid4().hex,'kind':'explanation_ready',
                        'time':datetime.now(timezone.utc).isoformat(),'version':version,
                        'title':f'Готово пояснение к плану №{version}',
                        'text':'YandexGPT выделила важные компромиссы. Индивидуальные факты назначений и риски доступны в журнале решений.',
                        'request_ids':affected})
                try:persist(region_key,context,log)
                except OSError:pass

    def autopilot_loop():
        from .intake import apply_autopilot
        while not stop_autopilot.is_set():
            wake_autopilot.wait(2)
            wake_autopilot.clear()
            if stop_autopilot.is_set():break
            with regions.lock: active=list(regions.contexts.items())
            for region_key, context in active:
                batch_choice=None
                with context['lock']:
                    current=context['holder']['session']
                    if not current.autopilot_due():continue
                    candidate=deepcopy(current)
                    try:
                        batch_choice=apply_autopilot(candidate,time_limit=time_limit,solver=solver)
                    except Exception:
                        candidate=deepcopy(current)
                        candidate.record_autopilot_failure('Не удалось построить допустимый план. Заявки остались в очереди; проверьте их вручную.')
                    try:persist(region_key,context,candidate)
                    except OSError:continue
                    demo_run_id=context.get('demo_run_id')
                if batch_choice and not stop_autopilot.is_set():
                    batch, chosen_id=batch_choice
                    # The provider can take tens of seconds; keep ingestion and
                    # later autopilot batches responsive while it is answering.
                    threading.Thread(target=enrich_decisions,
                        args=(region_key,context,batch,chosen_id,candidate.version,True,demo_run_id),
                        daemon=True).start()

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
            self.end_headers()
            if not getattr(self,'_head_only',False):self.wfile.write(body)

        def trusted(self):
            hosts={f'{name}:{self.server.server_port}' for name in allowed_names}
            if self.server.server_port==(443 if tls_context else 80):hosts.update(allowed_names)
            authority=self.headers.get('Host','').lower()
            if public_origin and authority == urlsplit(public_origin).netloc:
                origin=self.headers.get('Origin')
                return origin is None or origin == public_origin
            if authority not in hosts:return False
            origin=self.headers.get('Origin')
            return origin is None or origin == scheme+'://'+authority

        def do_HEAD(self):
            # Only the APK exposes HEAD. Run exactly the same authentication as
            # GET, but suppress every response body (including login redirects).
            self._head_only=True
            route=urlsplit(self.path).path.split('/')
            target='/'+'/'.join(route[3:]) if len(route)>3 and route[1] in ('r','regions') else urlsplit(self.path).path
            if target!='/engineer-app.apk':
                self.extra_headers={'Allow':'GET, POST'}
                return self.reply(405,{'error':'HEAD доступен только для скачивания приложения'})
            original=self.command
            try:
                self.command='GET'
                return self.do_GET()
            finally:
                self.command=original

        def do_GET(self):
            if not self.trusted():return self.reply(403,{'error':'Адрес сервера или источник запроса не разрешён'})
            if self.path == '/healthz':return self.reply(200,{'status':'ok'})
            if native_gps.receive(self):return
            if auth.before(self):return
            if native_gps.authenticated(self):return
            try:holder,path,lock,crews,gps,address_service=self.context()
            except UnknownRegion:return self.reply(404,{'error':'Неизвестный район'})
            except (ValueError,OSError,KeyError,TypeError):return self.reply(400,{'error':'Район недоступен: проверьте набор и отдельный файл состояния'})
            route=urlsplit(self.path)
            if route.path=='/engineer-app.apk':
                apk=_STATIC.parents[2]/'output'/'android'/'field-engineer-debug.apk'
                if not apk.is_file():
                    return self.reply(404,{'error':'Android-приложение ещё не собрано. Обратитесь к диспетчеру.'})
                from .apk_download import apk_response
                try:
                    status,body,headers=apk_response(apk,
                        None if getattr(self,'_head_only',False) else self.headers.get('Range'),
                        self.headers.get('If-Range'))
                except OSError:
                    return self.reply(503,{'error':'Файл приложения временно недоступен. Повторите скачивание.'})
                self.extra_headers=headers
                return self.reply(status,body,'application/vnd.android.package-archive')
            if route.path=='/api/regions':
                manifest=regions.manifest(self.region_key)
                if self.identity and self.identity['role']=='engineer':
                    manifest['regions']=[r for r in manifest['regions'] if r['id']==self.identity['region']]
                return self.reply(200,manifest)
            if route.path=='/api/risk':
                try:
                    from .risk_dashboard import capture_dashboard_session, dashboard
                    # Freeze a coherent, compact view quickly; Monte Carlo must
                    # neither hold the mutation lock nor copy road matrices and
                    # historical workbench views.
                    with lock:
                        source_session=holder['session']
                        risk_session=capture_dashboard_session(source_session)
                        risk_run=demo_fields(self.region_key).get('demo_run_id')
                    gps_status,gps_snapshot=gps.request('GET','/api/gps','channel=device')
                    result=dashboard(risk_session,gps_snapshot if gps_status==200 else None)
                    with lock:
                        if holder['session'] is not source_session or source_session.version!=risk_session.version or demo_fields(self.region_key).get('demo_run_id')!=risk_run:
                            return self.reply(409,{'error':'План изменился. Обновите показатели.'})
                        snapshot_id=uuid4().hex
                        result.update(snapshot_id=snapshot_id,llm_enabled=explainer is not None)
                        snapshots=holder.setdefault('risk_snapshots',{})
                        snapshots[snapshot_id]=(result,id(source_session),risk_run)
                        if len(snapshots)>8:snapshots.pop(next(iter(snapshots)))
                    return self.reply(200,result)
                except Exception:
                    return self.reply(503,{'error':'Показатели риска временно недоступны. Планирование продолжает работать.'})
            if route.path == '/api/gps' or route.path.startswith('/api/gps/'):
                status, data = gps.request('GET',route.path,route.query)
                if status == 200 and route.path == '/api/gps':
                    native_gps.describe_connections(self.region_key, data)
                return self.reply(status, data)
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
                            if route.path=='/api/revision':result.update(demo_fields(self.region_key))
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
                    query=parse_qs(route.query,keep_blank_values=True)
                    run_ids=query.pop('demo_run_id',None)
                    if run_ids is not None and len(run_ids)!=1:raise ValueError('Укажите один запуск демо')
                    version=_version(urlencode(query,doseq=True))
                    with lock:
                        if run_ids is not None:check_demo_run(self.region_key,{'demo_run_id':run_ids[0]})
                        session=holder['session']
                        payload=session.workbench(version)
                        selected_version=version if version is not None else session.version
                    if route.path=='/dispatcher':
                        document=render_dispatcher(payload).replace('href="report.html"',f'href="/report?version={selected_version}"',1)
                    else:
                        document=render_report(Scenario.model_validate(payload['scenario']),[Plan.model_validate(p) for p in payload['plans']])
                    return self.reply(200,document.encode('utf-8'),'text/html; charset=utf-8',rendered=True)
                except (ValueError,KeyError,TypeError) as exc:
                    return self.reply(409 if 'перезапущен' in str(exc) else 400,{'error':str(exc)})
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
                with lock:self.reply(200,{**holder['session'].state(),**demo_fields(self.region_key)})
                return
            if route.path=='/api/demo-stream/requests' and demo_fields(self.region_key)['demo_stream_enabled']:
                with lock:return self.reply(200,{'requests':demo_stream_requests,**demo_fields(self.region_key)})
            if route.path=='/api/inbox':
                if route.query:return self.reply(400,{'error':'Неизвестные параметры запроса'})
                with lock:
                    current=holder['session']
                    last_applied=next((row for row in reversed(current.history) if row.get('candidate_id')),None)
                    self.reply(200,{'version':current.version,**demo_fields(self.region_key),'planning_settings':current.planning_settings,
                        'incoming_requests':current.incoming_requests[-200:],
                        'dispatch_notifications':current.dispatch_notifications[-100:],
                        'decision_log':current.decision_log[-300:],
                        'last_applied':({'version':last_applied['version'],
                            'automatic':last_applied.get('automatic'),
                            'incoming_count':len(last_applied.get('incoming_ids',[])),
                            'changed_count':len(last_applied.get('changed_request_ids',[]))}
                            if last_applied else None)})
                return
            files={'/':('scenarios.html','text/html; charset=utf-8'),'/scenarios.js':('scenarios.js','text/javascript; charset=utf-8'),'/scenarios.css':('scenarios.css','text/css; charset=utf-8')}
            files['/time-input.js']=('time-input.js','text/javascript; charset=utf-8')
            files.update({'/app-context.js':('app-context.js','text/javascript; charset=utf-8'),'/app-context.css':('app-context.css','text/css; charset=utf-8')})
            files.update({'/risk-dashboard.js':('risk-dashboard.js','text/javascript; charset=utf-8'),'/risk-dashboard.css':('risk-dashboard.css','text/css; charset=utf-8')})
            files.update({'/crew':('crew.html','text/html; charset=utf-8'),'/crew.js':('crew.js','text/javascript; charset=utf-8'),'/crew.css':('crew.css','text/css; charset=utf-8')})
            files.update({'/engineer':('engineer.html','text/html; charset=utf-8'),
                          '/engineer.js':('engineer.js','text/javascript; charset=utf-8'),
                          '/engineer.css':('engineer.css','text/css; charset=utf-8')})
            files.update({f'/{name}':(name,kind) for name,kind in (
                ('gps-engineer.js','text/javascript; charset=utf-8'),
                ('gps-android.js','text/javascript; charset=utf-8'),
                ('apk-download.js','text/javascript; charset=utf-8'),
                ('gps-dispatcher.js','text/javascript; charset=utf-8'),
                ('gps.css','text/css; charset=utf-8'))})
            if route.path.startswith('/assets/'):
                name=route.path[len('/assets/'):]
                if name not in _ASSETS:return self.reply(404,{'error':'Не найдено'})
                return self.reply(200,(_STATIC/'vendor'/name).read_bytes(),_ASSETS[name])
            if route.path not in files:return self.reply(404,{'error':'Не найдено'})
            name,kind=files[route.path]
            if route.path=='/crew' and parse_qs(route.query).get('embedded')!=['1']:
                name='scenarios.html'
            self.reply(200,(_STATIC/name).read_bytes(),kind)

        def do_POST(self):
            if not self.trusted():return self.reply(403,{'error':'Запрос с другого сайта запрещен'})
            if native_gps.receive(self):return
            raw=urlsplit(self.path).path
            is_intake=raw=='/api/inbox/requests' or re.fullmatch(r'/(?:r|regions)/[a-zA-Z0-9_-]+/api/inbox/requests',raw)
            machine_request=bool(is_intake and intake_token and
                hmac.compare_digest(self.headers.get('X-Intake-Token',''),intake_token))
            if not machine_request and auth.before(self):return
            if machine_request:self.identity=None
            if native_gps.authenticated(self):return
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
                demo_payload=payload
                payload={key:value for key,value in payload.items() if key!='demo_run_id'}
                # Read the immutable generation token without the planner lock:
                # telemetry must remain responsive during a long solve. All
                # plan mutations repeat this check under their commit lock.
                check_demo_run(self.region_key,demo_payload,
                    required=self.path in ('/api/demo-stream/reset','/api/inbox/requests'))
                if self.identity and self.identity['role']=='engineer' and payload.get('engineer_id')!=self.identity['engineer_id']:
                    return self.reply(403,{'error':'Можно отправлять только свои статусы и координаты'})
                # Telemetry has its own lock; the solver may be busy for seconds.
                if is_gps and gps_route.path!='/api/gps/confirm-delay':return self.reply(*gps.request('POST',gps_route.path,gps_route.query,payload))
                if self.path=='/api/risk/summary':
                    with lock:
                        check_demo_run(self.region_key,demo_payload)
                        session=holder['session']
                        stored=holder.get('risk_snapshots',{}).get(payload.get('snapshot_id'))
                        if (stored is None or stored[1]!=id(session) or payload.get('version')!=session.version
                            or stored[0]['context']['plan_version']!=session.version
                            or stored[2]!=demo_fields(self.region_key).get('demo_run_id')):
                            return self.reply(409,{'error':'Показатели устарели. Нажмите «Обновить».'})
                        snapshot=stored[0]
                    if explainer is None:raise ValueError('LLM не настроена')
                    result=explainer.summarize_day(snapshot)
                    with lock:
                        if holder['session'] is not session or session.version!=payload.get('version') or stored[2]!=demo_fields(self.region_key).get('demo_run_id'):
                            return self.reply(409,{'error':'План изменился. Обновите показатели.'})
                    return self.reply(200,{**result,'snapshot_id':payload['snapshot_id']})
                if self.path in ('/api/explain','/api/recommend'):
                    with lock:
                        check_demo_run(self.region_key,demo_payload)
                        session=holder['session']
                        if payload.get('version')!=session.version:raise ValueError('Устаревшая версия плана')
                        stored=session.batches.get(payload.get('batch_id'))
                        if stored is None:raise ValueError('Варианты устарели; рассчитайте заново')
                        batch=deepcopy(stored['batch'])
                    if self.path=='/api/explain' and payload.get('candidate_id') is None:raise ValueError('Нужен вариант для объяснения')
                    if explainer is None:raise ValueError('LLM не настроена')
                    result=explainer.recommend(batch) if self.path=='/api/recommend' else explainer.explain(batch,payload.get('candidate_id'))
                    return self.reply(200,result)
                manual_explanation=None
                with lock:
                    check_demo_run(self.region_key,demo_payload,
                        required=self.path in ('/api/demo-stream/reset','/api/inbox/requests'))
                    if self.path=='/api/demo-stream/reset':
                        if not demo_fields(self.region_key)['demo_reset_enabled']:
                            return self.reply(404,{'error':'Перезапуск доступен только в учебной песочнице'})
                        # The dispatcher deliberately replaces the whole run.
                        # Autopilot may have advanced it since the last poll;
                        # its generation, rather than that poll's version,
                        # identifies the run the reset button is acting on.
                        requested_version=payload.get('version')
                        if type(requested_version) is not int or not 1<=requested_version<=holder['session'].version:
                            raise ValueError('Некорректная версия плана для перезапуска')
                        context=regions.contexts[self.region_key]
                        candidate=deepcopy(demo_baseline)
                        # Archive before committing; a failed archive or atomic
                        # replacement leaves the current run fully active.
                        archive_dir=path.parent/(path.stem+'-archives')
                        archive_dir.mkdir(parents=True,exist_ok=True)
                        archive=archive_dir/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-'+context['demo_run_id']+'.json')
                        if path.exists():archive.write_bytes(path.read_bytes())
                        else:archive.write_text(json.dumps({**holder['session'].dump(),
                            'demo_run_id':context['demo_run_id']},ensure_ascii=False,indent=2),encoding='utf-8')
                        persist(self.region_key,context,candidate,demo_run_id=uuid4().hex)
                        crews.draft=None
                        if address_service is not None:address_service.candidates={}
                        result={**candidate.state(),**demo_fields(self.region_key)}
                    elif self.path=='/api/address/search':
                        if address_service is None:raise ValueError('Поиск адресов выключен; запустите сервер с --network')
                        result=address_service.search(holder['session'],payload)
                    elif self.path=='/api/address/confirm':
                        if address_service is None:raise ValueError('Поиск адресов выключен')
                        candidate=deepcopy(holder['session'])
                        result=address_service.confirm(candidate,payload)
                        persist(self.region_key,regions.contexts[self.region_key],candidate)
                    elif self.path=='/api/crew/preview':
                        result=crews.preview(holder['session'],payload,time_limit=morning_time_limit,solver=solver)
                    elif self.path=='/api/crew/apply':
                        candidate=crews.selected(holder['session'],payload)
                        persist(self.region_key,regions.contexts[self.region_key],candidate)
                        crews.draft=None
                        result=candidate.state()
                    elif self.path=='/api/preview':
                        result={**holder['session'].preview(payload,time_limit=time_limit,solver=solver),'llm_enabled':explainer is not None}
                    elif self.path in ('/api/inbox/requests','/api/inbox/settings'):
                        candidate=deepcopy(holder['session'])
                        result=(candidate.receive_requests(payload) if self.path=='/api/inbox/requests'
                                else candidate.set_planning_settings(payload))
                        persist(self.region_key,regions.contexts[self.region_key],candidate)
                        wake_autopilot.set()
                    elif self.path in ('/api/approve','/api/facts','/api/mode','/api/engineer/facts',
                                      '/api/engineer/unavailability','/api/engineer/decline',
                                      '/api/engineer/revoke-report',
                                      '/api/completions/confirm','/api/gps/confirm-delay'):
                        # Persist the candidate state before publishing it in memory.
                        candidate=deepcopy(holder['session'])
                        if self.path=='/api/approve' and explainer is not None:
                            stored=candidate.batches.get(payload.get('batch_id'))
                            if stored is not None:
                                manual_explanation=(deepcopy(stored['batch']),payload.get('candidate_id'))
                        if self.path=='/api/engineer/facts':result=record_engineer_fact(candidate,payload)
                        elif self.path=='/api/engineer/unavailability':
                            candidate.report_unavailability(payload)
                            result=engineer_state(candidate,payload['engineer_id'])
                        elif self.path=='/api/engineer/decline':
                            candidate.decline_assignment(payload)
                            result=engineer_state(candidate,payload['engineer_id'])
                        elif self.path=='/api/engineer/revoke-report':
                            result=candidate.revoke_engineer_report(payload)
                        elif self.path=='/api/completions/confirm':result=confirm_completion(candidate,payload)
                        elif self.path=='/api/gps/confirm-delay':
                            from .gps.delay import confirm
                            result=confirm(candidate,gps.service,payload)
                        else:result=candidate.approve(payload) if self.path=='/api/approve' else candidate.set_mode(payload) if self.path=='/api/mode' else candidate.record_fact(payload)
                        persist(self.region_key,regions.contexts[self.region_key],candidate)
                    else:return self.reply(404,{'error':'Не найдено'})
                    demo_run_id=regions.contexts[self.region_key].get('demo_run_id')
                    if isinstance(result,dict):
                        # Keep mobile/preview/address response contracts intact.
                        # Only dispatcher state and intake envelopes carry the
                        # simulation controls also exposed by GET /api/state.
                        if self.path in ('/api/demo-stream/reset','/api/inbox/requests','/api/inbox/settings',
                                         '/api/crew/apply','/api/approve','/api/facts','/api/mode',
                                         '/api/engineer/revoke-report','/api/completions/confirm',
                                         '/api/gps/confirm-delay'):
                            result.update(demo_fields(self.region_key))
                        if isinstance(result.get('state'),dict):result['state'].update(demo_fields(self.region_key))
                if manual_explanation:
                    explanation_batch,chosen_id=manual_explanation
                    threading.Thread(target=enrich_decisions,
                        args=(self.region_key,regions.contexts[self.region_key],explanation_batch,
                              chosen_id,candidate.version,False,demo_run_id),daemon=True).start()
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
            stop_autopilot.set();wake_autopilot.set()
            autopilot_thread.join(timeout=30)
            super().server_close()
            regions.close()

    try:
        server=Server((host,port),Handler)
        server.gps=regions.contexts[regions.default_key]['gps']
        autopilot_thread=threading.Thread(target=autopilot_loop,name='field-router-autopilot',daemon=True)
        autopilot_thread.start()
        return server
    except Exception:
        regions.close()
        raise


def point_preview(point):
    return ('<!doctype html><html lang="ru"><meta charset="utf-8"><link rel="stylesheet" href="/assets/leaflet.css">'
        '<style>body{margin:0;font:13px system-ui}p{padding:8px;margin:0}#map{height:240px}</style>'
        '<p>'+escape(point.label)+'</p><div id="map"></div><script src="/assets/leaflet.js"></script><script>'
        f'const p=[{point.lat},{point.lon}];const m=L.map("map").setView(p,17);'
        'm.attributionControl.setPrefix(\'<a href="https://leafletjs.com">Leaflet</a>\');'
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
    parser.add_argument('--public-origin',help='Внешний HTTPS origin за доверенным reverse proxy')
    parser.add_argument('--network',action='store_true',help='Разрешить поиск адресов через Photon и координатные запросы OSRM')
    parser.add_argument('--cache',type=Path,default=Path('output/geo-cache'))
    parser.add_argument('--time-limit',type=float,default=DAY_TIME_LIMIT,help='Бюджет пересчёта после события дня, секунды')
    parser.add_argument('--morning-time-limit',type=float,default=MORNING_TIME_LIMIT,
                        help='Бюджет утреннего плана (бригады, старты, выдача), секунды; ALNS v2 включается от 2,5 с на вызов')
    parser.add_argument('--solver',choices=['ortools','alns','hybrid'],default='hybrid',help='Оптимизатор; hybrid делит бюджет между OR-Tools и ALNS')
    parser.add_argument('--gps-config',type=Path)
    parser.add_argument('--gps-model',help='Replaceable Python module:class with analyze(job)')
    parser.add_argument('--gps-routing',type=Path,help='Явно настроенные OSRM endpoints для передачи координат и дорожного ETA')
    parser.add_argument('--disable-gps',action='store_true')
    parser.add_argument('--accounts',type=Path,help='База учётных записей; по умолчанию рядом с файлом состояния')
    parser.add_argument('--certfile',type=Path,help='TLS certificate with IP/hostname SAN')
    parser.add_argument('--keyfile',type=Path,help='TLS private key (never served over HTTP)')
    args=parser.parse_args(argv)
    try:validate_public_origin(args.public_origin)
    except ValueError as exc:parser.error(str(exc))
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
    if initial_password and not args.public_origin:
        print('Первый вход: admin / '+initial_password+'\nСохраните пароль: он показывается только при создании базы.',flush=True)
    explainer=configured()
    server=create_server(session,args.state,port=args.port,time_limit=args.time_limit,morning_time_limit=args.morning_time_limit,solver=args.solver,explainer=explainer,address_service=AddressService(CachedHttp(args.cache),routed_access=True) if args.network else None,host=args.host,
                         gps_config=args.gps_config,gps_model=args.gps_model,disable_gps=args.disable_gps,tls_context=tls,
                         region_sources=discover_regions(args.comparison,args.state,args.regions_dir),accounts=accounts,
                         public_origin=args.public_origin,gps_routing=args.gps_routing)
    print('Учётные записи: '+str(accounts.path.resolve()),flush=True)
    addresses=local_ipv4_addresses() if args.host=='0.0.0.0' else [args.host]
    for address in addresses:
        print(f'Диспетчер: {scheme}://{address}:{server.server_port}/',flush=True)
        print(f'Инженер:   {scheme}://{address}:{server.server_port}/engineer',flush=True)
    if args.public_origin:print('Публичное демо: '+args.public_origin,flush=True)
    elif args.host!='127.0.0.1' and not tls:print('Для входа и GPS с телефона используйте HTTPS (--certfile/--keyfile или -Https).',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':main()
