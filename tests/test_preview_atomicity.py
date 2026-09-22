from copy import deepcopy

import pytest

from test_real_replanning import real_session


def test_rejected_ordinary_preview_does_not_publish_geography(real_session):
    session = real_session
    session.record_fact({'version': session.version, 'engineer_id': 'e1', 'status': 'working',
                         'time': 550, 'request_id': 'a', 'remaining_minutes': 150})
    before = deepcopy(session.dump())
    with pytest.raises(ValueError, match='Сначала выполните обычный пересчёт'):
        session.preview({'version': session.version, 'event': {'id': 'rejected',
            'type': 'new_request', 'work_kind': 'local', 'time': 551, 'anchor_id': 'b',
            'duration_minutes': 1, 'window_start': 551, 'window_end': 700}}, time_limit=.03)
    assert session.dump() == before


def test_draft_request_is_not_a_selectable_saved_address(real_session):
    batch = real_session.preview({'version': 1, 'event': {'id': 'draft', 'type': 'emergency',
        'time': 540, 'anchor_id': 'b', 'duration_minutes': 20}}, time_limit=.03)
    assert 'emergency:draft' not in {p['id'] for p in real_session.state()['locations']}
    real_session.approve({'version': 1, 'batch_id': batch['id'], 'candidate_id': 'stable'})
    assert 'emergency:draft' in {p['id'] for p in real_session.state()['locations']}
