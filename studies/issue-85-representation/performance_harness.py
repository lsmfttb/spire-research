from __future__ import annotations
import argparse, copy, ctypes, hashlib, importlib, importlib.util, json, os, platform, subprocess, sys, time, types
from pathlib import Path
try:
    import resource
except ImportError:
    resource=None
sys.dont_write_bytecode=True

ap=argparse.ArgumentParser()
ap.add_argument('--arm',required=True,choices=list('BCDEF'))
ap.add_argument('--mode',required=True,choices=['baseline','candidate'])
ap.add_argument('--issue84-root',required=True,type=Path)
ap.add_argument('--pilot-root',required=True,type=Path)
ap.add_argument('--out-root',required=True,type=Path)
a=ap.parse_args()
arm=a.arm
issue84_root=a.issue84_root.resolve(strict=True)
pilot_root=a.pilot_root.resolve(strict=True)
out_root=a.out_root.resolve()
study=Path(__file__).resolve().parent
repo=study.parents[1]
# Worktrees were created by Windows Git; translate their .git pointers for the pinned WSL Python.
sys.path.insert(0,str(study))
from performance_provenance import capture_worktree_source, _git_environment

_real_subprocess_run=subprocess.run
def _git_aware_run(cmd,*args,**kwargs):
    if isinstance(cmd,(list,tuple)) and cmd and str(cmd[0]).endswith('git') and '-C' in cmd:
        try: target=Path(cmd[list(cmd).index('-C')+1]).resolve()
        except Exception: target=None
        if target==repo.resolve() or target==issue84_root:
            env=dict(os.environ); env.update(kwargs.get('env') or {}); env.update(_git_environment(target)); kwargs['env']=env
    return _real_subprocess_run(cmd,*args,**kwargs)
subprocess.run=_git_aware_run
expected_head='5cd70c00d03f1104728efdc4b66c764592e5acf8'
try:
    measurement_source=capture_worktree_source(repo)
except (OSError,ValueError,subprocess.CalledProcessError) as exc:
    raise SystemExit(f'could not bind measurement code to a clean commit: {exc}') from exc
runner_path='studies/issue-85-representation/pilot_runner.py'
locked_runner=subprocess.run(['git','-C',str(repo),'show',f'{expected_head}:{runner_path}'],check=True,capture_output=True).stdout
if (repo/runner_path).read_bytes().replace(b'\r\n',b'\n')!=locked_runner.replace(b'\r\n',b'\n'): raise SystemExit('pilot runner differs from the locked baseline commit')
lock_path=study/'source-lock.json'
lock_sha=hashlib.sha256(lock_path.read_bytes()).hexdigest()
if lock_sha!='a25c25cd4144cbe362c7c241460e9d71f4554627c9bb129407ef69ec8366404a': raise SystemExit('source-lock SHA mismatch')
out_root.mkdir(parents=True,exist_ok=True)
arm_out=out_root/a.mode/arm
arm_out.mkdir(parents=True,exist_ok=False)

spec=importlib.util.spec_from_file_location('issue85_perf_pilot_runner',study/'pilot_runner.py')
runner=importlib.util.module_from_spec(spec); sys.modules[spec.name]=runner; spec.loader.exec_module(runner)
os.environ['ISSUE84_WORKTREE_CLEAN']='1'
head84=subprocess.run(['git','-C',str(issue84_root),'rev-parse','HEAD'],check=True,capture_output=True,text=True).stdout.strip()
status84=subprocess.run(['git','-C',str(issue84_root),'status','--porcelain','--untracked-files=all'],check=True,capture_output=True,text=True).stdout
if head84!='556128df66797bf979d36b56b6920ac88bab5b35' or status84: raise SystemExit('Issue 84 source worktree is not the exact clean lock')
identity={'package_commit':expected_head,'source_lock_sha256':lock_sha}
backend,driver=runner._configure_runtime(issue84_root,pilot_root,identity)
import torch
import numpy as np
torch.set_num_threads(1)
if tuple(torch.__version__.split('+')[0].split('.')[:2]) != ('2','12') or np.__version__!='2.4.6': raise SystemExit('pinned torch/numpy versions differ')

