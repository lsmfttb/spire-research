"""Issue43 admission only: fixed 434xxx roots, all actions, no learning labels.

Opaque checkpoints never leave their creating process. Outputs are cost evidence
for Reviewer; this executable cannot address any formal or fresh source seed.
"""
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import time

from stage0 import CONFIG, Restored, controller, prepared, digest
from adapter import resolve_native_action
from sts_combat_rl.sim.lightspeed import LightSpeedAdapter
from sts_combat_rl.sim.action_space import ActionSpaceConfig
from sts_combat_rl.sim.controlled_run import execute_controlled_run
import slaythespire

FAMILIES = ('MAP_SCREEN', 'REWARDS')
# Prior attempts, native build upper bound, reserved unmeasured regression work,
# and independent Reviewer replay are mutually exclusive ledger components.
PRIOR_CPU = 1089.3609968 + 1084.41165868 + 100 + 233.434184638
PRIOR_WALL = 272.3402492 + 701.411470218 + 100 + 235.406515972
PREP_RESERVE = 120.0  # this follow-up's hashes, synthetic checks and summaries
FAMILY_MAX_CPU = {'MAP_SCREEN': 92.554332244, 'REWARDS': 20.61544407}
SOURCE_MAX_CPU = 40.0  # conservative preparation bound, enforced cumulatively
RETRY_FRACTION = .20
EXTENSION_SHA = 'ced9941eb1cd042630b27ed3b9b0be700cd8cbcc143156d52bdabf7bf07234cd'


class Captured(Exception):
    pass


def append(path, row):
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def save(path, value):
    """Atomic evidence publication; existing immutable results cannot be replaced."""
    if path.exists():
        raise RuntimeError('immutable output exists: ' + str(path))
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def available():
    return int(next(s.split()[1] for s in Path('/proc/meminfo').read_text().splitlines()
                    if s.startswith('MemAvailable:')))


def source(seed, wanted, log, deadline, cpu_limit, stop_family=None):
    if not 434001 <= seed <= 434064:
        raise ValueError('admission source seed outside fixed pilot range')
    begin = time.monotonic(); cpu = time.process_time()
    adapter = LightSpeedAdapter(seed=seed, ascension=20, module=slaythespire)
    roots = {}
    def observe(snapshot, actions, context, index):
        if time.monotonic() >= deadline or time.process_time()-cpu >= cpu_limit:
            raise Captured('RESOURCE_BLOCKED: source budget')
        family = context.screen_state
        if family in FAMILIES:
            p = prepared(adapter)  # malformed supported inputs fail closed
            if family in wanted and family not in roots and len(actions) >= 2:
                cp = adapter.capture_checkpoint(snapshot)
                fingerprint = adapter.checkpoint_fingerprint(snapshot)
                value = json.loads(p.serialized_input)
                meta = {'owner': seed, 'family': family, 'step': index,
                        'legal_count': len(actions), 'public_input_sha256': digest(value),
                        'serialized_public_input': p.serialized_input,
                        'privileged_checkpoint_fingerprint_sha256': digest(fingerprint),
                        'candidate_sha256': [digest(option) for option in value['legal_options']]}
                roots[family] = (meta, cp, p, fingerprint)
                append(log, {'boundary': 'root_captured', **meta})
                if family == stop_family:
                    raise Captured()
    append(log, {'boundary': 'source_started', 'seed': seed})
    try:
        try:
            run = execute_controlled_run(adapter, controller(CONFIG['source_driver_seed']), seed=seed,
                max_steps=CONFIG['max_steps'], action_space=ActionSpaceConfig.initial_no_potions(),
                before_decision=observe)
        except Captured as exc:
            if exc.args:
                raise RuntimeError(str(exc)) from exc
            return adapter, roots, None
        row = {'boundary': 'source_completed', 'seed': seed, 'terminal': run.terminal,
               'problems': list(run.problems), 'steps': len(run.steps),
               'cpu': time.process_time()-cpu, 'wall': time.monotonic()-begin,
               'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
               'captured_families': list(roots)}
        append(log, row)
        if not run.terminal or run.problems:
            raise RuntimeError('INVALID_SOURCE: ' + str(run.problems))
        return adapter, roots, row
    except BaseException:
        adapter.close()
        raise


