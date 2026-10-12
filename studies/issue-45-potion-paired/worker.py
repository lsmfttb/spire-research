"""Thin composition of native Search-v2 and the existing whole-run executor."""
import json
import os
from pathlib import Path
import resource
import sys
import time
from collections import Counter

from sts_combat_rl.sim.action_space import ActionSpaceConfig, POTION_ACTION_KINDS
from sts_combat_rl.sim.controlled_run import execute_controlled_run, build_decision_context
from sts_combat_rl.sim.controller_contract import ControllerDecision, ControllerProvenance
from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
from sts_combat_rl.sim.non_combat_policy import ExpertNonCombatDriver
from sts_combat_rl.sim.online_controller import PolicyController, RoutedRunController
from sts_combat_rl.sim.oracle_search import build_oracle_search_report, select_oracle_root_action, oracle_search_controller_metadata

DRIVER_SEED = 433042  # Accepted stable driver_seed + simulator_seed lifecycle.
NATIVE_API = 'StepSimulator.battle_search_v2.v1'
NATIVE_PATCH = 'sts_lightspeed_battle_search_v2_tree_internal_v1'

def write(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, sort_keys=True, allow_nan=False))
    os.replace(tmp, path)

class Battle:
    def __init__(self, enabled):
        self.enabled = enabled
        self.calls = 0; self.cpu = 0.; self.wall = 0.
        self.provenance = ControllerProvenance(kind='native_search_v2', name='issue45_search_v2_400',
            config={'simulations':400, 'include_potions':enabled, 'root_selection_rule':'highest_mean',
                    'information_regime':'full_simulator_state_oracle_like', 'guidance':None})

    def select_action(self, adapter, snapshot, actions, context, step_index):
        before_cpu = time.process_time(); before_wall = time.monotonic()
        raw = adapter.battle_search_v2(snapshot, simulations=400, include_potions=self.enabled,
                                     policy_prior_callback=None, leaf_value_callback=None)
        self.cpu += time.process_time()-before_cpu; self.wall += time.monotonic()-before_wall; self.calls += 1
        report = build_oracle_search_report(raw, actions, context,
            expected_native_api=NATIVE_API, expected_patch_identity=NATIVE_PATCH)
        if not report.search_ok or report.include_potions != self.enabled:
            raise ValueError('POTION_ACTION_MAPPING_INVALID: '+repr(report.problems))
        target = select_oracle_root_action(report, selection_rule='highest_mean')
        if not self.enabled and actions[target.legal_action_index].kind in POTION_ACTION_KINDS:
            raise ValueError('POTION_ACTION_MAPPING_INVALID: disabled Battle potion')
        metadata = oracle_search_controller_metadata(report,target)
        return ControllerDecision(target.legal_action_index,self.provenance,'highest_mean',target.score,metadata)

