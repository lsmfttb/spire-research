"""One genuine source owner, then every action x two unassisted terminal continuations."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import time
from collections import Counter
from common import CONFIG,sha,write,event,owner_plan,controller,prepared,Restored
from student import with_public_history
from adapter import resolve_native_action
from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
from sts_combat_rl.sim.action_space import ActionSpaceConfig
from sts_combat_rl.sim.controlled_run import execute_controlled_run
from sts_combat_rl.sim.controller_contract import ControllerDecision,ControllerProvenance
from sts_combat_rl.sim.assisted_source_generation import assistance_schedule_by_level,_apply_assistance_schedule

STORE=Path(sys.argv[1]); OWNER=int(sys.argv[2]); PLAN=next(p for p in owner_plan() if p['owner']==OWNER)
DIR=STORE/str(OWNER); DIR.mkdir(parents=True,exist_ok=True)
START=time.process_time(); WALL=time.monotonic()
resource.setrlimit(resource.RLIMIT_CPU,(10000,10000)); resource.setrlimit(resource.RLIMIT_AS,(8*1024**3,8*1024**3))
SOURCE=json.loads((STORE/'source.json').read_text())
PRODUCER=os.environ['ISSUE46_PRODUCER']
if PRODUCER!=SOURCE['producer']: raise ValueError('producer mismatch')

class Replay:
    def __init__(self,rows):
        self.rows=rows; self.provenance=ControllerProvenance(kind='recorded_authentic_source_replay',name='issue45_on',config={'source':SOURCE['old45_manifest_sha256']})
    def select_action(self,adapter,snapshot,actions,context,step_index):
        row=self.rows[step_index]
        if dict(snapshot.raw)!=row['snapshot_raw']: raise ValueError('OLD45_SNAPSHOT_MISMATCH')
        return ControllerDecision(row['chosen_action_index'],self.provenance,'sealed_issue45_action')

def run(adapter,selected,name,seed,before=None,transform=None):
    start=time.process_time(); last=None; maximum=1; heart=False; counts=Counter(); choices=Counter()
    trace=gzip.open(DIR/(name+'.jsonl.gz'),'wt',compresslevel=1)
    def after(step):
        nonlocal last,maximum,heart
        last=step
        maximum=max(maximum,int(step.snapshot_raw.get('act',1)),int(step.next_snapshot_raw.get('act',1)))
        heart|=step.snapshot_raw.get('encounter_id')=='THE_HEART' or step.next_snapshot_raw.get('encounter_id')=='THE_HEART'
        role='battle' if step.battle_active else 'nonbattle'
        counts[role+'_decisions']+=1; choices[step.screen_state]+=1
        kind=step.chosen_action_kind
        if kind in ('potion','game_potion_use'): counts[role+'_potion_use']+=1
        if kind in ('potion_discard','game_potion_discard'): counts[role+'_potion_discard']+=1
        if kind in ('reward_potion','shop_reward_potion'): counts['potion_acquisition']+=1
        value={key:getattr(step,key) for key in ('step_index','screen_state','battle_active','legal_action_identities','eligible_action_indices','chosen_action_index','chosen_action_identity','chosen_action_kind','terminal_after_step','decision_metadata','snapshot_raw','next_snapshot_raw')}
        trace.write(json.dumps(value,sort_keys=True,allow_nan=False)+'\n'); trace.flush()
        write(DIR/'progress.json',{'owner':OWNER,'boundary':name,'step':step.step_index,'floor':step.next_floor,'cpu':time.process_time()-START,'wall':time.monotonic()-WALL}) if step.step_index%25==0 else None
    try:
        result=execute_controlled_run(adapter,selected,seed=seed,max_steps=CONFIG['max_steps'],action_space=ActionSpaceConfig.include_all(),before_decision=before,before_decision_transform=transform,after_transition=after,retain_steps=False)
    finally: trace.close()
    value={'terminal':result.terminal,'problems':result.problems,'outcome':result.outcome,'terminal_floor':result.final_raw.get('floor_num'),'initial_raw':result.initial_raw,'final_raw':result.final_raw,'max_act':maximum,'heart_reached':heart,'heart_win':heart and result.outcome=='PLAYER_WIN','loss_type':('battle' if last.battle_active else 'nonbattle') if last and result.outcome=='PLAYER_LOSS' else None,'loss_encounter':last.snapshot_raw.get('encounter_id') if last and result.outcome=='PLAYER_LOSS' else None,'metrics':dict(counts),'screens':dict(choices),'cpu':time.process_time()-start,'source':SOURCE,'producer':PRODUCER}
    write(DIR/(name+'.json'),value)
    if not result.terminal or result.problems or result.outcome not in ('PLAYER_WIN','PLAYER_LOSS'): raise ValueError('INCOMPLETE_NATIVE_RUN: '+repr(result.problems))
    return value

def main():
    adapter=LightSpeedAdapter(ascension=20); chosen=None; chosen_native=None; best=None; occupancy=Counter(); assistance=[]; active=False; battle_index=0
    original=None
    if PLAN['old45']:
        old=Path(SOURCE['old45_store']); original=[json.loads(line) for line in (old/f'{OWNER}-on.jsonl').read_text().splitlines()]
        result=json.loads((old/f'{OWNER}-on.json').read_text())
        if not result['valid'] or not result['terminal'] or result['source']['native_commit']!=CONFIG['native_commit']: raise ValueError('OLD45_IDENTITY_INVALID')
        selected=Replay(original)
    else: selected=controller()
    def transform(current,snapshot,index):
        nonlocal active,battle_index
        if not snapshot.raw.get('battle_active'): active=False; return snapshot
        if active: return snapshot
        active=True
        decision,changed=_apply_assistance_schedule(current,snapshot,schedule=assistance_schedule_by_level(CONFIG['assistance_level']),policy_seed=CONFIG['driver_seed'],source_run_id=f'issue46-{OWNER}',source_seed=OWNER,source_battle_index=battle_index,step_index=index)
        battle_index+=1; assistance.append(decision)
        if decision['problems'] or decision['native_support_status'] in ('unsupported','error'): raise ValueError('ASSISTANCE_UNSUPPORTED: '+repr(decision))
        return changed
    def before(snapshot,actions,context,index):
        nonlocal chosen,chosen_native,best
        if original is not None and dict(snapshot.raw)!=original[index]['snapshot_raw']: raise ValueError('OLD45_REPLAY_CHANGED')
        family=context.screen_state
        if family not in ('MAP_SCREEN','REWARDS'): return
        p=prepared(adapter)
        serialized=with_public_history(p.serialized_input,context.public_run_context.get('history',[]),context.public_run_context)
        stratum=[snapshot.raw.get('act'),snapshot.raw.get('floor_num'),family]
        occupancy[json.dumps(stratum)]+=1
        preference='MAP_SCREEN' if OWNER%2==0 else 'REWARDS'
        rank=(family!=preference,sha([46001,OWNER,stratum]),sha([46002,OWNER,index]))
        if best is not None and rank>=best: return
        best=rank
        fingerprint=adapter.checkpoint_fingerprint(snapshot)
        chosen={**PLAN,'step':index,'act':stratum[0],'floor':stratum[1],'family':family,'legal_count':len(actions),'serialized':serialized,'public_sha256':sha(json.loads(serialized)),'native_fingerprint_sha256':sha(fingerprint),'root_raw':dict(snapshot.raw),'candidate_sha256':[sha(v) for v in json.loads(p.serialized_input)['legal_options']]}
        chosen_native=(adapter.capture_checkpoint(snapshot),fingerprint,p)
    try:
        event(STORE/'events.jsonl',{'boundary':'source_started',**PLAN,'pid':os.getpid()})
        src=run(adapter,selected,'source',OWNER,before,transform if PLAN['assisted'] else None)
        if original is not None and src['final_raw']!=original[-1]['next_snapshot_raw']: raise ValueError('OLD45_FINAL_CHANGED')
        if chosen is None: raise ValueError('SOURCE_ROOT_MISSING')
        chosen['root_id']=sha(chosen)
        write(DIR/'root.json',chosen); write(DIR/'occupancy.json',dict(occupancy)); write(DIR/'assistance.json',assistance)
        event(STORE/'events.jsonl',{'boundary':'root_frozen',**PLAN,'root_id':chosen['root_id'],'family':chosen['family'],'floor':chosen['floor'],'legal_count':chosen['legal_count']})
        cp,fingerprint,p=chosen_native; labels=[]
        for ordinal in range(chosen['legal_count']):
            for driver in CONFIG['continuation_driver_seeds']:
                name=f'label-{ordinal}-{driver}'
                event(STORE/'events.jsonl',{'boundary':'continuation_started','owner':OWNER,'ordinal':ordinal,'driver':driver})
                snapshot=adapter.restore_checkpoint(cp)
                if adapter.checkpoint_fingerprint(snapshot)!=fingerprint: raise ValueError('RESTORE_FIDELITY_INVALID')
                native=resolve_native_action(p,ordinal,dict(adapter._sim.public_projection_v3()),list(adapter._sim.legal_actions()))
                actions=adapter.legal_actions(snapshot)
                if native.bits!=actions[ordinal].raw['native'].bits: raise ValueError('PUBLIC_ACTION_MAPPING_INVALID')
                transition=adapter.step(actions[ordinal]); post=adapter.capture_checkpoint(transition.snapshot)
                value=run(Restored(adapter,post),controller(driver),name,OWNER)
                value.update(owner=OWNER,root_id=chosen['root_id'],ordinal=ordinal,driver_seed=driver,forced_action_identity=chosen['candidate_sha256'][ordinal],forced_pre_raw=chosen['root_raw'],forced_post_raw=dict(transition.snapshot.raw),new_assistance=False)
                write(DIR/(name+'.json'),value); labels.append(value)
        q=[sum(v['terminal_floor'] for v in labels if v['ordinal']==i)/2 for i in range(chosen['legal_count'])]
        write(DIR/'table.json',{'root':chosen,'q':q,'source_cpu':src['cpu'],'label_cpu':sum(v['cpu'] for v in labels),'branch_count':len(labels),'producer':PRODUCER})
        write(DIR/'complete.json',{'owner':OWNER,'cpu':time.process_time()-START,'wall':time.monotonic()-WALL,'producer':PRODUCER,'table_sha256':hashlib.sha256((DIR/'table.json').read_bytes()).hexdigest()})
    finally: adapter.close()

if __name__=='__main__':
    try: main()
    except BaseException as exc:
        write(DIR/'failure.json',{'owner':OWNER,'error':repr(exc),'cpu':time.process_time()-START,'producer':PRODUCER}); raise
