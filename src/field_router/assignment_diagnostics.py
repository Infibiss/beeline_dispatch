"""Read-only insertion checks for the final plan, never in the search loop.

Failure here concerns the current route orders and issued stock only. It is
not a proof that a different allocation/order cannot serve the request.
"""
from collections import Counter

from .ordinary_insertion import _fixed_route
from .planning import build_route, compatible


def _time_obstacle(engineer, sequence, travel):
    """First obstacle in an earliest-start schedule for this fixed order."""
    now, location = engineer.shift.start, engineer.start_location
    for request in sequence:
        if not compatible(request, engineer):
            return {'type':'route_constraint','request_id':request.id}
        arrival = now + travel.minutes(location, request.id, engineer.transport)
        starts = [max(arrival, w.start) for w in request.windows if max(arrival, w.start) <= w.end]
        if not starts:
            return {'type':'window','request_id':request.id,'arrival':arrival,
                    'latest_start':max(w.end for w in request.windows)}
        now = min(starts) + request.duration_minutes
        if now > engineer.shift.end:
            return {'type':'shift','request_id':request.id,'end':now,'shift_end':engineer.shift.end}
        location = request.id
    return {'type':'route_constraint'}


def annotate_unassigned(scenario, travel, plan):
    if not plan.unassigned:
        if 'unassigned_diagnostics' not in plan.diagnostics:return plan
        return plan.model_copy(update={'diagnostics':{key:value for key,value in plan.diagnostics.items()
                                                      if key!='unassigned_diagnostics'}})
    jobs = {r.id: r for r in scenario.requests}
    routes = {r.engineer_id: r for r in plan.routes}
    diagnostics, rows = {}, []
    for unresolved in plan.unassigned:
        request = jobs[unresolved.request_id]
        counts, checks = Counter(), []
        positions = fixed_slots = retimed_slots = 0
        for engineer in scenario.engineers:
            detail = {'engineer_id': engineer.id, 'engineer_name': engineer.name}
            reason = None
            if engineer.region != request.region:
                reason = 'region'
            elif request.skill not in engineer.skills:
                reason = 'skill'
            elif request.required_transport is not None and request.required_transport != engineer.transport:
                reason = 'transport'
            elif engineer.off_duty:
                reason = 'off_duty'
            elif engineer.id in request.excluded_engineer_ids:
                reason = 'declined'
            elif engineer.shift.end-engineer.shift.start < request.duration_minutes:
                reason = 'shift_capacity'
            route = routes.get(engineer.id)
            ids = [v.request_id for v in route.visits] if route else []
            used_router = sum(jobs[key].equipment.router for key in ids)
            used_box = sum(jobs[key].equipment.tv_box for key in ids)
            if reason is None and engineer.stock is not None:
                remaining = {'router': engineer.stock.router-used_router, 'tv_box': engineer.stock.tv_box-used_box}
                detail['remaining_stock'] = remaining
                if remaining['router'] < request.equipment.router or remaining['tv_box'] < request.equipment.tv_box:
                    reason = 'stock'
            if reason is not None:
                counts[reason] += 1
                checks.append({**detail, 'status': reason})
                continue
            counts['eligible'] += 1
            starts = {v.request_id: v.start for v in route.visits} if route else {}
            obstacles, fixed, retimed, examples = Counter(), [], [], []
            for gap in range(len(ids)+1):
                inserted = ids[:gap]+[request.id]+ids[gap:]
                positions += 1
                if _fixed_route(engineer, inserted, jobs, starts, travel) is not None:
                    fixed.append(gap+1)
                if build_route(engineer, [jobs[key] for key in inserted], travel) is not None:
                    retimed.append(gap+1)
                else:
                    obstacle=_time_obstacle(engineer, [jobs[key] for key in inserted], travel)
                    obstacles[obstacle['type']] += 1
                    if len(examples)<2:examples.append({'position':gap+1,**obstacle})
            fixed_slots += len(fixed)
            retimed_slots += len(retimed)
            counts.update(obstacles)
            status = 'fixed_slot' if fixed else 'retimed_slot' if retimed else 'no_slot'
            counts[status] += 1
            checks.append({**detail, 'status': status, 'checked_positions': len(ids)+1,
                          'fixed_positions': fixed, 'retimed_positions': retimed,
                          'obstacles': dict(obstacles), 'obstacle_examples':examples,
                          'stock_known': engineer.stock is not None})
        diagnosis = {'scope': 'current_route_orders_and_stock', 'global_impossibility_proven': False,
                     'counts': dict(counts), 'checked_positions': positions,
                     'fixed_slots': fixed_slots, 'retimed_slots': retimed_slots, 'engineers': checks}
        if fixed_slots:
            summary = (f'Найдены допустимые места вставки: {fixed_slots} у {counts["fixed_slot"]} инженеров, '
                       'без изменения начала остальных визитов. Поиск не выбрал их; проверьте варианты пересчёта.')
        elif retimed_slots:
            summary = (f'Вставка возможна в {retimed_slots} местах у {counts["retimed_slot"]} инженеров, '
                       'только с изменением времени прежних визитов. Без переноса обещанного времени места нет.')
        elif positions:
            causes = []
            if counts['window']:
                causes.append(f'окно клиента не соблюдается в {counts["window"]} позициях')
            if counts['shift']:
                causes.append(f'работа выходит за смену в {counts["shift"]} позициях')
            if counts['route_constraint']:
                causes.append(f'нарушены ограничения прежних визитов в {counts["route_constraint"]} позициях')
            summary = (f'Подходят инженеров: {counts["eligible"]}. Проверено мест в их маршрутах: {positions}; '
                       + '; '.join(causes) + '. Для текущего порядка вставка не найдена.')
        else:
            summary = 'С текущими ресурсами нет подходящего инженера.'
        rejected = [(key, label) for key, label in (
            ('skill', 'нет навыка'), ('transport', 'не тот транспорт'), ('off_duty', 'не работают'),
            ('declined', 'отказ от заявки'), ('shift_capacity', 'не хватает времени в доступной смене'),
            ('stock', 'не хватает свободного оборудования'), ('region', 'другой район'))
                    if counts[key]]
        if rejected:
            summary += ' Исключены: '+', '.join(f'{label} — {counts[key]}' for key, label in rejected)+'.'
        summary += ' Перестановки и обмены между маршрутами не проверены; глобальная невозможность не доказана.'
        diagnosis['summary'] = summary
        diagnostics[request.id] = diagnosis
        # Retain proven structural explanations and established machine codes.
        rows.append(unresolved.model_copy(update={'reason': summary}) if unresolved.code in (
            'SEARCH_UNASSIGNED', 'NO_APPEND_SLOT', 'NO_FREE_GAP') else unresolved)
    return plan.model_copy(update={'unassigned': rows,
        'diagnostics': {**plan.diagnostics, 'unassigned_diagnostics': diagnostics}})