def main():
    began = time.monotonic(); cpu = time.process_time()
    base=Path(sys.argv[1]); seed=int(sys.argv[2]); arm=sys.argv[3]
    assert seed in range(437001,437097) and arm in ('off','on')
    resource.setrlimit(resource.RLIMIT_CPU,(600,600)); resource.setrlimit(resource.RLIMIT_AS,(8*1024**3,8*1024**3))
    name=f'{seed}-{arm}'; adapter=LightSpeedAdapter(ascension=20)
    battle=Battle(arm=='on'); counts=Counter(); maxima={'act':1,'floor':0}; opportunities=set(); offered=set(); encounters=set()
    action_space=ActionSpaceConfig.include_all() if arm=='on' else ActionSpaceConfig.initial_no_potions()
    log=(base/(name+'.jsonl')).open('a')
    parity_done=False
    def before(snapshot,actions,context,index):
        nonlocal parity_done
        if seed==437001 and arm=='off' and not parity_done and snapshot.raw.get('battle_active') and any(a.kind in ('potion','potion_discard') for a in actions):
            # One focused real-root parity check at an actual scheduled seed;
            # native search copies the same state and owns target/slot legality.
            evidence=[]
            for enabled in (False,True):
                cfg=ActionSpaceConfig.include_all() if enabled else ActionSpaceConfig.initial_no_potions()
                ctx=build_decision_context(snapshot.raw,actions,cfg)
                raw=adapter.battle_search_v2(snapshot,simulations=400,include_potions=enabled,
                    policy_prior_callback=None,leaf_value_callback=None)
                rep=build_oracle_search_report(raw,actions,ctx,expected_native_api=NATIVE_API,expected_patch_identity=NATIVE_PATCH)
                if not rep.search_ok or rep.include_potions!=enabled:
                    raise ValueError('POTION_ACTION_MAPPING_INVALID: native parity '+repr(rep.problems))
                evidence.append({'include_potions':enabled,'raw_search':raw})
            write(base/'native-parity.json',{'seed':seed,'step':index,'legal_action_kinds':[a.kind for a in actions],'checks':evidence})
            parity_done=True
        if index%25==0:
            write(base/(name+'-progress.json'),{'seed':seed,'arm':arm,'step':index,'floor':snapshot.raw.get('floor_num'),
                'cpu':time.process_time()-cpu,'wall':time.monotonic()-began})
    def after(step):
        battle_step=step.battle_active
        role='battle' if battle_step else 'nonbattle'
        kinds=step.legal_action_kinds; chosen=step.chosen_action_kind
        maxima['act']=max(maxima['act'],int(step.snapshot_raw.get('act',1)),int(step.next_snapshot_raw.get('act',1)))
        maxima['floor']=max(maxima['floor'],int(step.floor or 0),int(step.next_floor or 0))
        if battle_step: encounters.add(str(step.snapshot_raw.get('encounter')))
        counts['decisions']+=1; counts[role+'_decisions']+=1
        if any(k in ('potion','game_potion_use') for k in kinds): counts[role+'_potion_eligible_decisions']+=1
        if any(k in POTION_ACTION_KINDS for k in kinds): opportunities.add((step.floor,step.screen_state))
        for identity,kind in zip(step.legal_action_identities,kinds):
            if kind in ('reward_potion','shop_reward_potion'):
                offered.add((step.floor,step.screen_state,json.dumps(identity,sort_keys=True)))
        if chosen in ('potion','game_potion_use'): counts[role+'_consumed_actions']+=1
        if chosen in ('potion_discard','game_potion_discard'): counts[role+'_discarded_actions']+=1
        if chosen=='reward_potion': counts['earned_reward_actions']+=1
        if chosen=='shop_reward_potion': counts['purchased_actions']+=1
        if chosen in ('potion_discard','game_potion_discard') and any(k in ('reward_potion','shop_reward_potion') for k in kinds):
            counts['replacement_preparation_discards']+=1
        if step.potion_count is not None and step.next_potion_count is not None:
            counts['inventory_net_additions']+=max(0,step.next_potion_count-step.potion_count)
        if battle_step and step.player_hp is not None and step.next_player_hp is not None:
            counts['battle_hp_loss_positive_transitions']+=max(0,step.player_hp-step.next_player_hp)
        row={k:getattr(step,k) for k in ('step_index','controller_role','screen_state','legal_action_kinds',
            'eligible_action_indices','chosen_action_index','chosen_action_identity','chosen_action_kind',
            'legal_action_identities','terminal_after_step','floor','next_floor','player_hp','next_player_hp',
            'gold','next_gold','potion_count','next_potion_count','battle_active','next_battle_active',
            'battle_outcome','next_battle_outcome','decision_metadata','snapshot_raw','next_snapshot_raw')}
        log.write(json.dumps(row,sort_keys=True,allow_nan=False)+'\n'); log.flush()
    try:
        run=execute_controlled_run(adapter,RoutedRunController(battle,PolicyController(ExpertNonCombatDriver(DRIVER_SEED))),
            seed=seed,max_steps=20000,action_space=action_space,before_decision=before,after_transition=after,retain_steps=False)
        valid=run.terminal and not run.problems and run.outcome in ('PLAYER_LOSS','PLAYER_WIN')
        result={'seed':seed,'arm':arm,'terminal':run.terminal,'outcome':run.outcome,'problems':run.problems,
            'producer':os.environ['ISSUE45_PRODUCER'],
            'valid':valid,'floor':run.final_raw.get('floor_num',maxima['floor']), 'max_act':maxima['act'],
            'initial_raw':dict(run.initial_raw),'final_raw':dict(run.final_raw),'metrics':dict(counts),
            'source':json.loads((base/'source.json').read_text()),
            'battle_encounters':sorted(encounters),
            'loss_type':('battle' if run.final_raw.get('battle_active') else 'nonbattle') if run.outcome=='PLAYER_LOSS' else None,
            'loss_encounter':run.final_raw.get('encounter') if run.outcome=='PLAYER_LOSS' else None,
            'potion_offer_option_occurrences':len(offered),'potion_opportunity_floor_screens':len(opportunities),
            'search_calls':battle.calls,'search_cpu':battle.cpu,'search_wall':battle.wall,
            'controller':dict(run.controller_provenance),'action_space':dict(run.action_space_config),
            'cpu':time.process_time()-cpu,'wall':time.monotonic()-began,
            'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
        write(base/(name+'.json'),result)
        if not valid: raise ValueError('INCOMPLETE: '+repr(run.problems))
    finally:
        log.close(); adapter.close()

if __name__=='__main__': main()
