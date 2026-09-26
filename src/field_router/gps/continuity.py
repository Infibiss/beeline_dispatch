"""Repair legacy device-token resets from recorded evidence, never from task addresses.

The stored receipts stay unchanged. Only the live reference is reconstructed;
later model history begins after the repair so formerly accepted outliers cannot
re-enter the filter after another restart.
"""
from collections import deque
from copy import deepcopy
from .quality import assess


def repair_legacy_handoff(stream, evidence, transport, config):
    anchor,samples=evidence
    candidate={'last_good':deepcopy(anchor),'last_raw':None,'quality':None,
               'quality_detail':{},'quality_notice':None,'conflict_until':0,
               'conflict_detail':{},'recovery':[],'points':deque([deepcopy(anchor)],maxlen=config.max_points),
               'trace':deque(maxlen=config.max_points)}
    first=samples[0]
    # Repair only a demonstrably impossible first fix after a recent handoff.
    reason,_=assess(candidate,first['point'],transport,config,first['received_at'])
    if reason!='jump':return False
    for sample in samples:
        point=sample['point'];now=sample['received_at']
        if (point['measured_at_ms']<=candidate['last_good']['measured_at_ms'] or
            now-point['measured_at_ms']/1000>config.stale_seconds):
            continue
        reason,recovered=assess(candidate,point,transport,config,now)
        if candidate['last_raw'] is None or point['measured_at_ms']>candidate['last_raw']['measured_at_ms']:
            candidate['last_raw']=deepcopy(point)
        candidate['quality']=reason or ('source_conflict' if candidate['conflict_until']>now else None)
        if recovered:candidate['points'].clear()
        if reason is None:
            candidate['last_good']=deepcopy(point)
            candidate['points'].append(deepcopy(point))
        candidate['trace'].append({**sample,'accepted':reason is None,'reason':reason})
    for name in ('last_good','last_raw','quality','quality_detail','quality_notice','conflict_until','conflict_detail','recovery'):
        stream[name]=deepcopy(candidate[name])
    stream['quality_detail']['continuity_repaired']=True
    stream['points_since_ms']=max(s['point']['measured_at_ms'] for s in samples)+1
    return True
