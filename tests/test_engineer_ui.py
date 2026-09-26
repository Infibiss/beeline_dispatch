import shutil
import subprocess
from pathlib import Path

import pytest


def test_mobile_payload_validation_and_insecure_http_event_id():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is only needed for client tests')
    root = Path(__file__).resolve().parents[1]
    code = r"""
const assert = require('node:assert/strict');
const mobile = require('./src/field_router/static/engineer.js');
const dispatch = require('./src/field_router/static/scenarios.js');
const input = {time:'09:10', location:'office', remaining:'30', router:'0', tv:'0', comment:'Done'};
assert.equal(mobile.minute('09:10'), 550);
assert.throws(() => mobile.minute('24:00'));
assert.throws(() => mobile.actionPayload(1,'e1','working','a',{...input, remaining:'-1'}));
assert.throws(() => mobile.actionPayload(1,'e1','interrupted','a',{...input, router:''}));
assert.throws(() => mobile.actionPayload(1,'e1','unavailable',null,{...input, comment:''}));
assert.deepEqual(mobile.actionPayload(1,'e1','completed','a',input), {
  version:1, engineer_id:'e1', status:'completed', time:550, comment:'Done',
  request_id:'a', consumed_router:0, consumed_tv_box:0
});
const period = {startDate:'2026-09-26', startTime:'10:00', endDate:'2026-09-28', endTime:'18:00', reason:'sick', comment:'Заболел'};
assert.deepEqual(mobile.unavailabilityPayload(2,'e1',period,'2026-09-26',540), {
  version:2, engineer_id:'e1', start_date:'2026-09-26', start_time:'10:00',
  end_date:'2026-09-28', end_time:'18:00', reason:'sick', comment:'Заболел'
});
assert.throws(() => mobile.unavailabilityPayload(2,'e1',{...period,startTime:'08:00'},'2026-09-26',540));
assert.throws(() => mobile.unavailabilityPayload(2,'e1',{...period,startTime:'08:00',location:'office'},'2026-09-26',540));
assert.equal(mobile.unavailabilityPayload(2,'e1',{...period,startTime:'09:00',location:'office'},'2026-09-26',540).location_id,'office');
assert.equal(mobile.unavailabilityPayload(2,'e1',{...period,startTime:'19:00'},'2026-09-26',1140,540,1080).location_id, undefined);
assert.throws(() => mobile.unavailabilityPayload(2,'e1',{...period,endDate:'2026-09-25'},'2026-09-26',540));
assert.throws(() => mobile.unavailabilityPayload(2,'e1',{...period,startDate:'2026-02-30'},'2026-09-26',540));
assert.deepEqual(mobile.declinePayload(2,'e1','a',{time:'09:10',comment:'Нет возможности выехать'}), {
  version:2, engineer_id:'e1', request_id:'a', time:550, comment:'Нет возможности выехать'
});
assert.throws(() => mobile.declinePayload(2,'e1','a',{time:'09:10',comment:'  '}));
const webcrypto = require('node:crypto').webcrypto;
Object.defineProperty(globalThis, 'crypto', {configurable:true, value:{getRandomValues:a=>webcrypto.getRandomValues(a)}});
assert.match(dispatch.uniqueEventId(), /^event-[0-9a-f]{32}$/);
assert.notEqual(dispatch.uniqueEventId(), dispatch.uniqueEventId());
"""
    subprocess.run([node, '-e', code], cwd=root, check=True, capture_output=True, text=True)
