"""One prescribed fresh standard-start run; models get serialized public JSON only."""
import gzip
import json
import os
from pathlib import Path
import resource
import sys
import time
from collections import Counter
from common import CONFIG,sha,write,prepared,controller,Battle
from student import with_public_history
from model import load
from adapter import resolve_native_action
from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
from sts_combat_rl.sim.action_space import ActionSpaceConfig
from sts_combat_rl.sim.controlled_run import execute_controlled_run
from sts_combat_rl.sim.controller_contract import ControllerDecision,ControllerProvenance
from sts_combat_rl.sim.online_controller import RoutedRunController,PolicyController
from sts_combat_rl.sim.non_combat_policy import ExpertNonCombatDriver

class Learned:
    def __init__(self,checkpoint):
        self.model=load(checkpoint); self.checkpoint=checkpoint
        self.fallback=PolicyController(ExpertNonCombatDriver(CONFIG['driver_seed']))
        self.counts=Counter()
        self.provenance=ControllerProvenance(kind='decision_policy',name='issue47_'+checkpoint['cell'],config={'information_regime':'normal_public_policy','checkpoint_sha256':checkpoint['sha256'],'supported':['MAP_SCREEN','REWARDS'],'unsupported':'expert_non_combat_v1'})
    def reset_for_run(self,seed): self.fallback.reset_for_run(seed); self.counts.clear()
    def select_action(self,adapter,snapshot,actions,context,step_index):
        family=context.screen_state
        if family not in ('MAP_SCREEN','REWARDS'):
            self.counts['unsupported_expert']+=1
            return self.fallback.select_action(adapter,snapshot,actions,context,step_index)
        p=prepared(adapter)
        serialized=with_public_history(p.serialized_input,context.public_run_context.get('history',[]),context.public_run_context)
        index=self.model.choose(serialized,'visible')
        native=resolve_native_action(p,index,dict(adapter._sim.public_projection_v3()),list(adapter._sim.legal_actions()))
        if native.bits!=actions[index].raw['native'].bits: raise ValueError('PUBLIC_INPUT_INVALID: action mapping')
        self.counts[family]+=1
        return ControllerDecision(index,self.provenance,'frozen_public_scorer',metadata={'public_sha256':sha(json.loads(serialized)),'serialized_public_input':serialized,'checkpoint_sha256':self.checkpoint['sha256']})

