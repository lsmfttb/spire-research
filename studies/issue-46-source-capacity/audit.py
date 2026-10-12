"""Builder verification of all recorded source, all-action labels and640 fresh paths."""
import gzip
import json
from pathlib import Path
import sys
import time
from collections import Counter
from common import CONFIG,sha,write,owner_plan
from model import load

STORE=Path(sys.argv[1]); START=time.process_time(); COUNTS=Counter()

def pathcheck(directory,name,result,search=True,assistance=None,checkpoint=None):
    previous=None; last=None; used=Counter(); transforms={d['step_index'] for d in assistance or [] if d['actual_change'].get('native_rebuild_called')}
    with gzip.open(directory/(name+'.jsonl.gz'),'rt') as stream:
        for index,line in enumerate(stream):
            row=json.loads(line); raw=row['snapshot_raw']; nxt=row['next_snapshot_raw']
            assert row['step_index']==index
            if previous is None:
                assert raw==result['initial_raw'] or index in transforms
            else: assert raw==previous or index in transforms
            assert 0<=row['chosen_action_index']<len(row['legal_action_identities'])
            assert row['chosen_action_index'] in row['eligible_action_indices']
            assert row['chosen_action_identity']==row['legal_action_identities'][row['chosen_action_index']]
            if row['battle_active'] and search:
                reports=row['decision_metadata']['oracle_search_decision_reports']; assert len(reports)==1
                report=reports[0]; telemetry=report['decision_telemetry']
                assert report['include_potions'] and not report['problems'] and report['model_calls']==0
                assert telemetry['root_visits']==400 and telemetry['unmapped_search_edge_count']==0 and telemetry['unmapped_root_row_count']==0
                assert report['native_api']=='StepSimulator.battle_search_v2.v1' and report['patch_identity']=='sts_lightspeed_battle_search_v2_tree_internal_v1'
                eligible=[r for r in report['root_actions'] if r['eligible'] and r['visits']>0]
                chosen=next(r for r in eligible if r['legal_action_index']==row['chosen_action_index'])
                assert abs(chosen['mean_value']-max(r['mean_value'] for r in eligible))<1e-12
                COUNTS['battle_search_decisions']+=1
            if checkpoint is not None and not row['battle_active']:
                supported=row['screen_state'] in ('MAP_SCREEN','REWARDS')
                if supported:
                    assert row['route']=='learned' and row['serialized_public_input']
                    assert checkpoint.choose(row['serialized_public_input'],'visible')==row['chosen_action_index']
                    public=json.loads(row['serialized_public_input'])
                    assert len(public['legal_options'])==len(row['legal_action_identities'])
                    assert [x['candidate_index'] for x in public['legal_options']]==list(range(len(public['legal_options'])))
                    assert 'assisted' not in public['public_state'] and 'source_seed' not in public['public_state']
                    used[row['screen_state']]+=1
                else: assert row['route']=='explicit_unsupported_expert'
            COUNTS['transitions']+=1; previous=nxt; last=row
    assert last is not None and last['terminal_after_step'] and last['next_snapshot_raw']==result['final_raw']
    assert result['terminal'] and not result['problems'] and result['outcome'] in ('PLAYER_WIN','PLAYER_LOSS')
    assert result.get('terminal_floor',result.get('floor'))==result['final_raw']['floor_num']
    return used

def main():
    roots=set(); splitowners={s:set() for s in ('train','validation','heldout')}
    for plan in owner_plan():
        directory=STORE/str(plan['owner']); root=json.loads((directory/'root.json').read_text()); table=json.loads((directory/'table.json').read_text())
        assert root['owner']==plan['owner'] and root['split']==plan['split'] and root['assisted']==plan['assisted']
        assert root['root_id'] not in roots; roots.add(root['root_id']); splitowners[plan['split']].add(plan['owner'])
        assert root==table['root'] and root['public_sha256']==sha(json.loads(root['serialized']))
        source=json.loads((directory/'source.json').read_text()); assistance=json.loads((directory/'assistance.json').read_text())
        if not plan['assisted']: assert not assistance
        pathcheck(directory,'source',source,not plan['old45'],assistance)
        for ordinal in range(root['legal_count']):
            values=[]
            for seed in CONFIG['continuation_driver_seeds']:
                name=f'label-{ordinal}-{seed}'; result=json.loads((directory/(name+'.json')).read_text())
                assert result['root_id']==root['root_id'] and result['ordinal']==ordinal and result['driver_seed']==seed and not result['new_assistance']
                assert result['forced_pre_raw']==root['root_raw'] and result['forced_action_identity']==root['candidate_sha256'][ordinal]
                assert result['forced_post_raw']==result['initial_raw']
                pathcheck(directory,name,result)
                values.append(result['terminal_floor']); COUNTS['label_branches']+=1
            assert table['q'][ordinal]==sum(values)/2
        assert table['branch_count']==root['legal_count']*2
        COUNTS['source_owners']+=1
    assert len(splitowners['train'])==384 and len(splitowners['validation'])==64 and len(splitowners['heldout'])==64
    assert not any(splitowners[a]&splitowners[b] for a,b in [('train','validation'),('train','heldout'),('validation','heldout')])
    checkpoints=json.loads((STORE/'training/checkpoints.json').read_text()); models={cell:load(cp) for cell,cp in checkpoints.items()}
    for seed in range(441001,441129):
        initial=None
        for arm in ('SxT','SxB','LxT','LxB','Expert'):
            name=f'{seed}-{arm}'; result=json.loads((STORE/'fresh'/(name+'.json')).read_text())
            assert result['seed']==seed and result['arm']==arm
            if initial is None: initial=result['initial_raw']
            else: assert result['initial_raw']==initial
            used=pathcheck(STORE/'fresh',name,result,checkpoint=None if arm=='Expert' else models[arm])
            if arm!='Expert':
                assert result['checkpoint_sha256']==checkpoints[arm]['sha256']
                for family in ('MAP_SCREEN','REWARDS'): assert used[family]==result['learner_uses'].get(family,0)
            COUNTS['fresh_runs']+=1
    write(STORE/'audit.json',{'passed':True,'counts':dict(COUNTS),'split_owner_counts':{k:len(v) for k,v in splitowners.items()},'all_action_labels_recomputed':True,'all_learned_choices_recomputed_from_frozen_public_input':True,'supported_silent_fallbacks':0,'cpu':time.process_time()-START})

if __name__=='__main__': main()
