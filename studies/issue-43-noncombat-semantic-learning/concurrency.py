"""Admission-only repeat of first eight prespecified canary keys at 4/8 workers."""
from concurrent.futures import ProcessPoolExecutor
import argparse
import json
from pathlib import Path
import resource
import time
from stage0 import CONFIG, Battle, Restored, controller, prepared, digest
from adapter import resolve_native_action
from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
from sts_combat_rl.sim.action_space import ActionSpaceConfig
from sts_combat_rl.sim.controlled_run import execute_controlled_run
import slaythespire


class Captured(Exception):
    pass


def branch(key):
    resource.setrlimit(resource.RLIMIT_AS,(8*1024**3,8*1024**3))
    start=time.monotonic(); cpu=time.process_time()
    root,ordinal,driver_seed=key
    family=('MAP_SCREEN','REWARDS')[root]
    adapter=LightSpeedAdapter(seed=430001,ascension=20,module=slaythespire)
    state={}
    def observe(snapshot,actions,context,index):
        if context.screen_state==family and len(actions)>=2:
            state.update(cp=adapter.capture_checkpoint(snapshot),p=prepared(adapter),
                         fingerprint=adapter.checkpoint_fingerprint(snapshot))
            raise Captured()
    try:
        try:
            execute_controlled_run(adapter,controller(CONFIG['source_driver_seed']),seed=430001,
                max_steps=CONFIG['max_steps'],action_space=ActionSpaceConfig.initial_no_potions(),before_decision=observe)
        except Captured:
            pass
        if not state:
            raise RuntimeError('source root not reached')
        snapshot=adapter.restore_checkpoint(state['cp'])
        assert adapter.checkpoint_fingerprint(snapshot)==state['fingerprint']
        chosen=resolve_native_action(state['p'],ordinal,dict(adapter._sim.public_projection_v3()),list(adapter._sim.legal_actions()))
        actions=adapter.legal_actions(snapshot)
        assert actions[ordinal].raw['native'].bits==chosen.bits
        transition=adapter.step(actions[ordinal])
        cp=adapter.capture_checkpoint(transition.snapshot)
        run=execute_controlled_run(Restored(adapter,cp),controller(driver_seed),seed=430001,
            max_steps=CONFIG['max_steps'],action_space=ActionSpaceConfig.initial_no_potions())
        if not run.terminal or run.problems:
            raise RuntimeError(str(run.problems))
        return {'key':key,'terminal':run.terminal,'floor':run.final_raw.get('floor_num'),
                'outcome':run.outcome,'steps':len(run.steps),'wall':time.monotonic()-start,
                'cpu':time.process_time()-cpu,'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                'privileged_checkpoint_fingerprint_sha256':digest(state['fingerprint']),
                'public_input_sha256':digest(json.loads(state['p'].serialized_input))}
    finally:
        adapter.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--workers',type=int,choices=(4,8),required=True)
    args=parser.parse_args()
    if args.output.exists():
        raise RuntimeError('immutable attempt output exists')
    available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))
    if available<4*1024**2:
        raise RuntimeError('RESOURCE_BLOCKED: available memory below 4GiB')
    keys=[(root,ordinal,seed) for root in range(2) for ordinal in (0,1) for seed in CONFIG['continuation_seeds']['canary']]
    start=time.monotonic()
    rows=[]
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        for row in executor.map(branch,keys):
            rows.append(row)
            with args.output.with_suffix('.jsonl').open('a') as stream:
                stream.write(json.dumps(row,sort_keys=True)+'\n');stream.flush()
    result={'workers':args.workers,'keys':keys,'rows':rows,'wall':time.monotonic()-start,
            'aggregate_worker_cpu':sum(row['cpu'] for row in rows),'available_memory_kib_before':available,
            'disposition':'ADMISSION_COST_ONLY','config_sha256':digest(CONFIG)}
    args.output.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')


if __name__=='__main__':
    main()