# Reuse only receipt-bound existing pilot examples. No native/game runner is invoked.
seed=runner.read_json(pilot_root/'pilot-seed-table.json')
rows=[r for r in seed['rows'] if r.get('phase')=='training' and r.get('formal') is False and r.get('issue85_arm')==arm]
rows.sort(key=lambda r:int(r['owner_seed']))
if len(rows)!=16 or len({r['unit_id'] for r in rows})!=16: raise SystemExit(f'expected 16 unique saved pilot owners for {arm}, got {len(rows)}')
receipts=runner.read_json(pilot_root/'pilot-completed-training-reuse.json').get('validated_unit_receipts',[])
receipt_map={r['unit_id']:r for r in receipts if r.get('arm')==arm}
if arm in {'C','D'} and len(receipt_map)!=16: raise SystemExit(f'{arm} does not have 16 immutable completion receipts')
if arm=='B': package='0297d60382e5dfe64d8de93f6ca383e96b655f40'; base=pilot_root.parent/package/'training'/arm
elif arm in {'C','D'}: package=next(iter(receipt_map.values()))['package_commit']; base=Path(next(iter(receipt_map.values()))['evidence_root'])
else: package='60730db692faba1db5c85ccd8a88ae2000f47c05'; base=pilot_root.parent/package/'training'/arm
root_map={}
if receipt_map:
    for unit,r in receipt_map.items(): root_map[unit]=(Path(r['evidence_root']),r['package_commit'])
# Validate each immutable result/feature pointer and retain exact provenance hashes.
input_receipts=[]
for row in rows:
    row_root, row_pkg=root_map.get(row['unit_id'],(base,package))
    record=backend.existing_success_record(row_root,row,row_pkg)
    if record is None:
        key=backend.unit_key(row['unit_id']); unit=row_root/'units'/key
        files=list(unit.glob('attempt-*/result.json')) if unit.exists() else []
        print('DEBUG_RECEIPT',json.dumps({'unit_id':row['unit_id'],'row':row,'root':str(row_root),'package':row_pkg,'source_lock_expected':backend.source_lock_sha(),'unit_dir':str(unit),'files':[str(x) for x in files],'receipts':[json.loads(x.read_text()) for x in files]},default=str)[:6000],flush=True)
        raise SystemExit(f'missing source-bound successful outcome {row["unit_id"]}')
    result,result_path=record
    feat=result['training_features']; feat_path=row_root/feat['path']
    if runner.file_sha256(feat_path)!=feat['sha256']: raise SystemExit('training feature raw SHA mismatch')
    input_receipts.append({'unit_id':row['unit_id'],'owner_seed':row['owner_seed'],'package_commit':row_pkg,
        'result_sha256':runner.file_sha256(result_path),
        'training_features_sha256':feat['sha256'],'training_features_uncompressed_sha256':feat['uncompressed_sha256']})
# Load data from the verified immutable records through the pinned Issue 84 loader.
algorithm, runtime, model, learning, executor=backend.load_modules(arm_out/'module-scratch',pilot=True)
examples=runner._load_pilot_training_examples(driver,rows,root=base,executor=executor,package_commit=package,receipt_roots_by_owner=root_map)
if len(examples)!=16 or any(not runner._pilot_training_example_is_usable(x) for x in examples): raise SystemExit('loaded pilot examples failed complete-owner checks')

# Load each arm's exact completed Pilot actor and AdamW state via its immutable reuse manifest.
reuse_name='pilot-actor-update-reuse.json' if arm=='B' else f'pilot-{arm}-actor-update-reuse.json'
reuse=runner.read_json(pilot_root/reuse_name)
if reuse.get('source_lock_sha256')!=lock_sha or reuse.get('current_package_commit')!=expected_head: raise SystemExit('actor reuse manifest does not bind to reviewed pilot source')
state_root=pilot_root.parent/reuse['predecessor_package_commit']
receipt=reuse['actor_update_receipt']
if receipt.get('status')!='SUCCEEDED' or receipt.get('completed_steps')!=64 or receipt.get('arm')!=arm: raise SystemExit('pilot actor update receipt is not complete')
checkpoint=state_root/receipt['checkpoint_path']; optimizer_file=state_root/receipt['optimizer_path']
if runner.file_sha256(checkpoint)!=receipt['checkpoint_sha256'] or runner.file_sha256(optimizer_file)!=receipt['optimizer_sha256']: raise SystemExit('actor/optimizer checkpoint hash mismatch')
row_token=runner._CURRENT_ROW.set(rows[0])
try: actor,_,_=executor._load_model('pi12',proxy=False)
finally: runner._CURRENT_ROW.reset(row_token)
actor.load_state_dict(torch.load(checkpoint,map_location='cpu',weights_only=True),strict=True)
if model.model_digest(actor)!=receipt['model_digest'] or actor.arm!=arm: raise SystemExit('loaded Pilot actor digest/arm mismatch')
actor.train()
optimizer=learning._new_optimizer(actor)
optimizer.load_state_dict(torch.load(optimizer_file,map_location='cpu',weights_only=True))
gradients=importlib.import_module('gradients')

