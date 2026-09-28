"""Workflow regressions for the simplified dispatcher shell."""
from test_strategy_ui import client


def test_settings_and_diagnostics_do_not_share_operational_forms():
    client('''
      assert.equal($('planning-mode').closest('section').id,'panel-settings');
      assert.equal($('execution-mode').closest('section').id,'panel-settings');
      assert.ok($('risk-engine-meta').closest('#internal-diagnostics'));
      assert.ok($('intake-json').closest('#panel-demo'));
      assert.ok($('region-select').closest('header'));
      await $('tab-settings').dispatch('click');
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
      assert.equal($('manual-event-panel').tagName,'SECTION');
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


def test_header_switch_saves_mode_without_leaving_plan_and_keeps_thresholds():
    client('''
      const base=global.fetch;
      global.fetch=async(url,options)=>{
        if(url.endsWith('/inbox/settings')){
          const payload=JSON.parse(options.body);state.planning_settings={...payload};
        }
        return base(url,options);
      };
      $('quick-planning-mode').value='autopilot';await $('quick-planning-mode').dispatch('change');
      assert.equal($('panel-plan').hidden,false);
      assert.equal($('quick-planning-mode').value,'autopilot');
      assert.deepEqual(requests.filter(r=>r.url.endsWith('/inbox/settings')).at(-1).payload,
        {version:7,mode:'autopilot',every_n_requests:3,every_n_minutes:15});
      $('auto-count').value='4';await $('auto-count').dispatch('input');
      $('quick-planning-mode').value='manual';await $('quick-planning-mode').dispatch('change');
      assert.equal($('auto-count').value,'4');
      assert.equal($('quick-planning-mode').value,'manual');
      assert.equal($('panel-plan').hidden,false);
    ''',before="state.planning_settings={mode:'manual',every_n_requests:3,every_n_minutes:15};")


def test_failed_header_mode_switch_rolls_back_and_preserves_settings_draft():
    client('''
      $('auto-count').value='4';await $('auto-count').dispatch('input');
      const base=global.fetch;
      global.fetch=async(url,options)=>url.endsWith('/inbox/settings')?
        {ok:false,status:503,json:async()=>({error:'Сервис недоступен'})}:base(url,options);
      $('quick-planning-mode').value='manual';await $('quick-planning-mode').dispatch('change');
      assert.equal($('quick-planning-mode').value,'autopilot');
      assert.equal($('auto-count').value,'4');
      assert.equal($('error').hidden,false);
    ''',before="state.planning_settings={mode:'autopilot',every_n_requests:3,every_n_minutes:null};")


def test_crew_opens_inside_workspace_and_dirty_form_blocks_region_change():
    client('''
      await $('tab-crew').dispatch('click');
      assert.equal($('panel-crew').hidden,false);
      assert.equal($('page-title').textContent,'Состав и оборудование');
      assert.ok($('crew-frame').src.includes('crew?embedded=1'));
      const child={};$('crew-frame').contentWindow=child;
      window.dispatchEvent(Object.assign(new Event('message'),{origin:'http://localhost',source:child,data:{type:'crew-editing',dirty:true,busy:false}}));
      const region=new Event('region-change',{cancelable:true});window.dispatchEvent(region);
      assert.equal(region.defaultPrevented,true);
      assert.equal($('crew-discard').hidden,false);
      await $('tab-plan').dispatch('click');await $('tab-crew').dispatch('click');
      assert.equal($('crew-discard').hidden,false);
    ''')


def test_foreign_frame_cannot_mark_crew_dirty():
    client('''
      $('crew-frame').contentWindow={};
      window.dispatchEvent(Object.assign(new Event('message'),{origin:'https://example.com',source:{},data:{type:'crew-editing',dirty:true}}));
      const region=new Event('region-change',{cancelable:true});window.dispatchEvent(region);
      assert.equal(region.defaultPrevented,false);
    ''')
