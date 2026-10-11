"""Synthetic training and real three-arm dispatch on admission seed 434001.

No formal labels or 433xxx evaluation. Synthetic checkpoints are retained only
for the Reviewer to inspect the executable freeze/controller path.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import time
from learning import fit
from formal import atomic, evaluation_plan, evaluation_controller
from stage0 import CONFIG, digest
from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
from sts_combat_rl.sim.action_space import ActionSpaceConfig
from sts_combat_rl.sim.controlled_run import execute_controlled_run
import slaythespire


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--roots',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(); start=time.monotonic(); cpu=time.process_time()
    scan=json.loads(args.roots.read_text())
    # Fake ordinal targets are independent of simulator outcomes. Both arms use
    # the exact same two public fixtures and targets; this is NOT a fitted study.
    train=[]; validation=[]
    for i,root in enumerate(scan['roots'][:2]):
        table={'root_id':'synthetic_'+str(i),'serialized':root['serialized_public_input'],
               'q':[float(ordinal%2)*10 for ordinal in range(root['legal_count'])]}
        train.append({**table,'split':'train','owner':430001})
        validation.append({**table,'split':'validation','owner':431001})
    models=fit(train,validation)
    checkpoints={arm:value['checkpoint'] for arm,value in models.items()}
    atomic(args.output.with_name('synthetic-checkpoints.json'),{'purpose':'SYNTHETIC_READINESS_ONLY','models':models})
    plan=evaluation_plan(checkpoints); rows=[]
    def progress(snapshot,actions,context,index):
        if time.process_time()-cpu>100 or time.monotonic()-start>110:
            raise RuntimeError('RESOURCE_BLOCKED: readiness reserve')
    for arm in ('visible','masked','expert_source'):
        adapter=LightSpeedAdapter(seed=434001,ascension=20,module=slaythespire)
        selected=evaluation_controller(arm,checkpoints); before=time.process_time(); wall=time.monotonic()
        try:
            run=execute_controlled_run(adapter,selected,seed=434001,max_steps=CONFIG['max_steps'],
                action_space=ActionSpaceConfig.initial_no_potions(),before_decision=progress)
            if not run.terminal or run.problems:
                raise RuntimeError('invalid readiness run '+str(run.problems))
            choices=selected.non_combat.choices if arm!='expert_source' else []
            if arm!='expert_source':
                assert all(any(c['route']=='learned' and c['family']==family for c in choices)
                           for family in ('MAP_SCREEN','REWARDS'))
                assert any(c['route']=='explicit_other_fallback' for c in choices)
            rows.append({'arm':arm,'admission_seed':434001,'terminal':True,'problems':[],
                         'steps':len(run.steps),'choices':choices,'cpu':time.process_time()-before,
                         'wall':time.monotonic()-wall})
        finally:
            adapter.close()
    atomic(args.output,{'purpose':'SYNTHETIC_READINESS_ONLY','producer_commit':os.environ['ISSUE43_PRODUCER_COMMIT'],
        'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'config_sha256':digest(CONFIG),
        'plan_rows':len(plan),'plan_sha256':digest(plan),'models':{a:c['checkpoint_sha256'] for a,c in checkpoints.items()},
        'runs':rows,'cpu':time.process_time()-cpu,'wall':time.monotonic()-start,
        'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss})


if __name__=='__main__':
    main()
