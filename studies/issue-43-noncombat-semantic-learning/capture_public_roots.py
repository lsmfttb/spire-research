"""Reconstruct and retain canary public inputs; stop at roots, no return labels."""
import json
import os
from pathlib import Path
import sys
from concurrency import Captured
from stage0 import CONFIG, controller, prepared, digest
from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
from sts_combat_rl.sim.action_space import ActionSpaceConfig
from sts_combat_rl.sim.controlled_run import execute_controlled_run
import slaythespire

root=Path(sys.argv[1])
old=[json.loads(line) for line in (root/'stage0-attempt03/stage0.jsonl').read_text().splitlines()]
expected={(row['seed'],row['screen']):row for row in old if row['boundary']=='root_captured'}
rows=[]
for seed in (430001,430002):
    adapter=LightSpeedAdapter(seed=seed,ascension=20,module=slaythespire)
    found=set()
    def observe(snapshot,actions,context,index):
        screen=context.screen_state
        if screen in ('MAP_SCREEN','REWARDS') and screen not in found and len(actions)>=2:
            p=prepared(adapter)
            assert digest(json.loads(p.serialized_input))==expected[(seed,screen)]['public_sha256']
            rows.append({'seed':seed,'screen':screen,'step':index,'serialized_public_input':p.serialized_input,
                         'public_sha256':digest(json.loads(p.serialized_input)),'legal_count':len(actions)})
            found.add(screen)
        if len(found)==2:
            raise Captured()
    try:
        try:
            execute_controlled_run(adapter,controller(CONFIG['source_driver_seed']),seed=seed,
                max_steps=CONFIG['max_steps'],action_space=ActionSpaceConfig.initial_no_potions(),before_decision=observe)
        except Captured:
            pass
        assert len(found)==2
    finally:
        adapter.close()
(root/'public-roots.json').write_text(json.dumps({'producer_commit':os.environ.get('ISSUE43_PRODUCER_COMMIT'),
    'all_hashes_match_original_canary':True,'rows':rows},indent=2,sort_keys=True)+'\n')
