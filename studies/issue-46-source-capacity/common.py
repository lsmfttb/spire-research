"""Disposable Issue46 composition; simulator and accepted public encoder own semantics."""
import hashlib
import json
import os
from pathlib import Path
import time

CONFIG = json.loads(Path(__file__).with_name('config.json').read_text())

def sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def write(path,value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp')
    with tmp.open('w') as stream:
        json.dump(value,stream,sort_keys=True,allow_nan=False); stream.flush(); os.fsync(stream.fileno())
    os.replace(tmp,path)

def event(path,value):
    with Path(path).open('a') as stream:
        stream.write(json.dumps(value,sort_keys=True,allow_nan=False)+'\n'); stream.flush(); os.fsync(stream.fileno())

def owner_plan():
    result=[{'owner':s,'split':'train','assisted':False,'old45':True} for s in range(437001,437097)]
    for s in range(440001,440417):
        split='train' if s<=440288 else 'validation' if s<=440352 else 'heldout'
        assisted=s>=440097 if split=='train' else s%2==0
        result.append({'owner':s,'split':split,'assisted':assisted,'old45':False})
    return result

class Restored:
    def __init__(self,adapter,checkpoint): self.adapter,self.checkpoint=adapter,checkpoint
    def reset(self,seed=None): return self.adapter.restore_checkpoint(self.checkpoint)
    def __getattr__(self,name): return getattr(self.adapter,name)

def prepared(adapter):
    from adapter import prepare_policy_input
    return prepare_policy_input(dict(adapter._sim.public_projection_v3()),list(adapter._sim.legal_actions()))

def controller(driver_seed=None):
    from sts_combat_rl.sim.online_controller import PolicyController,RoutedRunController
    from sts_combat_rl.sim.non_combat_policy import ExpertNonCombatDriver
    return RoutedRunController(Battle(),PolicyController(ExpertNonCombatDriver(CONFIG['driver_seed'] if driver_seed is None else driver_seed)))

class Battle:
    def __init__(self):
        from sts_combat_rl.sim.controller_contract import ControllerProvenance
        self.cpu=0.; self.calls=0
        self.provenance=ControllerProvenance(kind='native_search_v2',name='issue46_frozen_search_v2_400',config={'simulations':400,'include_potions':True,'root_selection_rule':'highest_mean','guidance':None,'information_regime':'full_simulator_state_oracle_like'})
    def select_action(self,adapter,snapshot,actions,context,step_index):
        from sts_combat_rl.sim.oracle_search import build_oracle_search_report,select_oracle_root_action,oracle_search_controller_metadata
        from sts_combat_rl.sim.controller_contract import ControllerDecision
        start=time.process_time()
        raw=adapter.battle_search_v2(snapshot,simulations=400,include_potions=True,policy_prior_callback=None,leaf_value_callback=None)
        self.cpu+=time.process_time()-start; self.calls+=1
        report=build_oracle_search_report(raw,actions,context,expected_native_api='StepSimulator.battle_search_v2.v1',expected_patch_identity='sts_lightspeed_battle_search_v2_tree_internal_v1')
        if not report.search_ok or not report.include_potions: raise ValueError('NATIVE_SEARCH_INVALID: '+repr(report.problems))
        target=select_oracle_root_action(report,selection_rule='highest_mean')
        return ControllerDecision(target.legal_action_index,self.provenance,'highest_mean',target.score,oracle_search_controller_metadata(report,target))
