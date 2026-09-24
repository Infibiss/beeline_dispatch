"""Saved views retain displayed trips, not another all-pairs transport database."""
from copy import deepcopy
import json

from test_real_replanning import real_session
from field_router.replanning import ScenarioSession


def test_views_keep_only_used_journeys_and_full_solver_data_survives(real_session):
    session = real_session
    used = {'minutes': 10, 'metres': 4000, 'legs': [{'mode': 'walk'}]}
    unused = {'minutes': 20, 'metres': 8000, 'legs': ['large unused route'] * 1000}
    session.geography.matrices['public_journeys'] = {
        'office': {'a': used, 'b': unused}, 'a': {'b': used}, 'b': {'a': unused}}
    session._save_view('compact test')
    view = session.workbench()
    assert view['geography']['matrices'] == {'public_journeys': {
        'office': {'a': used}, 'a': {'b': used}}}
    assert 'metadata' not in view['geography']
    original = deepcopy(view)
    # Historical snapshots must not borrow mutable lists from the routing cache.
    used['legs'].append({'mode': 'wait'})
    assert session.workbench() == original
    restored = ScenarioSession.restore(json.loads(json.dumps(session.dump())))
    assert restored.workbench() == original
    assert restored.geography.matrices['public_journeys']['office']['b'] == unused
    assert restored._travel(restored.scenario).minutes('a', 'office', 'car') == 15


def test_compact_view_resolves_aliased_route_origins(real_session):
    session = real_session
    trip = {'minutes': 10, 'metres': 4000, 'legs': []}
    session.geography.matrices['public_journeys'] = {'office': {'a': trip}}
    session._alias_location('position:e1', 'office')
    engineers = [e.model_copy(update={'start_location': 'position:e1'}) for e in session.scenario.engineers]
    session.scenario = session.scenario.model_copy(update={'engineers': engineers})
    session._save_view('alias test')
    assert session.workbench()['geography']['matrices'] == {'public_journeys': {'office': {'a': trip}}}
