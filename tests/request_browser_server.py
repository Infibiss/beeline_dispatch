"""Small isolated dispatcher fixture: never touches the user's demo state."""
import argparse
import json
from pathlib import Path
import threading
import time

from field_router.models import Engineer, Equipment, Point, Request, Scenario, Skill, TimeWindow, Transport
from field_router.planning import make_plan
from field_router.replanning import ScenarioSession
from field_router.scenario_server import create_server
from field_router.travel import synthetic_travel
from field_router.assignment_narrative import PROMPT
from field_router.llm import Explainer


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('--stop',type=Path,required=True)
    parser.add_argument('--model-fixture',action='store_true')
    args=parser.parse_args()
    scenario=Scenario(id='request-browser',region='test',date='2026-08-17',office_address='Office',
        requests=[Request(id=key,region='test',date='2026-08-17',address='Адрес '+key,
            windows=[TimeWindow(start=start,end=start+20)],duration_minutes=30,skill=Skill.LOCAL)
            for key,start in [('a',600),('b',720),('unassigned',610)]],
        engineers=[Engineer(id='car',name='Инженер автомобиль',region='test',
            shift=TimeWindow(start=540,end=1080),skills=[Skill.LOCAL,Skill.EMERGENCY],
            transport=Transport.CAR,stock=Equipment(router=3))],
        points={'office':Point(x_km=0,y_km=0),**{key:Point(x_km=1,y_km=0) for key in ('a','b','unassigned')}},
        metadata={'geography':'synthetic_cartesian_km'},travel_config={
            'speed_kmh':{mode.value:60 for mode in Transport},
            'distance_factor':{mode.value:1 for mode in Transport},
            'boarding_minutes':{mode.value:0 for mode in Transport}})
    plan=make_plan(scenario,synthetic_travel(scenario),{'car':['a','b']},'test','valid')
    route=plan.routes[0]
    route=route.model_copy(update={'visits':[route.visits[0].model_copy(update={
        'explanation':'Сохранённое объяснение <img src=x onerror=alert(1)> — только текст.'}),*route.visits[1:]]})
    session=ScenarioSession(scenario,plan.model_copy(update={'routes':[route]}))
    def fixture_reply(body):
        # No external calls in browser tests; exercise the actual parser/guards.
        if not body['input'].startswith(PROMPT):raise TimeoutError('Plan comparison is not under test')
        context=json.loads(body['input'][len(PROMPT):])
        if context['state']=='assigned':
            texts=['{{engineer}} подходит для этого выезда: начало {{start}} попадает в окно {{window}}.',
                   'После {{previous}} предусмотрена дорога {{travel}} мин. Такой порядок укладывается в доступное расписание.']
        else:texts=['Заявка пока без исполнителя: нужно проверить доступное время и ресурсы инженеров.',
                    'Текущая проверка не исключает, что другое распределение поможет назначить этот визит.']
        time.sleep(.15)
        return {'output':[{'content':[{'type':'output_text','text':json.dumps({'paragraphs':[
            {'text':text,'evidence':context['required_evidence']} for text in texts]},ensure_ascii=False)}]}]}
    server=create_server(session,args.state,port=0,time_limit=.1,solver='ortools',disable_gps=True,accounts=None,
                         explainer=Explainer(send=fixture_reply,assignment_prose_enabled=True) if args.model_fixture else None)
    def stop_when_requested():
        deadline=time.monotonic()+120
        while time.monotonic()<deadline and not args.stop.exists():time.sleep(.2)
        server.shutdown()
    threading.Thread(target=stop_when_requested,daemon=True).start()
    print(f'http://127.0.0.1:{server.server_port}',flush=True)
    try:server.serve_forever()
    finally:server.server_close()
