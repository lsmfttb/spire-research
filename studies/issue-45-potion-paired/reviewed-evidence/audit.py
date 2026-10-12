import pathlib,json,time,hashlib,collections,math
P=pathlib.Path;cpu0=time.process_time(); original=P('/home/lsmft/.cache/issue45-potion-paired');base=original/'reviewed-native-18bcd565';ledger=original/'current-execution.json';s=json.loads(ledger.read_text());assert not s['active'];assert not s.get('stop'),s.get('stop')
assert set(s['completed'])=={f'{seed}-{arm}' for seed in range(437001,437097) for arm in ('off','on')}
counts=collections.Counter();rows={};keys=[]
for seed in range(437001,437097):
 for arm in ('off','on'):
  name=f'{seed}-{arm}';r=json.loads((base/(name+'.json')).read_text());assert s['completed'][name]['exit']==0
  assert r['seed']==seed and r['arm']==arm and r['producer']==s['producer'] and r['source']==s['source']
  assert r['valid'] and r['terminal'] and not r['problems'] and r['outcome'] in ('PLAYER_WIN','PLAYER_LOSS')
  assert r['initial_raw']['ascension']==20 and r['initial_raw']['act']==1 and r['initial_raw']['floor_num']==0
  if r['outcome']=='PLAYER_LOSS':assert r['final_raw']['cur_hp']==0
  previous=r['initial_raw'];n=0;battle=0;consumed=0;discarded=0
  for line in (base/(name+'.jsonl')).open():
   step=json.loads(line);assert step['step_index']==n;assert step['snapshot_raw']==previous
   chosen=step['chosen_action_index'];assert chosen in step['eligible_action_indices'];assert step['chosen_action_identity']==step['legal_action_identities'][chosen]
   if step['battle_active']:
    battle+=1;meta=step['decision_metadata'];reports=meta['oracle_search_decision_reports'];assert len(reports)==1;q=reports[0]
    assert q['simulations_requested']==400 and q['include_potions']==(arm=='on') and q['model_calls']==0
    assert q['unmapped_search_edge_count']==0 and not q['problems'] and q['selection_rule']=='highest_mean'
    assert q['selected_legal_action_index']==chosen
    candidates=[x for x in q['root_actions'] if x['eligible'] and x['visits']>0 and x['mean_value'] is not None]
    assert candidates and math.isclose(q['selected_mean_value'],max(x['mean_value'] for x in candidates),rel_tol=1e-12,abs_tol=1e-8)
    assert q['selected_action_identity']==step['chosen_action_identity']
    consumed+=step['chosen_action_kind']=='potion';discarded+=step['chosen_action_kind']=='potion_discard'
    if arm=='off':assert step['chosen_action_kind'] not in ('potion','potion_discard')
   previous=step['next_snapshot_raw'];n+=1
  assert previous==r['final_raw'];assert step['terminal_after_step'];assert n==r['metrics']['decisions'];assert battle==r['search_calls']
  counts['transitions']+=n;counts['battle_decisions']+=battle;counts[arm+'_battle_consumed']+=consumed;counts[arm+'_battle_discarded']+=discarded
  rows[seed,arm]=r;keys.append({'seed':seed,'arm':arm,'status':'VALID_NATIVE_TERMINAL','floor':r['floor'],'outcome':r['outcome'],'cpu':s['completed'][name]['cpu']})
for seed in range(437001,437097):assert rows[seed,'off']['initial_raw']==rows[seed,'on']['initial_raw']
report=json.loads((base/'report.json').read_text());ds=[rows[seed,'on']['floor']-rows[seed,'off']['floor'] for seed in range(437001,437097)]
assert report['paired_differences']==ds and report['valid']==report['completed']==192 and report['mean']==sum(ds)/96
assert sum(report['signs'].values())==96
cost=time.process_time()-cpu0;s['cpu']+=cost+15;s['final_audit_cpu']=cost;s['cleanup_handoff_cpu_charge']=15;s['wall']=time.time()-s['started_utc']
assert s['cpu']<12000
ledger.write_text(json.dumps(s,sort_keys=True,indent=2)+'\n');report['cpu']=s['cpu'];report['wall']=s['wall'];(base/'report.json').write_text(json.dumps(report,sort_keys=True,indent=2)+'\n')
audit={'status':'PASS','keys':192,'native_version_pooled':False,'producer':s['producer'],'source':s['source'],'counts':dict(counts),'cpu_measured':cost,'same_task_cpu':s['cpu'],'classification':report['classification'],'mean':report['mean'],'ci95':report['ci95'],'signs':report['signs'],'purpose':'STUDY_ONLY'}
for name,value in [('audit.json',audit),('keys.json',keys),('current-execution-snapshot.json',s)]: (base/name).write_text(json.dumps(value,sort_keys=True,indent=2)+'\n')
print(json.dumps(audit,indent=2))