# Record authentic decision cardinalities and typed-token/cross-product sizes outside timed updates.
rep=importlib.import_module('representations')
card=[]
for ex in examples:
  for di,d in enumerate(ex.base.decisions):
    payload=getattr(d,'issue85_payload',None)
    entry={'owner_seed':int(ex.owner_seed),'decision_index':di,'candidate_count':len(d.legal_identities)}
    if arm!='B' and payload:
      cand=rep._checked_payload(payload)
      full=arm in {'C','D','F'}
      ctx_leaves=rep._typed_leaves(rep._context_tree(payload,full=full),'context')
      cand_n=sum(len(rep._typed_leaves(rep._candidate_tree(x,include_public_action=full),'candidate')) for x in cand)
      entry.update({'context_token_count':len(ctx_leaves),'candidate_token_count':cand_n,'candidate_context_cross_products':len(ctx_leaves)*cand_n})
    card.append(entry)

# Low-overhead timing hooks isolate update stages; every saved tensor is a detached CPU copy.
timers={k:0.0 for k in ['owner_loss_inclusive','owner_training_outcome_adaptation','block_validation','feature_tensorization','distribution_inclusive','encoder_input_construction','backbone_forward','residual_forward']}
current={'step_records':None}
def timed(name,fn):
  def wrapped(*args,**kwargs):
    t=time.perf_counter()
    try: return fn(*args,**kwargs)
    finally: timers[name]+=time.perf_counter()-t
  return wrapped
def cell(value): return (lambda x: lambda: x)(value).__closure__[0]
orig_owner=gradients.owner_loss
if not orig_owner.__closure__: raise SystemExit('runtime owner_loss lacks expected source adapter closure')
names=orig_owner.__code__.co_freevars; cells=list(orig_owner.__closure__)
if 'owner_training_outcome' in names:
  ix=names.index('owner_training_outcome'); cells[ix]=cell(timed('owner_training_outcome_adaptation',cells[ix].cell_contents))
profiled_owner=types.FunctionType(orig_owner.__code__,orig_owner.__globals__,orig_owner.__name__,orig_owner.__defaults__,tuple(cells))
profiled_owner.__kwdefaults__=orig_owner.__kwdefaults__
orig_check=gradients._check_block; gradients._check_block=timed('block_validation',orig_check)
orig_feature=gradients._feature_matrix; gradients._feature_matrix=timed('feature_tensorization',orig_feature)
def owner_wrapper(actor_arg,example,**kwargs):
  t=time.perf_counter()
  try:
    value=profiled_owner(actor_arg,example,**kwargs)
    if current['step_records'] is not None:
      current['step_records']['owner_losses'].append({'owner_seed':int(example.owner_seed),'loss':value[0].detach().cpu().clone(),'diagnostics':copy.deepcopy(value[1])})
    return value
  finally: timers['owner_loss_inclusive']+=time.perf_counter()-t
gradients.owner_loss=owner_wrapper
captured={'inputs':[],'encoded':[],'distributions':[]}
if arm!='B':
  enc=actor.encoder.encode
  def enc_wrapper(payload,*,device):
    t=time.perf_counter()
    try:
      x=enc(payload,device=device)
      if current['step_records'] is not None and a.mode=='baseline':
        captured['encoded'].append(x.detach().cpu().clone())
      return x
    finally: timers['encoder_input_construction']+=time.perf_counter()-t
  actor.encoder.encode=enc_wrapper
  def residual_pre(_m,_in): current.setdefault('residual_start',[]).append(time.perf_counter())
  def residual_post(_m,_in,_out): timers['residual_forward']+=time.perf_counter()-current['residual_start'].pop()
  actor.residual.register_forward_pre_hook(residual_pre); actor.residual.register_forward_hook(residual_post)
backbone_logits=actor.backbone.logits
def backbone_wrapper(features):
  t=time.perf_counter()
  try: return backbone_logits(features)
  finally: timers['backbone_forward']+=time.perf_counter()-t
actor.backbone.logits=backbone_wrapper
old_dist=actor.distribution
def dist_wrapper(features,payload,**kwargs):
  t=time.perf_counter()
  try:
    if current['step_records'] is not None: captured['inputs'].append(features.detach().cpu().clone())
    if current['step_records'] is not None and kwargs.get('encoded_features') is not None:
      captured['encoded'].append(kwargs['encoded_features'].detach().cpu().clone())
    result=old_dist(features,payload,**kwargs)
    if current['step_records'] is not None: captured['distributions'].append(tuple(x.detach().cpu().clone() for x in result))
    return result
  finally: timers['distribution_inclusive']+=time.perf_counter()-t
