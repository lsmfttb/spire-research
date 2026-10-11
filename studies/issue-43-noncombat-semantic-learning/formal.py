"""Trusted, disposable Issue43 pipeline readiness. No automatic stage launch.

Call sites must be admitted by a subsequent Planner decision. This module is
tested with synthetic tables and admission roots only; no formal data exist.
"""
import hashlib
import json
import os
from pathlib import Path
import random
from stage0 import CONFIG, Battle, Restored, controller, prepared, digest
from student import with_public_history
from learning import load
from adapter import resolve_native_action
from sts_combat_rl.sim.controller_contract import ControllerProvenance, ControllerDecision
from sts_combat_rl.sim.online_controller import PolicyController, RoutedRunController
from sts_combat_rl.sim.non_combat_policy import ExpertNonCombatDriver
from sts_combat_rl.sim.action_space import ActionSpaceConfig
from sts_combat_rl.sim.controlled_run import execute_controlled_run
import resource
import time


def atomic(path, value):
    if path.exists():
        raise ValueError('immutable result already exists')
    temp = path.with_suffix('.tmp')
    with temp.open('w') as stream:
        json.dump(value,stream,sort_keys=True,allow_nan=False); stream.flush(); os.fsync(stream.fileno())
    os.replace(temp,path)


class Completed:
    """One worker owns this manifest. Atomic result precedes completion record.

    A crash between the two is recovered by verifying the exact immutable result
    and publishing its completion record. Truncated final JSONL bytes fail closed;
    preserve that attempt and start a new manifest referencing verified results.
    """
    def __init__(self, directory, identity):
        self.directory = Path(directory); self.directory.mkdir(parents=True,exist_ok=True)
        self.identity = identity; self.log = self.directory/'completed.jsonl'
        identity_path = self.directory/'identity.json'
        if identity_path.exists():
            if json.loads(identity_path.read_text()) != identity:
                raise ValueError('attempt identity changed')
        else:
            atomic(identity_path,identity)
        self.completed = {}
        if self.log.exists():
            for line in self.log.read_text().splitlines():
                event = json.loads(line)
                if event['boundary']=='completed':
                    self.completed[event['id']] = event['sha256']

    def event(self, event):
        with self.log.open('a') as stream:
            stream.write(json.dumps(event,sort_keys=True)+'\n'); stream.flush(); os.fsync(stream.fileno())

    def get(self, key):
        ident = digest(key); path = self.directory/(ident+'.json')
        if not path.exists():
            if ident in self.completed:
                raise ValueError('manifest result missing')
            return None
        result = json.loads(path.read_text())
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if result['key'] != key or result['identity'] != self.identity or not result['terminal'] or result['problems']:
            raise ValueError('invalid completed result')
        if ident in self.completed and self.completed[ident] != actual:
            raise ValueError('completed result checksum mismatch')
        if ident not in self.completed:
            self.event({'boundary':'completed','id':ident,'sha256':actual,'recovered_orphan':True})
            self.completed[ident] = actual
        return result

    def publish(self, key, result):
        if not result['terminal'] or result['problems']:
            self.event({'boundary':'invalid','key':key,'problems':result['problems']})
            raise ValueError('invalid execution is never a loss label')
        value = {**result,'key':key,'identity':self.identity}
        path = self.directory/(digest(key)+'.json')
        atomic(path,value)
        return self.get(key)


def cohort(roots, split):
    """Select first qualifying family per run in immutable source order."""
    low,high = CONFIG['source_ranges'][split]; quota = CONFIG['quotas'][split]
    selected=[]; families={f:0 for f in ('MAP_SCREEN','REWARDS')}; occupied=set()
    for root in sorted(roots,key=lambda r:(r['owner'],r['step'])):
        if not low <= root['owner'] <= high or root['split'] != split:
            raise ValueError('owner outside immutable split')
        family=root['family']; owner_family=(root['owner'],family)
        if family not in families or root['legal_count']<2:
            continue
        if owner_family in occupied:
            continue
        occupied.add(owner_family)
        if families[family] < quota[family]:
            selected.append(root); families[family]+=1
    owners={r['owner'] for r in selected}
    if any(families[f]!=quota[f] for f in families) or len(owners)<quota['owners']:
        raise ValueError('SOURCE_COVERAGE_INSUFFICIENT')
    return selected


