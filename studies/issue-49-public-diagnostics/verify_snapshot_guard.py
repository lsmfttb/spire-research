"""Replay sealed native transition observations; no new native execution."""
import json,hashlib,time,sys,subprocess,types
from pathlib import Path
import prototype
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
    start=time.process_time();original=Path(sys.argv[1]);out=Path(sys.argv[2]);out.mkdir(parents=True,exist_ok=True)
    public=json.loads((original/'visibility-public-fixtures.json').read_text());native=json.loads((original/'visibility-native.json').read_text())
    checks={};before=public['before']
    def identity(o):return {k:o['action'][k] for k in ('scope','kind','idx1','idx2','idx3')}
    for name,kind,after_name in [('native_card_group_remap','reward_card','after_card_group0'),('native_inventory_slot_reuse','game_potion_discard','after_potion_take')]:
        i=next(i for i,o in enumerate(before['legal_options']) if o['action']['kind']==kind and o['action']['idx1']==0 and o['action']['idx2']==0)
        old=before['legal_options'][i];after=public[after_name]
        replacement=next(o for o in after['legal_options'] if identity(o)==identity(old))
        assert old['semantics']!=replacement['semantics']
        try:prototype.mapped_action(before,i,native[after_name]['candidate_actions'],current_public=after)
        except ValueError as error:checks[name]={'passed':True,'reason':str(error),'old_semantics':old['semantics'],'replacement_semantics':replacement['semantics'],'reused_identity':identity(old)}
        else:raise AssertionError('reused identity accepted')
    for name,p in public.items():
        for i,o in enumerate(p['legal_options']):assert prototype.mapped_action(p,i,native[name]['candidate_actions'],current_public=p)==native[name]['candidate_actions'][i]
    checks['all_current_native_candidates_resolve']=True
    old_source=subprocess.check_output(['git.exe','-C','D:/DeadlycatCoding/spire-research','show','9e3b4c6f531b86f7d2e94d1098fc5ad22a79bb27:studies/issue-49-public-diagnostics/prototype.py']).decode()
    old=types.ModuleType('old_prototype');exec(compile(old_source,'original sealed prototype','exec'),old.__dict__)
    for p in public.values():
        for i in range(len(p['legal_options'])):assert prototype.features(p,i)==old.features(p,i)
    checks['all_policy_features_identical_to_original']=True
    try:prototype.mapped_action(before,0,native['before']['candidate_actions'])
    except ValueError:checks['missing_current_snapshot_rejected']=True
    else:raise AssertionError('missing fresh snapshot accepted')
    try:prototype.mapped_action(before,0,native['after_card_group0']['candidate_actions'],current_public=before)
    except ValueError:checks['mismatched_snapshot_candidate_list_rejected']=True
    else:raise AssertionError('inconsistent candidate list accepted')
    receipt={'passed':True,'checks':checks,'inputs':[{'path':p.name,'sha256':sha(p)} for p in (original/'visibility-public-fixtures.json',original/'visibility-native.json')],'cpu_seconds':time.process_time()-start,'fresh_games':0,'native_executions':0,'training_steps':0,'Q_labels_generated':0,'guard_external_to_policy_features':True}
    (out/'snapshot-verification.json').write_text(json.dumps(receipt,sort_keys=True,indent=2)+'\n');print(json.dumps(receipt))
if __name__=='__main__':main()
