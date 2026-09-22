import json
from pathlib import Path
import subprocess


def test_form_encodes_ordinary_request_without_redirecting_or_unlocking_work():
    path = Path(__file__).parents[1] / 'src/field_router/static/scenarios.js'
    script = """
const {eventPayload} = require(process.argv[1]);
const payload=eventPayload(7,{id:'new-1',type:'new_request',time:'11:30',target:'a',
 workKind:'connection',duration:'70',router:'1',tvBox:'2',windowStart:'12:00',windowEnd:'14:00',
 redirect:true,preserve:false});
console.log(JSON.stringify(payload));
"""
    result = subprocess.run(['node', '-e', script, str(path)], capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    assert payload['version'] == 7
    assert payload['event'] == {'id': 'new-1', 'type': 'new_request', 'time': 690,
        'anchor_id': 'a', 'duration_minutes': 70, 'router': 1, 'tv_box': 2,
        'work_kind': 'connection', 'window_start': 720, 'window_end': 840}
    assert payload['allow_redirect'] is False
    assert payload['preserve_in_progress'] is True
