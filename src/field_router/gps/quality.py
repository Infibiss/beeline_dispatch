"""Pure bounded quality policy. Recovery never infers employee movement."""
from .contracts import distance

PROVIDERS = {'gps':'спутники', 'network':'Wi-Fi/сотовая сеть', 'fused':'комбинированный',
             'browser':'браузер', 'unknown':'не указан'}


def assess(stream, point, transport, config, now):
    previous = stream['last_good']
    detail = {'provider':point.get('provider','unknown'), 'accuracy_m':round(point['accuracy_m'],1)}
    reason, recovered = None, False
    if point.get('is_mock'):
        reason = 'mock'
    elif point['accuracy_m'] > config.max_accuracy_m:
        reason = 'low_accuracy'
    elif previous:
        dt = (point['measured_at_ms']-previous['measured_at_ms'])/1000
        metres = distance(previous,point)
        speed = max(0,metres-previous['accuracy_m']-point['accuracy_m']) / dt if dt>0 else float('inf')
        limit = {'car':60,'walk':8,'bicycle':20,'public':65}[transport]
        detail.update(delta_seconds=round(dt,2), distance_m=round(metres),
                      minimum_speed_kmh=round(speed*3.6) if dt>0 else None,
                      limit_kmh=round(limit*3.6), previous_provider=previous.get('provider','unknown'))
        if dt<=0 or speed>limit:
            reason='jump'
            provider=point.get('provider','unknown')
            other=previous.get('provider','unknown')
            if provider!=other and provider!='unknown' and other!='unknown' and dt<=config.stale_seconds:
                stream['conflict_until']=now+config.source_conflict_seconds
                stream['conflict_detail']=dict(detail)
            # Do not remain anchored indefinitely to an erroneous first fix.
            # A new stationary cluster must outlive the old reliable fix AND
            # comprise independently timed measurements from the same provider.
            candidates=stream.get('recovery',[])
            candidates=[p for p in candidates if 0 < point['measured_at_ms']-p['measured_at_ms'] <= 120000]
            if any(p.get('provider')!=point.get('provider') or
                   distance(p,point)>max(75,2*max(p['accuracy_m'],point['accuracy_m'])) for p in candidates):
                candidates=[]
            candidates=(candidates+[point])[-6:]
            stream['recovery']=candidates
            span=(candidates[-1]['measured_at_ms']-candidates[0]['measured_at_ms'])/1000
            if (len(candidates)>=config.reacquire_points and span>=config.reacquire_span_seconds
                and dt>=max(config.reacquire_after_seconds,config.stale_seconds) and stream.get('conflict_until',0)<=now):
                reason=None;recovered=True
                stream['quality_notice']={'time':now,'code':'reacquired','samples':len(candidates),'span_seconds':round(span)}
    if reason!='jump':
        stream['recovery']=[]
    if reason:
        stream['quality_detail']=detail
    elif stream.get('conflict_until',0)>now:
        stream['quality_detail']=stream.get('conflict_detail',detail)
    else:
        stream['quality_detail']=detail
    return reason,recovered


def describe(reason, detail):
    """Operational copy only. Numeric evidence stays in quality_detail/history."""
    if reason=='low_accuracy':
        return 'Телефон пока определяет положение слишком приблизительно.'
    if reason=='mock':
        return 'На телефоне включено тестовое местоположение. Для рабочего режима отключите подмену координат.'
    if reason in ('source_conflict','jump'):
        return 'Телефон прислал противоречивые координаты. На карте сохранено последнее принятое положение.'
    return 'Ждём подходящее измерение телефона.'