actor.distribution=dist_wrapper

def cpu_tree(v):
  if torch.is_tensor(v): return v.detach().cpu().clone()
  if isinstance(v,dict): return {k:cpu_tree(x) for k,x in v.items()}
  if isinstance(v,(list,tuple)): return type(v)(cpu_tree(x) for x in v)
  return copy.deepcopy(v)
def process_memory():
  if os.name=='nt':
    class Counters(ctypes.Structure):
      _fields_=[('cb',ctypes.c_ulong),('PageFaultCount',ctypes.c_ulong),
        ('PeakWorkingSetSize',ctypes.c_size_t),('WorkingSetSize',ctypes.c_size_t),
        ('QuotaPeakPagedPoolUsage',ctypes.c_size_t),('QuotaPagedPoolUsage',ctypes.c_size_t),
        ('QuotaPeakNonPagedPoolUsage',ctypes.c_size_t),('QuotaNonPagedPoolUsage',ctypes.c_size_t),
        ('PagefileUsage',ctypes.c_size_t),('PeakPagefileUsage',ctypes.c_size_t)]
    counters=Counters(); counters.cb=ctypes.sizeof(counters)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True); psapi=ctypes.WinDLL('psapi',use_last_error=True)
    get_current=kernel.GetCurrentProcess; get_current.restype=ctypes.c_void_p
    get_info=psapi.GetProcessMemoryInfo; get_info.argtypes=[ctypes.c_void_p,ctypes.POINTER(Counters),ctypes.c_ulong]; get_info.restype=ctypes.c_int
    if not get_info(get_current(),ctypes.byref(counters),counters.cb): return None,None
    return int(counters.WorkingSetSize),int(counters.PeakWorkingSetSize//1024)
  rss=None
  try:
    for line in Path('/proc/self/status').read_text().splitlines():
      if line.startswith('VmRSS:'): rss=int(line.split()[1])*1024; break
  except Exception: pass
  peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource is not None else None
  return rss,peak
def rss_bytes(): return process_memory()[0]
def peak_rss_kib(): return process_memory()[1]

steps=[]
for step in range(1,3):
  current['step_records']={'owner_losses':[]}
  captured={'inputs':[],'encoded':[],'distributions':[]}
  timer_start=dict(timers)
  cpu0=time.process_time(); wall0=time.perf_counter(); t=time.perf_counter()
  optimizer.zero_grad(set_to_none=True)
  zero=time.perf_counter()-t
  t=time.perf_counter(); loss=gradients.minibatch_loss(actor,examples,arm=arm,block_examples=examples); loss_time=time.perf_counter()-t
  if not bool(torch.isfinite(loss)): raise SystemExit(f'non-finite loss at {arm} step {step}')
  current['step_records']['total_loss']=loss.detach().cpu().clone()
  t=time.perf_counter(); loss.backward(); backward=time.perf_counter()-t
  params=tuple(p for p in actor.parameters() if p.requires_grad)
  grads_before={n:p.grad.detach().cpu().clone() for n,p in actor.named_parameters() if p.grad is not None}
  clip_limit=runner._issue84_actor_gradient_norm_clip(driver)
  t=time.perf_counter(); grad_norm=torch.nn.utils.clip_grad_norm_(params,clip_limit); clip=time.perf_counter()-t
  if not bool(torch.isfinite(grad_norm)): raise SystemExit('non-finite gradient norm')
  grads_after={n:p.grad.detach().cpu().clone() for n,p in actor.named_parameters() if p.grad is not None}
  t=time.perf_counter(); optimizer.step(); opt_time=time.perf_counter()-t
  wall=time.perf_counter()-wall0; cpu=time.process_time()-cpu0
  cache_metrics=gradients._issue85_input_cache_metrics(actor) if hasattr(gradients,'_issue85_input_cache_metrics') else None
  state_digest=model.model_digest(actor)
  steps.append({'step':step,'total_loss':loss.detach().cpu().clone(),'owner_losses':current['step_records']['owner_losses'],
    'inputs':captured['inputs'],'encoded_features':captured['encoded'],'distributions':captured['distributions'],
    'gradients_before_clip':grads_before,'gradients_after_clip':grads_after,'grad_norm_before_clip':grad_norm.detach().cpu().clone(),
    'model_state':cpu_tree(actor.state_dict()),'optimizer_state':cpu_tree(optimizer.state_dict()),'model_digest_after_step':state_digest,
    'timings':{'wall_seconds':wall,'cpu_seconds':cpu,'zero_grad_seconds':zero,'loss_forward_owner_aggregation_seconds':loss_time,
      'backward_seconds':backward,'clip_seconds':clip,'optimizer_seconds':opt_time,
      **{k:timers[k]-timer_start[k] for k in timers}},'rss_bytes_after_step':rss_bytes(),
    'input_cache':copy.deepcopy(cache_metrics)})
  print(f'STEP_COMPLETE arm={arm} step={step} wall={wall:.3f}s cpu={cpu:.3f}s rss={rss_bytes()}',flush=True)
  current['step_records']=None

def stat(values):
  if not values:return None
  s=sorted(values); n=len(s)
  def q(p): return s[min(n-1,round((n-1)*p))]
  return {'count':n,'min':s[0],'median':q(.5),'p90':q(.9),'max':s[-1]}
candidate_counts=[x['candidate_count'] for x in card]
report={'schema_id':'issue85-offline-performance-'+a.mode+'-v2','scope':'existing pilot data only; no games/replays/formal training',
 'runner_commit':expected_head,'runner_source_lock_sha256':lock_sha,'issue84_commit':head84,
 'issue84_source_lock_sha256':driver.file_sha256(issue84_root/'studies'/'issue-84-six-arm'/'source-lock.json'),
 'pilot_package_commit':expected_head,'arm':arm,'mode':'uncached_baseline','python_version':sys.version.split()[0],
 'torch_version':torch.__version__,'numpy_version':np.__version__,'torch_num_threads':torch.get_num_threads(),
 'platform':platform.platform(),'machine':platform.machine(),'processor':platform.processor(),
 'input_receipts':input_receipts,'actor_checkpoint_sha256':receipt['checkpoint_sha256'],'optimizer_checkpoint_sha256':receipt['optimizer_sha256'],
 'actor_model_digest_start':receipt['model_digest'],'owner_count':len(examples),'decision_count':len(card),'mode':a.mode,
 'measurement_source':measurement_source,
 'candidate_count_distribution':stat(candidate_counts),'candidate_context_cardinality':{
   'context_token_count':stat([x['context_token_count'] for x in card if 'context_token_count'in x]),
   'candidate_token_count':stat([x['candidate_token_count'] for x in card if 'candidate_token_count'in x]),
   'candidate_context_cross_products':stat([x['candidate_context_cross_products'] for x in card if 'candidate_context_cross_products'in x])},
 'rss_bytes_before_updates':rss_bytes(),'ru_maxrss_before_updates_kib':peak_rss_kib(),
 'ru_maxrss_after_updates_kib':peak_rss_kib(),
 'steps':[{'step':s['step'],'total_loss':float(s['total_loss'].item()),'grad_norm_before_clip':float(s['grad_norm_before_clip'].item()),
   'owner_count':len(s['owner_losses']),'decision_input_count':len(s['inputs']),'encoded_input_count':len(s['encoded_features']),
   'rss_bytes_after_step':s['rss_bytes_after_step'],'timings':s['timings'],'input_cache':s['input_cache'],'model_digest_after_step':s['model_digest_after_step']} for s in steps],
 'input_cache':next((s['input_cache'] for s in steps if s['input_cache'] is not None),None),
 'interpretation':'Step 1 is first measured update after source/model/data loading; step 2 is repeated update, not a long-run steady-state estimate.'}
try:
    final_measurement_source=capture_worktree_source(repo)
except (OSError,ValueError,subprocess.CalledProcessError) as exc:
    raise SystemExit(f'measurement source changed during run: {exc}') from exc
if final_measurement_source!=measurement_source: raise SystemExit('measurement source identity changed during run')
tensors={'schema_id':report['schema_id'],'arm':arm,'steps':steps,'measurement_source':measurement_source}
torch.save(tensors,arm_out/'snapshots.pt')
report['snapshot_sha256']=hashlib.sha256((arm_out/'snapshots.pt').read_bytes()).hexdigest()
report['snapshot_bytes']=(arm_out/'snapshots.pt').stat().st_size
raw=(json.dumps(report,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()
with (arm_out/'metrics.json').open('xb') as f: f.write(raw); f.flush(); os.fsync(f.fileno())
print(json.dumps({'arm':arm,'report':str(arm_out/'metrics.json'),'snapshot_bytes':report['snapshot_bytes'],'steps':report['steps'],'rss_bytes_before_updates':report['rss_bytes_before_updates']},sort_keys=True))
