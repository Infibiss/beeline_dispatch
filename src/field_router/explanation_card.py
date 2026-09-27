"""Short dispatcher-facing presentation of verified plans, not a planner."""
from .recommendation import LABELS


def build_card(batch, choice, emphasis=()):
    from .llm import risk_metrics
    facts=choice['facts']
    alternatives=[c for c in batch['candidates'] if c['id']!=choice['id'] and not c.get('duplicate_of')]
    other=next((c for c in alternatives if c['id']=='stable'),next(iter(alternatives),None))
    reasons=[];tradeoffs=[];attention=[];reason_categories=[]
    assigned=facts.get('assigned',0);unassigned=facts.get('unassigned',0)
    summary=f'На остаток дня назначено {assigned} из {assigned+unassigned} заявок.'
    if other:
        label='«'+LABELS.get(other['id'],'Другой вариант')+'»'
        summary+=f' Сравнение с {label}.'
        def number(value):return f'{value:.2f}'.rstrip('0').rstrip('.')
        def compare(value, reference, description, lower=True, category="changes"):
            if value is None or reference is None or value==reference:return
            target=reasons if ((value<reference)==lower) else tradeoffs
            target.append(f'{description}: {number(value)} против {number(reference)}.')
            if target is reasons:reason_categories.append(category)
        if batch.get('recommendation_policy')=='preserve_approved':
            compare(facts.get('previous_approved_lost',facts.get('lost_assignments',0)),
                    other['facts'].get('previous_approved_lost',other['facts'].get('lost_assignments',0)),
                    'Потеря обещанных визитов')
        for field,description,lower in (
            ('emergency_assigned','Назначено аварий',False),
            ('connection_assigned','Назначено подключений',False),
            ('unassigned','Без исполнителя',True)):
            compare(facts.get(field),other['facts'].get(field),description,lower,category="coverage")
        risk,other_risk=risk_metrics(choice),risk_metrics(other)
        if risk and other_risk:
            compare(risk['p_sla_breach']*100,other_risk['p_sla_breach']*100,'Риск хотя бы одного нарушения окна или срока реакции, %',category='risk')
            compare(risk['expected_loss'],other_risk['expected_loss'],'Потери по модели, усл. ед.',category='risk')
            compare(risk['affected_clients'],other_risk['affected_clients'],'Изменено обещаний',category='risk')
            compare(risk['p_overtime']*100,other_risk['p_overtime']*100,'Риск переработки, %',category='risk')
        compare(facts.get('outside_scope_changes'),other['facts'].get('outside_scope_changes'),'Изменений вне выбранных бригад')
        emergency=choice.get('emergency') or {};other_emergency=other.get('emergency') or {}
        if emergency.get('assigned') and other_emergency.get('assigned'):
            compare(emergency.get('response_minutes'),other_emergency.get('response_minutes'),'До начала новой аварии, мин',category='reaction')
        compare(facts.get('avoidable_reassigned',facts.get('reassigned')),
                other['facts'].get('avoidable_reassigned',other['facts'].get('reassigned')),
                'Предотвратимых смен исполнителя')
        compare(facts.get('time_shift_minutes'),other['facts'].get('time_shift_minutes'),'Суммарный сдвиг начала работ, мин')
        a,b=facts.get('travel_minutes'),other['facts'].get('travel_minutes')
        if a is not None and b is not None and a!=b:
            (reasons if a<b else tradeoffs).append(f'Дороги на {abs(a-b)} мин '+('меньше' if a<b else 'больше')+'.')
            if a<b:reason_categories.append('travel')
        if facts.get('distance_m') is not None and other['facts'].get('distance_m') is not None:
            compare(facts['distance_m']/1000,other['facts']['distance_m']/1000,'Расстояние, км',category='travel')
        compare(facts.get('engineers_used'),other['facts'].get('engineers_used'),'Задействовано бригад',category='travel')
        if batch.get('recommendation_policy')=='preserve_approved' and reasons:
            order={'Потеря обещанных':0,'Без исполнителя':1,'Предотвратимых':2,'Суммарный сдвиг':3}
            indices=sorted(range(len(reasons)),key=lambda i:next((rank for prefix,rank in order.items() if reasons[i].startswith(prefix)),4))
            reasons=[reasons[i] for i in indices];reason_categories=[reason_categories[i] for i in indices]
        if not reasons and not tradeoffs:
            reasons.append(f'По основным показателям результат такой же, как у {label}; явного преимущества нет.')
        elif not reasons:
            reasons.append('По показанным показателям преимущества перед альтернативой нет. Проверьте ограничения и изменения.')
    else:
        reasons.append('Получен один уникальный план; отдельной альтернативы для сравнения нет.')
    lost=facts.get('lost_assignments',0)
    if unassigned or lost:
        attention.append(f'Без исполнителя: {unassigned}. Потеря прежнего назначения: {lost}. Проверьте эти заявки.')
    emergency=choice.get('emergency') or {}
    if emergency and not emergency.get('assigned'):attention.append('Новая авария остаётся без исполнителя.')
    elif emergency.get('exceeds_response_target'):
        attention.append(f"До начала новой аварии — {emergency['response_minutes']} мин: ориентир реакции превышен.")
    changed=(choice.get('risk') or {}).get('affected_clients')
    shifted=facts.get('shifted',0)
    if changed:
        attention.append(f'Изменения ранее назначенных визитов: {changed}. Проверьте, кому нужно сообщить изменения через поддержку.')
    elif shifted:
        attention.append(f'Сдвиги начала у {shifted} заявок. Проверьте необходимость уведомления через поддержку.')
    # Keep the primary factual reason; the model may select a secondary emphasis.
    if len(reasons)>2 and emphasis:
        positions={key:index for index,key in enumerate(emphasis)}
        secondary=sorted(range(1,len(reasons)),key=lambda i:positions.get(reason_categories[i],len(positions)))
        reasons=[reasons[0],reasons[secondary[0]]]
    return {'candidate_id':choice['id'],'label':LABELS.get(choice['id'],'Вариант'),
        'summary':summary,'reasons':reasons[:2],'tradeoffs':tradeoffs,
        'attention':attention,'note':'Риски — модельная оценка; вероятности пока не откалиброваны.' if risk_metrics(choice) else ''}
