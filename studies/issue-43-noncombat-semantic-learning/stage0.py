"""Bounded current-native admission canary. Trusted executor; no training labels."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import time

from adapter import prepare_policy_input, resolve_native_action
from student import Scorer, treatment, features, with_public_history
from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
from sts_combat_rl.sim.action_space import ActionSpaceConfig
from sts_combat_rl.sim.controlled_run import execute_controlled_run
from sts_combat_rl.sim.controller_contract import ControllerProvenance, ControllerDecision
from sts_combat_rl.sim.non_combat_policy import ExpertNonCombatDriver
from sts_combat_rl.sim.online_controller import PolicyController, RoutedRunController
from sts_combat_rl.sim.oracle_search import build_oracle_search_report, select_oracle_root_action
import slaythespire

CONFIG = json.loads(Path(__file__).with_name('config.json').read_text())
START = time.monotonic()


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def ledger(path, event):
    event.update(wall_seconds=time.monotonic()-START,cpu_seconds=time.process_time(),
                 peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    with path.open('a') as stream:
        stream.write(json.dumps(event,sort_keys=True)+'\n')
        stream.flush()
        os.fsync(stream.fileno())


class Battle:
    provenance = ControllerProvenance(kind='native_search',name='issue43_unguided_search_v2_400',config=CONFIG['battle'])

    def select_action(self,adapter,snapshot,actions,context,step_index):
        raw = adapter.battle_search_v2(snapshot,simulations=400,include_potions=False,
                                      policy_prior_callback=None,leaf_value_callback=None)
        report = build_oracle_search_report(raw,actions,context,
            expected_native_api='StepSimulator.battle_search_v2.v1',
            expected_patch_identity='sts_lightspeed_battle_search_v2_tree_internal_v1')
        if not report.search_ok:
            raise ValueError('BATTLE_TEACHER_INCOMPATIBLE: '+str(report.problems))
        target = select_oracle_root_action(report,selection_rule='highest_mean')
        return ControllerDecision(selected_index=target.legal_action_index,provenance=self.provenance,reason='highest_mean',score=target.score)


def controller(driver_seed):
    return RoutedRunController(battle=Battle(),non_combat=PolicyController(ExpertNonCombatDriver(seed=driver_seed)))


def prepared(adapter):
    return prepare_policy_input(dict(adapter._sim.public_projection_v3()),list(adapter._sim.legal_actions()))


class Restored:
    """Only reset semantics differ: exact same-native post-candidate cursor."""
    def __init__(self,adapter,checkpoint):
        self.adapter,self.checkpoint=adapter,checkpoint
    def reset(self,seed=None):
        return self.adapter.restore_checkpoint(self.checkpoint)
    def __getattr__(self,name):
        return getattr(self.adapter,name)


def synthetic_smoke(serialized):
    result={}
    for arm in ('visible','masked'):
        model=Scorer(CONFIG['model_seeds'][0])
        value=treatment(serialized,arm)
        rows=[(features(value,i),float(i%2),1.0) for i in range(len(value['legal_options']))]
        before=digest(model.state())
        gradients=[model.update(rows) for _ in range(10)]
        scores=model.scores(serialized,arm)
        assert max(scores)!=min(scores) and all(g>0 for g in gradients)
        assert digest(model.state())!=before
        result[arm]={'gradient_min':min(gradients),'scores':scores,'chosen':model.choose(serialized,arm),'model':model}
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--artifacts',type=Path,required=True)
    parser.add_argument('--resume-ledger',type=Path)
    args=parser.parse_args()
    args.artifacts.mkdir(parents=True,exist_ok=True)
    log=args.artifacts/'stage0.jsonl'
    if log.exists():
        raise RuntimeError('Use a new attempt directory; completed/failed attempts stay immutable.')
    extension=Path(slaythespire.__file__)
    completed={}
    resumed=[]
    if args.resume_ledger:
        resumed=[json.loads(line) for line in args.resume_ledger.read_text().splitlines()]
        origin=resumed[0]
        if origin['config_sha256']!=digest(CONFIG) or origin['native_extension_sha256']!=hashlib.sha256(extension.read_bytes()).hexdigest():
            raise RuntimeError('resume config/native identity mismatch')
        completed={tuple(row['key']):row for row in resumed if row['boundary']=='branch_completed' and row['terminal'] and not row['problems']}
    ledger(log,{'boundary':'start','config_sha256':digest(CONFIG),'native_extension':str(extension),
        'native_extension_sha256':hashlib.sha256(extension.read_bytes()).hexdigest(),
        'python':sys.version,'pid':os.getpid(),'native_commit':CONFIG['native_commit'],
        'frozen_stsrl_commit':CONFIG['frozen_stsrl_commit'],'attempt':args.artifacts.name,
        'producer_commit':os.environ.get('ISSUE43_PRODUCER_COMMIT','uncommitted'),
        'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'resume_ledger_sha256':hashlib.sha256(args.resume_ledger.read_bytes()).hexdigest() if args.resume_ledger else None})
    adapter=LightSpeedAdapter(seed=430001,ascension=20,module=slaythespire)
    roots=[]
    counts={'MAP_SCREEN':0,'REWARDS':0}
    try:
        for seed in range(430001,430513):
            captured=set()
            def observe(snapshot,actions,context,index):
                screen=context.screen_state
                if screen in counts:
                    p=prepared(adapter) # fail closed even on nonselected variants
                    if screen not in captured and counts[screen]<2 and len(actions)>=2:
                        cp=adapter.capture_checkpoint(snapshot)
                        original=next((row for row in resumed if row['boundary']=='root_captured' and row['seed']==seed and row['screen']==screen),None)
                        if original and original['public_sha256']!=digest(json.loads(p.serialized_input)):
                            raise RuntimeError('resume root public identity mismatch')
                        roots.append((seed,screen,index,cp,p,adapter.checkpoint_fingerprint(snapshot),
                                      context.public_run_context.get('history',[]),context.public_run_context))
                        counts[screen]+=1
                        captured.add(screen)
                        ledger(log,{'boundary':'root_captured','seed':seed,'screen':screen,'step':index,
                                    'public_sha256':digest(json.loads(p.serialized_input)),'actions':len(actions)})
            ledger(log,{'boundary':'source_started','seed':seed})
            run=execute_controlled_run(adapter,controller(CONFIG['source_driver_seed']),seed=seed,
                max_steps=CONFIG['max_steps'],action_space=ActionSpaceConfig.initial_no_potions(),before_decision=observe)
            ledger(log,{'boundary':'source_completed','seed':seed,'terminal':run.terminal,'problems':run.problems,
                        'floor':run.final_raw.get('floor_num'),'steps':len(run.steps)})
            if run.problems or not run.terminal:
                raise RuntimeError('INCOMPLETE: source '+str(run.problems))
            del run
            if all(n==2 for n in counts.values()):
                break
        if len(roots)!=4:
            raise RuntimeError('SOURCE_COVERAGE_INSUFFICIENT')
        for root_index,(seed,screen,step,cp,p,fingerprint,history,public_context) in enumerate(roots):
            restored=adapter.restore_checkpoint(cp)
            assert adapter.checkpoint_fingerprint(restored)==fingerprint
            student_input=with_public_history(p.serialized_input,history,public_context)
            smoke=synthetic_smoke(student_input)
            for arm,info in smoke.items():
                adapter.restore_checkpoint(cp)
                current=prepared(adapter)
                selected=info['model'].choose(with_public_history(current.serialized_input,history,public_context),arm)
                action=resolve_native_action(current,selected,dict(adapter._sim.public_projection_v3()),list(adapter._sim.legal_actions()))
                adapter._sim.step(action)
                ledger(log,{'boundary':'trained_public_choice_executed','root':root_index,'arm':arm,
                            'selected':selected,'scores':info['scores'],'gradient_min':info['gradient_min'],
                            'synthetic_only':True})
            for ordinal in CONFIG['canary']['forced_ordinals']:
                for driver_seed in CONFIG['continuation_seeds']['canary']:
                    key=[root_index,ordinal,driver_seed]
                    if tuple(key) in completed:
                        ledger(log,{'boundary':'branch_resumed','key':key,'original':completed[tuple(key)]})
                        continue
                    ledger(log,{'boundary':'branch_started','key':key})
                    restored=adapter.restore_checkpoint(cp)
                    assert adapter.checkpoint_fingerprint(restored)==fingerprint
                    fresh=prepared(adapter)
                    action=resolve_native_action(p,ordinal,dict(adapter._sim.public_projection_v3()),list(adapter._sim.legal_actions()))
                    native_actions=adapter.legal_actions(restored)
                    assert native_actions[ordinal].raw['native'].bits==action.bits
                    transition=adapter.step(native_actions[ordinal])
                    after=adapter.capture_checkpoint(transition.snapshot)
                    begin=time.monotonic(); cpu=time.process_time()
                    def progress(snapshot,actions,context,index):
                        if index%25==0:
                            ledger(log,{'boundary':'branch_progress','key':key,'step':index,'screen':context.screen_state})
                        if time.monotonic()-START>CONFIG['resource_caps']['stage0']['wall_seconds']:
                            raise RuntimeError('RESOURCE_BLOCKED: Stage0 wall cap')
                    run=execute_controlled_run(Restored(adapter,after),controller(driver_seed),seed=seed,
                        max_steps=CONFIG['max_steps'],action_space=ActionSpaceConfig.initial_no_potions(),before_decision=progress)
                    ledger(log,{'boundary':'branch_completed','key':key,'terminal':run.terminal,
                        'problems':run.problems,'steps':len(run.steps),'floor':run.final_raw.get('floor_num'),
                        'outcome':run.outcome,'branch_wall':time.monotonic()-begin,'branch_cpu':time.process_time()-cpu})
                    if run.problems or not run.terminal:
                        raise RuntimeError('INCOMPLETE: branch '+str(run.problems))
                    del run
                    if time.monotonic()-START>CONFIG['resource_caps']['stage0']['wall_seconds']:
                        raise RuntimeError('RESOURCE_BLOCKED: Stage0 wall budget')
        ledger(log,{'boundary':'canary_completed','root_count':len(roots)})
    except Exception as exc:
        ledger(log,{'boundary':'failed','exception':type(exc).__name__,'message':str(exc)})
        raise
    finally:
        adapter.close()


if __name__=='__main__':
    main()
