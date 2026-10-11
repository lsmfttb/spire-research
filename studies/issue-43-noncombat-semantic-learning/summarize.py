"""Reproduce resource admission from immutable cost-only canary artifacts."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--artifacts',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    root=args.artifacts
    rows=[json.loads(line) for line in (root/'stage0-attempt03/stage0.jsonl').read_text().splitlines()]
    branches=[row for row in rows if row['boundary']=='branch_completed']
    assert len(branches)==16 and all(row['terminal'] and not row['problems'] for row in branches)
    roots=[row for row in rows if row['boundary']=='root_captured']
    assert [row['screen'] for row in roots]==['MAP_SCREEN','REWARDS','MAP_SCREEN','REWARDS']
    means={family:statistics.mean(row['branch_cpu'] for row in branches if roots[row['key'][0]]['screen']==family)
           for family in ('MAP_SCREEN','REWARDS')}
    options={family:statistics.mean(row['actions'] for row in roots if row['screen']==family) for family in means}
    # Fixed train/val two seeds and heldout four, with observed family action counts.
    jobs={'MAP_SCREEN':(96*2+24*2+24*4)*options['MAP_SCREEN'],
          'REWARDS':(160*2+40*2+40*4)*options['REWARDS']}
    continuation_cpu=sum(jobs[family]*means[family] for family in means)
    source_rows=[row for row in rows if row['boundary']=='source_completed']
    source_mean=statistics.mean([source_rows[0]['cpu_seconds']-rows[1]['cpu_seconds'],
        source_rows[1]['cpu_seconds']-next(row['cpu_seconds'] for row in rows if row['boundary']=='source_started' and row['seed']==430002)])
    source_projection_minimum=source_mean*(160+40+40)
    modes=[json.loads((root/f'concurrency{workers}.json').read_text()) for workers in (4,8)]
    assert all((a['key'],a['floor'],a['outcome'],a['steps'],a['privileged_checkpoint_fingerprint_sha256'])==
               (b['key'],b['floor'],b['outcome'],b['steps'],b['privileged_checkpoint_fingerprint_sha256'])
               for a,b in zip(modes[0]['rows'],modes[1]['rows']))
    smoke=[json.loads(line) for line in (root/'stage0-attempt05/stage0.jsonl').read_text().splitlines()]
    trained=[row for row in smoke if row['boundary']=='trained_public_choice_executed']
    current_roots=json.loads((root/'public-roots.json').read_text())
    assert len(trained)==8 and all(row['gradient_min']>0 and max(row['scores'])>min(row['scores']) for row in trained)
    files=[path for path in root.rglob('*') if path.is_file() and path.suffix in ('.json','.jsonl')]
    report={
        'status':'RESOURCE_BLOCKED','disposition':'STUDY_ONLY','scientific_claim':'none',
        'formal_targets_collected':0,'formal_models_fitted':0,'fresh_runs':0,
        'native_commit':rows[0]['native_commit'],'native_extension_sha256':rows[0]['native_extension_sha256'],
        'frozen_stsrl_commit':rows[0]['frozen_stsrl_commit'],'config_sha256':rows[0]['config_sha256'],
        'canary':{'roots':roots,'current_roots_with_public_inputs':current_roots,'branches':branches,'wall':rows[-1]['wall_seconds'],
                  'cpu':rows[-1]['cpu_seconds'],'peak_rss_kib':rows[-1]['peak_rss_kib'],
                  'current_code_smoke':trained,'current_code_resume_start':smoke[0]},
        'concurrency':[{'workers':mode['workers'],'wall':mode['wall'],
            'aggregate_cpu':mode['aggregate_worker_cpu'],
            'peak_worker_rss_kib':max(row['peak_rss_kib'] for row in mode['rows']),
            'sum_worker_peak_rss_kib_upper_bound':sum(row['peak_rss_kib'] for row in mode['rows']),
            'available_memory_kib_before':mode['available_memory_kib_before']} for mode in modes],
        'concurrency_checkpoint_terminal_identity_equal':True,
        'projection':{'mean_cpu_per_branch':means,'mean_legal_actions':options,
            'root_action_seed_jobs':jobs,'continuation_cpu_seconds':continuation_cpu,
            'minimum_source_runs_at_observed_occupancy':240,'mean_source_cpu':source_mean,
            'minimum_source_cpu_seconds':source_projection_minimum,
            'stage1_cpu_excluding_training_and_retries':continuation_cpu+source_projection_minimum,
            'stage1_cpu_cap':72000,'stage2_384_run_source_baseline_cpu':384*source_mean,
            'limitations':'Four roots/two source owners, first two actions only; uncertain extrapolation, not a bound. Fresh learned trajectories may differ. No outcome-selected replacement. Eight-worker timing is admission-only repeated first eight keys.'},
        'limitations':['No formal source cohort, target table, trained scientific checkpoints, heldout Q estimates or 128x3 comparison.',
            'Early launcher/code smoke attempts failed and are retained; original cost attempt did not record a producer commit. Final public smoke/replay and concurrency have committed source and exact native/config identity.',
            'Compilation CPU was not directly timed; admission accounting must disclose a conservative upper bound instead of inventing measured CPU.'],
        'next_scientific_choice':'Planner may amend Stage1 aggregate CPU cap to 144000 seconds while retaining all sample/teacher/information contracts, or redesign the causal study budget explicitly; this handoff does not authorize either change.',
        'artifact_consumer':'Issue43 independent Reviewer','review_or_delete_date':'2026-10-18',
        'native_checks':{'public_choice_regression':'PASS, exact 7fef4e19 including 9 Sapphire and indexed-gold fixtures; adapter PYTHONPATH required',
            'indexed_gold_regression':'PASS','adapter_and_student_tests':13,
            'loaded_native_source':'Release module ced9941e; natural MAP/REWARDS callbacks and restore/step executed',
            'limitation':'Constructed Sapphire/multi-gold fixtures execute in same-source embedded binding regression, not through Python StepSimulator mutation API (which exposes no reward-container mutator).'},
        'read_only_source_blobs':{'action_space.py':'817db4e621b0a12e15f29c24e1dc3378da957bfc',
            'controlled_run.py':'3ad3b57bb6fd433e5cb9ef7d41245d9ba75b4fd8',
            'lightspeed.py':'8a95863fe26506b55ee43bca4196c1af73abc888',
            'non_combat_policy.py':'8079fe274a49a39201e8270a2ddc2a7d2b618568',
            'oracle_search.py':'32926eb70e7c9648046851c5616bea4ff1e8e793'},
        'stage0_budget_accounting':{'build_start_utc':'2026-10-11T09:15:53.6334588Z',
            'last_native_target_link_utc':'2026-10-11T09:20:25.973708Z',
            'build_wall_upper_bound_seconds':272.3402492,
            'build_cpu_upper_bound_seconds':1089.3609968,
            'build_bound_method':'Four compiler workers times complete launcher-write to last-target-link interval; not measured CPU.',
            'measured_study_cpu_seconds':sum(json.loads(path.read_text().splitlines()[-1])['cpu_seconds'] for path in root.glob('stage0-attempt*/stage0.jsonl'))+sum(mode['aggregate_worker_cpu'] for mode in modes),
            'measured_study_wall_seconds':sum(json.loads(path.read_text().splitlines()[-1])['wall_seconds'] for path in root.glob('stage0-attempt*/stage0.jsonl'))+sum(mode['wall'] for mode in modes),
            'native_launcher_and_unit_smoke_unmeasured_cpu_reserve_seconds':100,
            'cap_cpu_seconds':14400,'cap_wall_seconds':3600},
        'artifact_manifest':[{'path':str(path.relative_to(root)),'bytes':path.stat().st_size,
            'sha256':hashlib.sha256(path.read_bytes()).hexdigest()} for path in sorted(files)]}
    args.output.write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')


if __name__=='__main__':
    main()
