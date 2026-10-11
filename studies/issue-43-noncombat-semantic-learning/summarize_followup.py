"""Cost-only admission report, with owner-clustered sensitivity and lifecycle."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import random
from statistics import mean
from pilot import PRIOR_CPU, PRIOR_WALL, PREP_RESERVE
from stage0 import CONFIG,digest

ROOT_DRIVERS={'MAP_SCREEN':336,'REWARDS':560}
ORIGINAL_JOBS={'MAP_SCREEN':1176,'REWARDS':3360}
# Reserved before full pilot completion; extrapolation, not measured formal fit.
FIXED_CPU={'training_two_arms_two_seeds_100_updates':1000.,
           'checkpoint_and_readiness':240.,'launch_and_serialization':120.}


def describe(roots, branches, source_mean):
    summaries=[]
    for i,root in enumerate(roots):
        rows=[b for b in branches if b['root']==i]
        summaries.append({'owner':root['owner'],'family':root['family'],'legal_count':root['legal_count'],
                          'observed_branches':len(rows),'mean_branch_cpu':mean(b['cpu'] for b in rows),
                          'all_legal_one_driver_cpu':root['legal_count']*mean(b['cpu'] for b in rows)})
    family_means={f:mean(b['cpu'] for b in branches if roots[b['root']]['family']==f) for f in ROOT_DRIVERS}
    weighted=sum(ROOT_DRIVERS[f]*mean(r['all_legal_one_driver_cpu'] for r in summaries if r['family']==f)
                 for f in ROOT_DRIVERS)
    return {'family_mean_cpu':family_means,'root_summaries':summaries,
            'same_original_formula_label_cpu':sum(ORIGINAL_JOBS[f]*family_means[f] for f in ROOT_DRIVERS),
            'same_original_formula_plus_240_sources':sum(ORIGINAL_JOBS[f]*family_means[f] for f in ROOT_DRIVERS)+240*source_mean,
            'all_action_count_weighted_label_cpu':weighted,
            'all_action_count_weighted_plus_240_sources':weighted+240*source_mean,
            'source_mean_cpu':source_mean,'owners':len({r['owner'] for r in roots})}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--artifacts',type=Path,required=True)
    parser.add_argument('--reviewer',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    original=json.loads(Path(__file__).with_name('admission.json').read_text())
    old_roots=[{'owner':r['seed'],'family':r['screen'],'legal_count':r['actions']} for r in original['canary']['roots']]
    old_branches=[{'root':b['key'][0],'cpu':b['branch_cpu']} for b in original['canary']['branches']]
    replay=[json.loads(line) for line in (args.reviewer/'independent-canary/stage0.jsonl').read_text().splitlines()]
    replay_branches=[{'root':b['key'][0],'cpu':b['branch_cpu']} for b in replay if b['boundary']=='branch_completed']
    replay_source_mean=json.loads((args.reviewer/'independent-canary-comparison.json').read_text())['mean_source_cpu']
    pilot=json.loads((args.artifacts/'pilot02/pilot.json').read_text())
    roots=pilot['roots']; index={(r['owner'],r['family']):i for i,r in enumerate(roots)}
    branches=[{'root':index[(b['owner'],b['family'])],'cpu':b['cpu']} for b in pilot['branches']]
    if len(branches)!=146 or any(not b['terminal'] or b['problems'] for b in pilot['branches']):
        raise ValueError('fixed pilot incomplete; no partial admission projection')
    source_mean=mean(s['cpu'] for s in pilot['sources'])
    comparisons={'original_builder':describe(old_roots,old_branches,original['projection']['mean_source_cpu']),
                 'independent_reviewer':describe(old_roots,replay_branches,replay_source_mean),
                 'independent_owner_pilot':describe(roots,branches,source_mean)}
    owner_roots=defaultdict(list)
    for row in comparisons['independent_owner_pilot']['root_summaries']:
        owner_roots[row['owner']].append(row)
    source_by_owner={s['seed']:s['cpu'] for s in pilot['sources']}
    owners=sorted(owner_roots); rng=random.Random(434091); sensitivity=[]
    for _ in range(10000):
        sampled=rng.choices(owners,k=len(owners))
        rows=[r for owner in sampled for r in owner_roots[owner]]
        labels=sum(ROOT_DRIVERS[f]*mean(r['all_legal_one_driver_cpu'] for r in rows if r['family']==f)
                   for f in ROOT_DRIVERS)
        source_cost=mean(source_by_owner[owner] for owner in sampled)
        # Full-range 1024 source scan sensitivity, then worst 384 distinct source
        # owners regenerated for label workers. Neither outcome selects roots.
        sensitivity.append(labels+(1024+384)*source_cost)
    sensitivity.sort(); p90=sensitivity[9000]
    central_labels=comparisons['independent_owner_pilot']['all_action_count_weighted_label_cpu']
    fixed=sum(FIXED_CPU.values())
    central_base=central_labels+(240+240)*source_mean+fixed
    conservative_base=p90+fixed
    central=central_base*1.2; conservative=conservative_base*1.2
    largest=max(b['cpu'] for b in pilot['branches'])
    # Include serial overhead rather than pretending all preparation divides by8.
    conservative_wall=conservative/8+fixed*7/8+6*largest*1.2+120
    max_rss=max(b['peak_rss_kib'] for b in pilot['branches'])
    stage0_cpu=PRIOR_CPU+PREP_RESERVE+pilot['scan_cpu']+pilot['failed_attempt_cpu_bound']+max(pilot['worker_cpu'],pilot['job_cpu'])+pilot['parent_cpu']
    stage0_wall=PRIOR_WALL+PREP_RESERVE+pilot['scan_wall']+pilot['failed_attempt_wall']+pilot['wall']
    if stage0_cpu>14400 or stage0_wall>3600:
        raise ValueError('Stage0 combined execution cap exceeded')
    decision='RESOURCE_BLOCKED' if conservative>72000 or conservative_wall>18000 or max_rss>8*1024**2 else 'ADMISSION_READY_FOR_INDEPENDENT_REVIEW'
    manifest=[{'path':str(path.relative_to(args.artifacts)),'bytes':path.stat().st_size,
               'sha256':hashlib.sha256(path.read_bytes()).hexdigest()} for path in sorted(args.artifacts.rglob('*')) if path.is_file()]
    report={'decision':decision,'purpose':'STUDY_ONLY_ADMISSION_READINESS','config_sha256':digest(CONFIG),
        'config_git_blob':'12a1119933beecb72e52922444b387c9d9eaa0c2',
        'native_commit':CONFIG['native_commit'],'frozen_stsrl_commit':CONFIG['frozen_stsrl_commit'],
        'native_extension_sha256':pilot['native_extension_sha256'],
        'pilot':{'roots':16,'family_roots':{'MAP_SCREEN':8,'REWARDS':8},'branches':len(branches),
                 'source_seeds':[s['seed'] for s in pilot['sources']],'distinct_owners':owners,
                 'shared_family_owners':owners,'map_only_selected_owners':[],
                 'eligibility':'first qualifying family/owner, ascending seeds; MAP-only owners eligible',
                 'legal_counts':[{k:r[k] for k in ('owner','family','step','legal_count','public_input_sha256')} for r in roots],
                 'successful_branch_cpu':sum(b['cpu'] for b in branches),'failed_attempt_cpu_bound':pilot['failed_attempt_cpu_bound'],
                 'scan_cpu':pilot['scan_cpu'],'scan_wall':pilot['scan_wall'],
                 'recovery_cpu':max(pilot['worker_cpu'],pilot['job_cpu'])+pilot['parent_cpu'],
                 'recovery_wall':pilot['wall'],'peak_job_rss_kib':max_rss,
                 'eight_worker_sum_peak_rss_upper_kib':8*max_rss,
                 'retry_completed_keys_reused':98,'unfinished_keys_replayed':48,
                 'failed_attempt_reason':'per-root CPU guard; invalid branch excluded, completed keys recovered'},
        'comparison':comparisons,
        'stage1_projection':{'root_driver_weights':ROOT_DRIVERS,'same_formula_jobs':ORIGINAL_JOBS,
            'source_scan_central_runs':240,'source_regeneration_central_owners':240,
            'source_scan_conservative_runs':1024,'source_regeneration_conservative_owners':384,
            'central_labels_cpu':central_labels,'central_sources_and_regeneration_cpu':480*source_mean,
            'fixed_cpu_reserves':FIXED_CPU,'owner_bootstrap_seed':434091,'replicates':10000,
            'owner_bootstrap90_labels_sources_regeneration_cpu':p90,
            'central_before_uncertainty_cpu':central_base,'central_uncertainty_retry_reserve_cpu':central_base*.2,
            'central_inclusive_cpu':central,'conservative_before_uncertainty_cpu':conservative_base,
            'conservative_uncertainty_retry_reserve_cpu':conservative_base*.2,
            'conservative_inclusive_cpu':conservative,'conservative_inclusive_wall':conservative_wall,
            'cpu_cap':72000,'wall_cap':18000,'meaning':'Small-owner sensitivity, not certified confidence bound or runtime guarantee.'},
        'stage0_combined':{'prior_builder_build_cpu_bound':1089.3609968,'prior_builder_study_cpu':1084.41165868,
            'prior_builder_regression_cpu_reserve':100,'independent_reviewer_cpu':233.434184638,
            'independent_reviewer_wall':235.406515972,'new_preparation_cpu_wall_reserve':PREP_RESERVE,
            'combined_cpu_bound':stage0_cpu,'combined_wall_bound':stage0_wall,'cpu_cap':14400,'wall_cap':3600,
            'method':'Disjoint attempt costs; completed branch timings are not added again to failed/recovery worker totals.'},
        'readiness':{'unit_tests':19,'synthetic_three_arm_admission_runs':3,
                     'formal_targets_collected':0,'formal_models_fitted':0,'fresh433_runs':0,
                     'scope':'See READINESS.md for tested primitives and explicitly remaining formal launcher/report gates.'},
        'artifact_root':str(args.artifacts),'artifact_consumer':'Issue43 independent Reviewer',
        'review_or_delete_date':'2026-10-18','artifact_manifest':manifest,
        'limitations':['Eight shared family owners; no selected MAP-only owner happened to occur.',
                       'Original/Reviewer pricing sampled only first two ordinals; new pilot samples every legal ordinal.',
                       'CPU varies with source/action and runtime conditions; causes of original/replay timing difference remain unresolved.',
                       'Training/launch/checkpoint prices are explicit reserves, not measured full-cohort costs.',
                       'Formal launcher/report wiring remains an independent readiness gate; no scientific gain claim.',
                       'Privileged Battle teacher; not a normal-public noSL whole-run result.']}
    args.output.write_text(json.dumps(report,sort_keys=True,indent=2)+'\n')
    print(json.dumps({'decision':decision,'pilot_branches':len(branches),'stage0_cpu_bound':stage0_cpu,
                      'stage0_wall_bound':stage0_wall,'central_inclusive_cpu':central,
                      'conservative_inclusive_cpu':conservative,'conservative_wall':conservative_wall},indent=2))


if __name__=='__main__':
    main()