def main():
    store=Path(sys.argv[1]); seed=int(sys.argv[2]); arm=sys.argv[3]
    if not 471001<=seed<=471128 or arm not in ('A','P','Expert'): raise ValueError('not a prescribed fresh key')
    resource.setrlimit(resource.RLIMIT_CPU,(5000,5000)); resource.setrlimit(resource.RLIMIT_AS,(8*1024**3,8*1024**3))
    start=time.process_time(); wall=time.monotonic(); directory=store/'fresh'; directory.mkdir(exist_ok=True); name=f'{seed}-{arm}'
    source=json.loads((store/'source.json').read_text()); checkpoints=json.loads((store/'training/checkpoints.json').read_text())
    selected=controller() if arm=='Expert' else RoutedRunController(Battle(),Learned(checkpoints[arm]))
    adapter=LightSpeedAdapter(ascension=20); metrics=Counter(); screens=Counter(); offers=set(); maximum=1; heart=False; last=None; public=None; encounters=set()
    trace=gzip.open(directory/(name+'.jsonl.gz'),'wt',compresslevel=1)
    def before(snapshot,actions,context,index):
        nonlocal public
        public=None
        if context.screen_state in ('MAP_SCREEN','REWARDS'):
            p=prepared(adapter)
            public=with_public_history(p.serialized_input,context.public_run_context.get('history',[]),context.public_run_context)
    def after(step):
        nonlocal maximum,heart,last
        last=step; raw=step.snapshot_raw; nxt=step.next_snapshot_raw; role='battle' if step.battle_active else 'nonbattle'
        maximum=max(maximum,int(raw.get('act',1)),int(nxt.get('act',1)))
        heart|=raw.get('encounter_id')=='THE_HEART' or nxt.get('encounter_id')=='THE_HEART'
        if step.battle_active: encounters.add(str(raw.get('encounter_id')))
        metrics[role+'_decisions']+=1; screens[step.screen_state]+=1
        kind=step.chosen_action_kind
        if kind in ('potion','game_potion_use'): metrics[role+'_potion_use']+=1
        if kind in ('potion_discard','game_potion_discard'): metrics[role+'_potion_discard']+=1
        if kind=='reward_potion': metrics['potion_reward_acquisition']+=1
        if kind=='shop_reward_potion': metrics['potion_purchase']+=1
        if any(k in ('potion','potion_discard','game_potion_use','game_potion_discard') for k in step.legal_action_kinds): metrics[role+'_potion_action_opportunities']+=1
        for identity,k in zip(step.legal_action_identities,step.legal_action_kinds):
            if k in ('reward_potion','shop_reward_potion'): offers.add((step.floor,step.screen_state,sha(identity)))
        if step.potion_count is not None and step.next_potion_count is not None: metrics['inventory_net_additions']+=max(0,step.next_potion_count-step.potion_count)
        if step.battle_active and step.player_hp is not None and step.next_player_hp is not None: metrics['battle_hp_loss_positive_transitions']+=max(0,step.player_hp-step.next_player_hp)
        row={key:getattr(step,key) for key in ('step_index','screen_state','battle_active','legal_action_identities','eligible_action_indices','chosen_action_index','chosen_action_identity','chosen_action_kind','terminal_after_step','decision_metadata','snapshot_raw','next_snapshot_raw')}
        row['serialized_public_input']=public; row['route']='battle_search' if step.battle_active else 'expert' if arm=='Expert' else 'learned' if public is not None else 'explicit_unsupported_expert'
        trace.write(json.dumps(row,sort_keys=True,allow_nan=False)+'\n'); trace.flush()
        if step.step_index%25==0: write(directory/(name+'-progress.json'),{'seed':seed,'arm':arm,'step':step.step_index,'floor':step.next_floor,'cpu':time.process_time()-start})
    try:
        result=execute_controlled_run(adapter,selected,seed=seed,max_steps=CONFIG['max_steps'],action_space=ActionSpaceConfig.include_all(),before_decision=before,after_transition=after,retain_steps=False)
        value={'seed':seed,'arm':arm,'terminal':result.terminal,'outcome':result.outcome,'problems':result.problems,'floor':result.final_raw.get('floor_num'),'final_hp':result.final_raw.get('cur_hp'),'max_act':maximum,'heart_reached':heart,'heart_win':heart and result.outcome=='PLAYER_WIN','initial_raw':result.initial_raw,'final_raw':result.final_raw,'loss_type':('battle' if last.battle_active else 'nonbattle') if last and result.outcome=='PLAYER_LOSS' else None,'loss_encounter':last.snapshot_raw.get('encounter_id') if last and result.outcome=='PLAYER_LOSS' else None,'metrics':dict(metrics),'screens':dict(screens),'potion_offer_option_occurrences':len(offers),'encounters':sorted(encounters),'learner_uses':dict(selected.non_combat.counts) if arm!='Expert' else {},'checkpoint_sha256':None if arm=='Expert' else checkpoints[arm]['sha256'],'cpu':time.process_time()-start,'search_cpu':selected.battle.cpu,'search_calls':selected.battle.calls,'wall':time.monotonic()-wall,'source_sha256':sha(source),'producer':os.environ['ISSUE47_PRODUCER']}
        write(directory/(name+'.json'),value)
        if not result.terminal or result.problems or result.outcome not in ('PLAYER_LOSS','PLAYER_WIN'): raise ValueError('INCOMPLETE_FRESH: '+repr(result.problems))
    except BaseException as exc:
        write(directory/(name+'-failure.json'),{'error':repr(exc),'cpu':time.process_time()-start}); raise
    finally: trace.close(); adapter.close()

if __name__=='__main__': main()
