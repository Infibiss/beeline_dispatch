"""Workflow regressions for the simplified dispatcher shell."""
from test_strategy_ui import client


def test_settings_and_diagnostics_do_not_share_operational_forms():
    client('''
      assert.equal($('planning-mode').closest('section').id,'panel-settings');
      assert.equal($('execution-mode').closest('section').id,'panel-settings');
      assert.ok($('risk-engine-meta').closest('#panel-technical'));
      assert.ok($('intake-json').closest('#panel-demo'));
      assert.ok($('region-select').closest('header'));
      await $('open-settings').dispatch('click');
      assert.equal($('panel-settings').hidden,false);
      assert.equal($('panel-plan').hidden,true);
      await $('tab-plan').dispatch('click');
      assert.equal($('panel-settings').hidden,true);
    ''')


def test_mobile_menu_closes_on_navigation_and_escape():
    client('''
      await $('menu-toggle').dispatch('click');
      assert.equal($('sidebar').classList.contains('is-open'),true);
      await $('tab-facts').dispatch('click');
      assert.equal($('sidebar').classList.contains('is-open'),false);
      assert.equal($('page-title').textContent,'Бригады');
      await $('menu-toggle').dispatch('click');
      window.dispatchEvent(Object.assign(new Event('keydown'),{key:'Escape'}));
      assert.equal($('sidebar').classList.contains('is-open'),false);
    ''')


def test_primary_actions_lead_to_existing_forms_without_changing_plan():
    client('''
      await $('new-request-action').dispatch('click');
      assert.equal($('panel-events').hidden,false);
      assert.equal($('event-type').value,'new_request');
      assert.equal($('manual-event-panel').open,true);
      assert.equal(requests.filter(r=>r.payload).length,0);
      await $('record-fact-action').dispatch('click');
      assert.equal($('panel-facts').hidden,false);
    ''')


def test_manual_status_next_step_differs_from_autopilot():
    client('''
      assert.equal($('go-replan').textContent,'Рассчитать изменения');
      assert.equal($('go-replan').hidden,false);
      assert.match($('fact-next-step').textContent,/План требует пересчёта/);
    ''',before="state.needs_replanning=true;state.planning_settings={mode:'manual'};")


def test_autopilot_status_does_not_claim_facts_were_automatically_replanned():
    client('''
      assert.equal($('go-replan').hidden,false);
      assert.match($('fact-next-step').textContent,/Автопилот обрабатывает новые заявки/);
    ''',before="state.needs_replanning=true;state.planning_settings={mode:'autopilot'};")


def test_mobile_arrow_navigation_keeps_drawer_open_until_selection():
    client('''
      await $('menu-toggle').dispatch('click');
      for(const handler of $('tab-plan').events.keydown)handler({key:'ArrowDown',preventDefault(){}});
      assert.equal($('sidebar').classList.contains('is-open'),true);
      assert.equal($('panel-events').hidden,false);
      await $('tab-events').dispatch('click');
      assert.equal($('sidebar').classList.contains('is-open'),false);
    ''')


def test_risk_visit_action_resets_correction_and_selects_requested_visit():
    client('''
      $('fact-edit').value='old';$('fact-late').checked=true;$('fact-request').value='a';
      window.dispatchEvent(new CustomEvent('risk-intent',{detail:{action:'facts',engineerId:'e1',requestId:'b',planVersion:7}}));
      assert.equal($('fact-edit').value,'');assert.equal($('fact-late').checked,false);
      assert.equal($('fact-request').value,'b');assert.equal($('fact-request').hidden,false);
      assert.equal(requests.filter(r=>r.payload).length,0);
      window.dispatchEvent(new CustomEvent('risk-intent',{detail:{action:'facts',engineerId:'e1',requestId:'a',planVersion:6}}));
      assert.equal($('fact-request').value,'b');assert.equal($('error').hidden,false);
    ''',before="state.engineers=[{id:'e1',name:'Engineer'}];state.fact_requests=[{id:'a',engineer_id:'e1',address:'A'},{id:'b',engineer_id:'e1',address:'B'}];")


def test_missing_shell_on_scoped_page_never_loads_default_region():
    client('''
      assert.equal(requests.some(r=>r.url==='/api/state'),false);
      assert.equal($('error').hidden,false);
      assert.match($('error').textContent,/навигац/);
    ''',before="location.pathname='/regions/south/';")
