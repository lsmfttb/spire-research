"""One bounded recovery of the fixed pilot; completed keys remain immutable.

Subdivide remaining keys into at most six branches/job before execution. This
changes scheduling only. All original roots/actions/drivers are retained and
failed-attempt CPU/wall are debited before the new full-remaining preflight.
"""
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from pilot import (PRIOR_CPU, PRIOR_WALL, PREP_RESERVE, FAMILY_MAX_CPU, SOURCE_MAX_CPU,
                   EXTENSION_SHA, branch_key, root_job, append, save, available, process_cpu)
from stage0 import CONFIG, digest


def batch(args):
    tasks,folder,deadline,previous=args
    return [root_job((meta,folder,deadline,bound,keys,previous)) for meta,keys,bound in tasks]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--scan',type=Path,required=True)
    parser.add_argument('--previous',type=Path,required=True)
    parser.add_argument('--artifacts',type=Path,required=True)
    args=parser.parse_args(); args.artifacts.mkdir(parents=True,exist_ok=False)
    scan=json.loads(args.scan.read_text()); roots=scan['roots']
    previous_events=[json.loads(s) for s in (args.previous/'pilot.jsonl').read_text().splitlines()]
    if scan['identity']['config_sha256']!=digest(CONFIG) or previous_events[0]['native_extension_sha256']!=EXTENSION_SHA:
        raise RuntimeError('resume identity mismatch')
    failure=previous_events[-1]
    if failure['boundary']!='failed':
        raise RuntimeError('this recovery requires a recorded failed attempt')
    expected={digest(k):k for r in roots for ordinal in range(r['legal_count'])
              for driver in CONFIG['continuation_seeds']['canary'] for k in [branch_key(r,ordinal,driver)]}
    recorded={}
    for path in args.previous.glob('owner*.jsonl'):
        for line in path.read_text().splitlines():
            event=json.loads(line)
            if event['boundary']=='branch_completed':
                recorded[digest(event['key'])]=event['result_sha256']
    completed={}
    for ident,key in expected.items():
        path=args.previous/(ident+'.json')
        if path.exists():
            row=json.loads(path.read_text())
            if row['key']!=key or not row['terminal'] or row['problems']:
                raise RuntimeError('invalid recovery result')
            if ident in recorded and hashlib.sha256(path.read_bytes()).hexdigest()!=recorded[ident]:
                raise RuntimeError('completed-key checksum mismatch')
            completed[ident]=row
    maxima={f:max(FAMILY_MAX_CPU[f],max(r['cpu'] for r in completed.values() if r['family']==f))
            for f in FAMILY_MAX_CPU}
    # Runtime-only partial observations price a repair, never authorize Stage1.
    # Debit every failed worker, including killed in-flight and source work;
    # 20s conservatively covers failure parent/launch CPU not in sampled workers.
    failure_cpu_bound=failure['observed_worker_cpu']+20.
    remaining_cpu=14400-PRIOR_CPU-PREP_RESERVE-scan['cpu']-failure_cpu_bound
    remaining_wall=3600-PRIOR_WALL-PREP_RESERVE-scan['wall']-failure['wall']
    tasks=[]
    for root in roots:
        pending=[k for k in expected.values() if k[0]==root['owner'] and k[1]==root['family'] and digest(k) not in completed]
        for offset in range(0,len(pending),6):
            chunk=pending[offset:offset+6]
            bound=(len(chunk)*maxima[root['family']]+SOURCE_MAX_CPU)*1.2
            tasks.append((root,chunk,bound))
    bins=[[] for _ in range(8)]; costs=[0.]*8
    for task in sorted(tasks,key=lambda t:(-t[2],t[0]['owner'],t[0]['family'],t[1][0][3])):
        i=min(range(8),key=lambda i:(costs[i],i)); bins[i].append(task); costs[i]+=task[2]
    preflight={'decision':'PILOT_RECOVERY_PREFLIGHT_PASS' if sum(costs)<=remaining_cpu and max(costs)+60<=remaining_wall else 'RESOURCE_BLOCKED',
        'completed_keys':len(completed),'remaining_keys':len(expected)-len(completed),'fixed_original_keys':len(expected),
        'cpu_bound':sum(costs),'wall_bound':max(costs)+60,'remaining_cpu':remaining_cpu,'remaining_wall':remaining_wall,
        'observed_family_max_cpu':maxima,'failed_attempt_cpu_bound':failure_cpu_bound,
        'failed_attempt_wall':failure['wall'],'retry_reserve_fraction':.20,'worker_cpu_bounds':costs,
        'producer_commit':os.environ['ISSUE43_PRODUCER_COMMIT'],'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'config_sha256':digest(CONFIG),'native_extension_sha256':EXTENSION_SHA}
    save(args.artifacts/'preflight.json',preflight)
    if preflight['decision']=='RESOURCE_BLOCKED':
        return
    start=time.monotonic(); cpu=time.process_time(); log=args.artifacts/'pilot.jsonl'; seen={}; results=[]
    append(log,{'boundary':'start',**preflight})
    pool=ProcessPoolExecutor(max_workers=8)
    pending={pool.submit(batch,(tasks,str(args.artifacts),start+remaining_wall,str(args.previous))) for tasks in bins}
    try:
        while pending:
            done,pending=wait(pending,timeout=1,return_when=FIRST_COMPLETED)
            for process in pool._processes.values():
                seen[process.pid]=max(seen.get(process.pid,0),process_cpu(process.pid))
            if sum(seen.values())+time.process_time()-cpu>remaining_cpu or time.monotonic()-start>remaining_wall or available()<4*1024**2:
                raise RuntimeError('RESOURCE_BLOCKED: recovery aggregate budget/memory')
            for future in done:
                for result in future.result():
                    results.append(result)
                    append(log,{'boundary':'chunk_completed','owner':result['root']['owner'],
                                'family':result['root']['family'],'job_cpu':result['job_cpu']})
    except BaseException as exc:
        append(log,{'boundary':'failed','message':str(exc),'observed_worker_cpu':sum(seen.values()),'wall':time.monotonic()-start})
        pool.terminate_workers(); raise
    else:
        pool.shutdown()
    new=[row for result in results for row in result['branches']]
    if len(new)+len(completed)!=len(expected) or {digest(r['key']) for r in new}|set(completed)!=set(expected):
        raise RuntimeError('incomplete fixed pilot')
    save(args.artifacts/'pilot.json',{'decision':'PILOT_COMPLETED_COST_ONLY','roots':roots,'sources':scan['sources'],
        'branches':list(completed.values())+new,'preflight':preflight,'worker_cpu':sum(seen.values()),
        'job_cpu':sum(r['job_cpu'] for r in results),'parent_cpu':time.process_time()-cpu,
        'wall':time.monotonic()-start,'scan_cpu':scan['cpu'],'scan_wall':scan['wall'],
        'failed_attempt_cpu_bound':failure_cpu_bound,'failed_attempt_wall':failure['wall'],
        'config_sha256':digest(CONFIG),'producer_commit':os.environ['ISSUE43_PRODUCER_COMMIT'],
        'native_extension_sha256':EXTENSION_SHA,'retry_policy':'98 exact completed keys reused; all 48 unfinished keys replayed, no invalid labels',
        'purpose':'ADMISSION_COST_ONLY'})


if __name__=='__main__':
    main()