def validate_splits(cohorts):
    seen=set()
    for split in ('train','validation','heldout'):
        roots=cohort(cohorts[split],split); owners={r['owner'] for r in roots}
        if seen & owners:
            raise ValueError('source owner overlap')
        seen.update(owners)
    return True


def keys(root):
    return [[root['split'],root['owner'],root['family'],root['step'],ordinal,driver,
             root['candidate_sha256'][ordinal],root['public_input_sha256'],
             root['privileged_checkpoint_fingerprint_sha256'],digest(CONFIG)]
            for ordinal in range(root['legal_count']) for driver in CONFIG['continuation_seeds'][root['split']]]


def force(adapter, checkpoint, fingerprint, public, ordinal, owner, driver, progress):
    """Same-process realized-world branch; no cross-process opaque cp transfer."""
    snapshot=adapter.restore_checkpoint(checkpoint)
    if adapter.checkpoint_fingerprint(snapshot)!=fingerprint:
        raise ValueError('checkpoint fidelity mismatch')
    native=resolve_native_action(public,ordinal,dict(adapter._sim.public_projection_v3()),list(adapter._sim.legal_actions()))
    actions=adapter.legal_actions(snapshot)
    if actions[ordinal].raw['native'].bits!=native.bits:
        raise ValueError('fresh native legal identity mismatch')
    transition=adapter.step(actions[ordinal]); post=adapter.capture_checkpoint(transition.snapshot)
    run=execute_controlled_run(Restored(adapter,post),controller(driver),seed=owner,
        max_steps=CONFIG['max_steps'],action_space=ActionSpaceConfig.initial_no_potions(),before_decision=progress)
    if not run.terminal or run.problems:
        raise ValueError('INCOMPLETE: never convert a cap/problem into loss')
    return {'terminal':True,'problems':[], 'terminal_floor':run.final_raw['floor_num'],
            'final_raw_privileged':run.final_raw,'outcome':run.outcome,'steps':len(run.steps)}