def branch_key(meta, ordinal, driver):
    return [meta['owner'], meta['family'], meta['step'], ordinal, driver,
            meta['candidate_sha256'][ordinal], meta['public_input_sha256'], digest(CONFIG), EXTENSION_SHA]


def source_job(args):
    seed, directory, deadline = args
    resource.setrlimit(resource.RLIMIT_AS, (8*1024**3, 8*1024**3))
    adapter, found, row = source(seed, FAMILIES, Path(directory)/('scan-%d.jsonl' % seed),
                                 deadline, SOURCE_MAX_CPU*1.2)
    adapter.close()
    return [found[f][0] for f in FAMILIES if f in found], row


def root_job(args):
    meta, directory, deadline, cpu_limit = args
    resource.setrlimit(resource.RLIMIT_AS, (8*1024**3, 8*1024**3))
    folder = Path(directory); log = folder / ('owner%d-%s.jsonl' % (meta['owner'], meta['family']))
    started = time.monotonic(); cpu = time.process_time()
    adapter, roots, _ = source(meta['owner'], [meta['family']], log, deadline, cpu_limit,
                               stop_family=meta['family'])
    try:
        current_meta, cp, p, fingerprint = roots[meta['family']]
        if current_meta != meta:
            raise RuntimeError('regenerated source/root provenance changed')
        rows = []
        for ordinal in range(meta['legal_count']):
            for driver in CONFIG['continuation_seeds']['canary']:
                key = branch_key(meta, ordinal, driver)
                result_path = folder / (digest(key) + '.json')
                if result_path.exists():
                    previous = json.loads(result_path.read_text())
                    if previous['key'] != key or not previous['terminal'] or previous['problems']:
                        raise RuntimeError('invalid completed branch recovery')
                    rows.append(previous)
                    append(log, {'boundary': 'completed_key_recovered', 'key': key,
                                 'result_sha256': hashlib.sha256(result_path.read_bytes()).hexdigest()})
                    continue
                append(log, {'boundary': 'branch_started', 'key': key})
                branch_start = time.monotonic(); branch_cpu = time.process_time()
                restored = adapter.restore_checkpoint(cp)
                if adapter.checkpoint_fingerprint(restored) != fingerprint:
                    raise RuntimeError('hidden checkpoint fidelity mismatch')
                chosen = resolve_native_action(p, ordinal, dict(adapter._sim.public_projection_v3()),
                                               list(adapter._sim.legal_actions()))
                actions = adapter.legal_actions(restored)
                if actions[ordinal].raw['native'].bits != chosen.bits:
                    raise RuntimeError('fresh native legal identity mismatch')
                transition = adapter.step(actions[ordinal])
                post = adapter.capture_checkpoint(transition.snapshot)
                def progress(snapshot, actions, context, index):
                    if time.monotonic() >= deadline or time.process_time()-cpu >= cpu_limit:
                        raise RuntimeError('RESOURCE_BLOCKED: root job budget')
                    if index % 25 == 0:
                        append(log, {'boundary': 'branch_progress', 'key': key, 'step': index})
                run = execute_controlled_run(Restored(adapter, post), controller(driver), seed=meta['owner'],
                    max_steps=CONFIG['max_steps'], action_space=ActionSpaceConfig.initial_no_potions(),
                    before_decision=progress)
                row = {'key': key, 'owner': meta['owner'], 'family': meta['family'],
                       'original_legal_count': meta['legal_count'], 'ordinal': ordinal, 'driver': driver,
                       'terminal': run.terminal, 'problems': list(run.problems), 'steps': len(run.steps),
                       'wall': time.monotonic()-branch_start, 'cpu': time.process_time()-branch_cpu,
                       'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                       'retries': 0, 'purpose': 'ADMISSION_COST_ONLY'}
                if not row['terminal'] or row['problems']:
                    append(log, {'boundary': 'branch_invalid', **row})
                    raise RuntimeError('INVALID_CONTINUATION: ' + str(run.problems))
                save(result_path, row)
                append(log, {'boundary': 'branch_completed', 'key': key,
                             'result_sha256': hashlib.sha256(result_path.read_bytes()).hexdigest()})
                rows.append(row)
        return {'root': meta, 'branches': rows, 'job_cpu': time.process_time()-cpu,
                'job_wall': time.monotonic()-started, 'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
    finally:
        adapter.close()


def process_cpu(pid):
    try:
        fields = Path('/proc/%d/stat' % pid).read_text().split(') ', 1)[1].split()
        return (int(fields[11])+int(fields[12])) / os.sysconf('SC_CLK_TCK')
    except FileNotFoundError:
        return 0.


def schedule(roots):
    bins = [[] for _ in range(8)]
    costs = [0.] * 8
    def bound(root):
        return (root['legal_count']*2*FAMILY_MAX_CPU[root['family']]+SOURCE_MAX_CPU)*1.2
    for root in sorted(roots, key=lambda r: (-bound(r), r['owner'], r['family'])):
        worker = min(range(8), key=lambda i: (costs[i], i))
        bins[worker].append(root); costs[worker] += bound(root)
    return bins, costs


def batch_job(args):
    batch, directory, deadline = args
    return [root_job((r, directory, deadline,
             (r['legal_count']*2*FAMILY_MAX_CPU[r['family']]+SOURCE_MAX_CPU)*1.2)) for r in batch]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--scan-only', action='store_true')
    parser.add_argument('--roots', type=Path)
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    log = args.artifacts / 'pilot.jsonl'
    if log.exists():
        raise RuntimeError('new attempt directory required')
    start = time.monotonic(); cpu = time.process_time()
    extension_sha = hashlib.sha256(Path(slaythespire.__file__).read_bytes()).hexdigest()
    if extension_sha != EXTENSION_SHA:
        raise RuntimeError('loaded native extension identity changed')
    identity = {'producer_commit': os.environ['ISSUE43_PRODUCER_COMMIT'],
                'config_sha256': digest(CONFIG), 'native_extension_sha256': extension_sha,
                'native_commit': CONFIG['native_commit'], 'frozen_stsrl_commit': CONFIG['frozen_stsrl_commit'],
                'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'prior_cpu_bound': PRIOR_CPU, 'prior_wall_bound': PRIOR_WALL,
                'prep_reserve_cpu_and_wall': PREP_RESERVE, 'purpose': 'ADMISSION_COST_ONLY'}
    append(log, {'boundary': 'start', **identity})
    remaining_wall = 3600 - PRIOR_WALL - PREP_RESERVE
    remaining_cpu = 14400 - PRIOR_CPU - PREP_RESERVE
    roots = []; sources = []
    if args.roots:
        scan = json.loads(args.roots.read_text())
        if scan['identity']['config_sha256'] != digest(CONFIG) or scan['identity']['native_extension_sha256'] != extension_sha:
            raise RuntimeError('scan identity mismatch')
        roots, sources = scan['roots'], scan['sources']
        remaining_wall -= scan['wall']; remaining_cpu -= scan['cpu']
    else:
        # Preflight the complete 64-source scan before any source execution.
        scan_cpu_bound = 64 * SOURCE_MAX_CPU * (1+RETRY_FRACTION)
        if scan_cpu_bound > remaining_cpu or scan_cpu_bound/8+SOURCE_MAX_CPU*1.2 > remaining_wall:
            save(args.artifacts/'decision.json', {'decision': 'RESOURCE_BLOCKED', 'scan_cpu_bound': scan_cpu_bound})
            return
        with ProcessPoolExecutor(max_workers=8) as pool:
            for first in range(434001, 434065, 8):
                # Dispatch whole ascending blocks. Completion timing does not
                # select roots; any unused tail owners still count as source cost.
                for found, row in pool.map(source_job, [(s,str(args.artifacts),start+remaining_wall)
                                                        for s in range(first,first+8)]):
                    sources.append(row)
                    for meta in found:
                        if sum(r['family']==meta['family'] for r in roots)<8:
                            roots.append(meta)
                if all(sum(r['family']==family for r in roots)==8 for family in FAMILIES):
                    break
        scan = {'identity': identity, 'roots': roots, 'sources': sources,
                'cpu': time.process_time()-cpu+sum(s['cpu'] for s in sources), 'wall': time.monotonic()-start}
        save(args.artifacts/'roots.json', scan)
        if not all(sum(r['family']==family for r in roots)==8 for family in FAMILIES):
            save(args.artifacts/'decision.json', {'decision': 'ADMISSION_COVERAGE_INSUFFICIENT'})
            return
        remaining_cpu -= scan['cpu']; remaining_wall -= scan['wall']
    jobs = {family: sum(r['legal_count']*2 for r in roots if r['family']==family) for family in FAMILIES}
    continuation_bound = sum(jobs[f]*FAMILY_MAX_CPU[f] for f in FAMILIES)
    # Source-prefix regeneration charged at full source costs for every root.
    cpu_bound = (continuation_bound + len(roots)*SOURCE_MAX_CPU) * (1+RETRY_FRACTION)
    # Fixed worker bins, selected using only original legal counts and PRIOR
    # family maximum costs. Each bin runs sequentially in one process, so its
    # sum (plus explicit 60s startup/serialization reserve) bounds that schedule.
    bins, bin_costs = schedule(roots)
    wall_bound = max(bin_costs) + 60
    preflight = {'jobs': jobs, 'family_observed_max_cpu': FAMILY_MAX_CPU,
                 'cpu_bound': cpu_bound, 'wall_bound': wall_bound,
                 'remaining_cpu': remaining_cpu, 'remaining_wall': remaining_wall,
                 'retry_reserve_fraction': RETRY_FRACTION,
                 'fixed_worker_cpu_bounds': bin_costs,
                 'worker_root_assignment': [[[r['owner'],r['family']] for r in batch] for batch in bins],
                 'interpretation': 'Observed worst feasible sensitivity, not a guaranteed future runtime bound.',
                 'decision': 'PILOT_BUDGET_PREFLIGHT_PASS' if cpu_bound<=remaining_cpu and wall_bound<=remaining_wall else 'RESOURCE_BLOCKED'}
    save(args.artifacts/'preflight.json', preflight)
    if args.scan_only or preflight['decision']=='RESOURCE_BLOCKED':
        return
    if available() < 4*1024**2:
        save(args.artifacts/'decision.json', {'decision': 'RESOURCE_BLOCKED', 'reason': 'available memory'})
        return
    branch_start = time.monotonic(); seen_cpu = {}; results = []
    deadline = branch_start+remaining_wall
    executor = ProcessPoolExecutor(max_workers=8)
    futures = [executor.submit(batch_job, (batch, str(args.artifacts), deadline)) for batch in bins]
    pending = set(futures)
    try:
        while pending:
            done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
            for process in executor._processes.values():
                seen_cpu[process.pid] = max(seen_cpu.get(process.pid, 0), process_cpu(process.pid))
            if sum(seen_cpu.values())+time.process_time()-cpu > remaining_cpu or time.monotonic()>=deadline or available()<4*1024**2:
                raise RuntimeError('RESOURCE_BLOCKED: pilot process-tree budget/memory')
            for future in done:
                for result in future.result():
                    results.append(result)
                    append(log, {'boundary': 'root_completed', 'owner': result['root']['owner'],
                                 'family': result['root']['family'], 'job_cpu': result['job_cpu']})
    except BaseException as exc:
        append(log, {'boundary': 'failed', 'message': str(exc), 'observed_worker_cpu': sum(seen_cpu.values()),
                     'wall': time.monotonic()-start})
        executor.terminate_workers()
        raise
    else:
        executor.shutdown()
    save(args.artifacts/'pilot.json', {'identity': identity, 'roots': roots, 'sources': sources,
         'results': results, 'worker_cpu': sum(seen_cpu.values()),
         'job_cpu': sum(r['job_cpu'] for r in results), 'parent_cpu': time.process_time()-cpu,
         'wall': time.monotonic()-start, 'branch_wall': time.monotonic()-branch_start,
         'scan_cpu': scan['cpu'], 'scan_wall': scan['wall'], 'preflight': preflight,
         'distinct_owners': sorted({r['owner'] for r in roots}),
         'shared_owners': sorted({r['owner'] for r in roots if sum(x['owner']==r['owner'] for x in roots)>1})})


if __name__ == '__main__':
    main()
