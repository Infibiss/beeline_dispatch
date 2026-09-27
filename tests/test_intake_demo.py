"""The hands-on sandbox starts from a real immutable road comparison."""
from pathlib import Path

from scripts.intake_demo import build_demo, build_stream_baseline, demo_requests


def test_demo_has_six_requests_two_absences_and_a_valid_preview():
    source = Path(__file__).resolve().parents[1] / 'data/demo-road/Восток.json'
    before = source.read_bytes()
    session = build_demo(source)
    assert source.read_bytes() == before
    assert session.planning_settings == {'mode': 'manual', 'every_n_requests': 5,
                                         'every_n_minutes': None}
    assert len(session.incoming_requests) == 6
    assert len(session.unavailability) == 2
    assert len(session.replanning_pool()) >= 2
    assert session.needs_replanning
    batch = session.preview({'version': session.version,
        'event': {'id':'demo-test-preview','type':'replan','time':session.time}},
        time_limit=.05, solver='ortools')
    assert len(batch['incoming_ids']) == 6
    assert batch['recommended_candidate_id'] in {'stable','sla','cost'}


def test_stream_demo_starts_empty_then_receives_six_separate_events():
    source=Path(__file__).resolve().parents[1]/'data/demo-road/Восток.json'
    session=build_demo(source,seed_requests=False)
    assert not session.incoming_requests
    assert len(session.unavailability)==2
    rows=demo_requests(session,prefix='demo-stream')
    assert len(rows)==6
    assert [row['time'] for row in rows]==['09:10','09:11','09:12','09:13','09:14','09:15']
    for index,row in enumerate(rows,1):
        assert session.receive_requests({'request':row})['added']==1
        assert len(session.incoming_requests)==index
        assert len([item for item in session.dispatch_notifications if item['kind']=='incoming'])==index
    assert session.time==555
    assert len(session.decision_log)==0  # Manual mode has not applied a plan.


def test_stream_baseline_is_repeatable_without_setup_history():
    source = Path(__file__).resolve().parents[1] / 'data/demo-road/Восток.json'
    before = source.read_bytes()
    first = build_stream_baseline(source)
    second = build_stream_baseline(source)
    assert source.read_bytes() == before
    assert first.dump() == second.dump()
    assert first.version == 1 and first.time == 540
    assert not first.history and not first.dispatch_notifications
    assert not first.incoming_requests and not first.decision_log
    assert len(first.views) == 1
    assert len(first.unavailability) == len(first.pending_report_ids) == 2
    assert len(first.replanning_pool()) >= 2 and first.needs_replanning
    original_ids = {item.id for item in first.scenario.requests}
    assert len(original_ids) >= 60
    rows = demo_requests(first, prefix='demo-stream')
    for row in rows:
        first.receive_requests({'request': row})
    assert second.version == 1 and not second.incoming_requests
    assert rows == demo_requests(second, prefix='demo-stream')