def capture_owner(adapter, owner, split, progress):
    low,high=CONFIG['source_ranges'][split]
    if not low<=owner<=high:
        raise ValueError('owner outside immutable split')
    roots={}; native={}; counts={'unsupported_other':0,'supported_single_action':0}
    begin=time.monotonic(); cpu=time.process_time()
    def observe(snapshot,actions,context,index):
        progress(snapshot,actions,context,index)
        family=context.screen_state
        if family not in ('MAP_SCREEN','REWARDS'):
            counts['unsupported_other']+=1
            return
        p=prepared(adapter)
        if len(actions)<2:
            counts['supported_single_action']+=1
            return
        if family in roots:
            return
        fingerprint=adapter.checkpoint_fingerprint(snapshot)
        serialized=with_public_history(p.serialized_input,context.public_run_context.get('history',[]),context.public_run_context)
        value=json.loads(p.serialized_input)
        roots[family]={'split':split,'owner':owner,'family':family,'step':index,
            'source_floor':snapshot.raw['floor_num'],'legal_count':len(actions),
            'serialized_public_input':serialized,'public_input_sha256':digest(json.loads(serialized)),
            'candidate_sha256':[digest(option) for option in value['legal_options']],
            'privileged_checkpoint_fingerprint_sha256':digest(fingerprint),
            'source_driver_seed':CONFIG['source_driver_seed']}
        native[family]=(adapter.capture_checkpoint(snapshot),fingerprint,p)
    run=execute_controlled_run(adapter,controller(CONFIG['source_driver_seed']),seed=owner,
        max_steps=CONFIG['max_steps'],action_space=ActionSpaceConfig.initial_no_potions(),before_decision=observe)
    if not run.terminal or run.problems:
        raise ValueError('INCOMPLETE_SOURCE')
    return roots,native,{'owner':owner,'cpu':time.process_time()-cpu,'wall':time.monotonic()-begin,
        'terminal':True,'problems':[], 'screen_counts':counts,
        'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}


def execute_owner(adapter, selected, store, progress):
    """Regenerate frozen source, then persist every original candidate/driver key.

    Selection happens in the separately frozen cohort manifest; regeneration
    cannot add or replace roots. No checkpoint is serialized or moved processes.
    """
    owners={r['owner'] for r in selected}; splits={r['split'] for r in selected}
    if len(owners)!=1 or len(splits)!=1:
        raise ValueError('worker must own one frozen source run')
    begin=time.monotonic(); cpu=time.process_time()
    roots,native,source_cost=capture_owner(adapter,next(iter(owners)),next(iter(splits)),progress)
    store.event({'boundary':'source_regenerated',**source_cost})
    results=[]
    for root in selected:
        if roots[root['family']]!=root:
            raise ValueError('frozen source/root identity changed')
        cp,fingerprint,p=native[root['family']]
        for key in keys(root):
            completed=store.get(key)
            if completed is not None:
                results.append(completed); continue
            store.event({'boundary':'branch_started','key':key})
            branch_start=time.monotonic(); branch_cpu=time.process_time()
            def observe(snapshot,actions,context,index):
                progress(snapshot,actions,context,index)
                if index%25==0:
                    store.event({'boundary':'branch_progress','key':key,'step':index,
                                 'attempt_cpu':time.process_time()-cpu,'attempt_wall':time.monotonic()-begin})
            result=force(adapter,cp,fingerprint,p,key[4],root['owner'],key[5],observe)
            result.update(cpu=time.process_time()-branch_cpu,wall=time.monotonic()-branch_start,
                          peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            results.append(store.publish(key,result))
    return results


def targets(root, branches):
    expected=keys(root)
    by_key={digest(row['key']):row for row in branches}
    if len(branches)!=len(expected) or set(by_key)!={digest(k) for k in expected}:
        raise ValueError('missing/duplicate/non-original legal continuation')
    q=[]
    for ordinal in range(root['legal_count']):
        rows=[by_key[digest(k)] for k in expected if k[4]==ordinal]
        if any(not r['terminal'] or r['problems'] for r in rows):
            raise ValueError('invalid continuation is not a Q label')
        q.append(sum(max(0,r['terminal_floor']-root['source_floor']) for r in rows)/len(rows))
    return {'split':root['split'],'owner':root['owner'],'root_id':digest(expected),
            'serialized':root['serialized_public_input'],'q':q}


class LearnedNonCombat:
    """Trusted ordinal bridge. Scorer sees only serialized public JSON."""
    def __init__(self,checkpoint):
        self.arm=checkpoint['arm']; self.model=load(checkpoint)
        self.fallback=PolicyController(ExpertNonCombatDriver(seed=CONFIG['fresh_driver_seed']))
        self.provenance=ControllerProvenance(kind='decision_policy',name='issue43_'+self.arm,
            config={'information_regime':'normal_public_policy','checkpoint_sha256':checkpoint['checkpoint_sha256'],
                    'supported':['MAP_SCREEN','REWARDS'],'other_fallback':self.fallback.provenance.to_dict()})
        self.choices=[]

    def reset_for_run(self,seed):
        self.fallback.reset_for_run(seed); self.choices=[]

    def select_action(self,adapter,snapshot,actions,context,step_index):
        if context.screen_state not in ('MAP_SCREEN','REWARDS'):
            decision=self.fallback.select_action(adapter,snapshot,actions,context,step_index)
            self.choices.append({'family':context.screen_state,'route':'explicit_other_fallback'})
            return decision
        p=prepared(adapter)  # throws on malformed/ambiguous supported variants
        serialized=with_public_history(p.serialized_input,context.public_run_context.get('history',[]),context.public_run_context)
        ordinal=self.model.choose(serialized,self.arm)
        native=resolve_native_action(p,ordinal,dict(adapter._sim.public_projection_v3()),list(adapter._sim.legal_actions()))
        if ordinal>=len(actions) or actions[ordinal].raw['native'].bits!=native.bits:
            raise ValueError('PUBLIC_INPUT_INVALID: legal execution mapping')
        self.choices.append({'family':context.screen_state,'route':'learned','public_sha256':digest(json.loads(serialized)),
                             'ordinal':ordinal,'step':step_index})
        return ControllerDecision(selected_index=ordinal,provenance=self.provenance,reason='frozen_scorer')


def evaluation_plan(checkpoints):
    if set(checkpoints)!={'visible','masked'}:
        raise ValueError('both frozen arms required')
    for checkpoint in checkpoints.values():
        load(checkpoint)
    return [{'seed':seed,'arm':arm,'key':digest([seed,arm,digest(CONFIG),
             checkpoints[arm]['checkpoint_sha256'] if arm!='expert_source' else 'frozen_expert_source']),
             'checkpoint_sha256':checkpoints[arm]['checkpoint_sha256'] if arm!='expert_source' else None}
            for seed in range(CONFIG['fresh_seeds'][0],CONFIG['fresh_seeds'][1]+1)
            for arm in ('visible','masked','expert_source')]


def evaluation_controller(arm, checkpoints):
    if arm=='expert_source':
        return controller(CONFIG['fresh_driver_seed'])
    return RoutedRunController(battle=Battle(),non_combat=LearnedNonCombat(checkpoints[arm]))


def evaluate_one(adapter, plan_row, checkpoints, store, progress):
    if plan_row not in evaluation_plan(checkpoints):
        raise ValueError('not a frozen fresh evaluation key')
    key=[plan_row['key'],plan_row['seed'],plan_row['arm']]
    previous=store.get(key)
    if previous is not None:
        return previous
    selected=evaluation_controller(plan_row['arm'],checkpoints)
    store.event({'boundary':'fresh_started','key':key})
    begin=time.monotonic(); cpu=time.process_time()
    run=execute_controlled_run(adapter,selected,seed=plan_row['seed'],max_steps=CONFIG['max_steps'],
        action_space=ActionSpaceConfig.initial_no_potions(),before_decision=progress)
    if not run.terminal or run.problems:
        raise ValueError('INCOMPLETE_FRESH: no death substitution')
    choices=selected.non_combat.choices if plan_row['arm']!='expert_source' else []
    return store.publish(key,{'terminal':True,'problems':[], 'seed':plan_row['seed'],'arm':plan_row['arm'],
        'floor':run.final_raw['floor_num'],'outcome':run.outcome,'final_raw_privileged':run.final_raw,
        'steps':len(run.steps),'choices':choices,'cpu':time.process_time()-cpu,'wall':time.monotonic()-begin,
        'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss})


def clustered_interval(values, seed, replicates=10000):
    """Owner -> list of root metrics. Resample owners, retain all nested roots."""
    rng=random.Random(seed); owners=sorted(values); samples=[]
    if not owners:
        raise ValueError('no owners')
    for _ in range(replicates):
        roots=[x for owner in rng.choices(owners,k=len(owners)) for x in values[owner]]
        samples.append(sum(roots)/len(roots))
    samples.sort()
    return {'mean':sum(x for row in values.values() for x in row)/sum(map(len,values.values())),
            'lower':samples[int(.025*replicates)],'upper':samples[int(.975*replicates)],
            'owners':len(owners),'bootstrap_seed':seed,'replicates':replicates}
