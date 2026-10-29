from __future__ import annotations
import argparse, hashlib, importlib.util, json, os, resource, subprocess, sys, tarfile, time, traceback, zlib
from pathlib import Path

BASE="1a117752e6057ea2660e196f150a85c12117a0e0"
REPO=Path(__file__).resolve().parents[2]
STUDY=REPO/"studies/issue-81-pg-only"
STUDY75=REPO/"studies/issue-75-no-expert-oracle"
STUDY79=REPO/"studies/issue-79-successor"
DATA=Path("/mnt/d/DeadlycatCoding/stsrl-data/issue81")
ART=DATA/"artifacts"; REF=DATA/"references"; STAGE1=STUDY/"stage1"
LOCK=STUDY/"source-lock.json"; SEEDS=STUDY/"seed-table.json"; CONTRACT=STUDY/"contract.json"
PYTHON=Path("/mnt/d/DeadlycatCoding/stsrl-data/stsrl-spikes/py313-torch/bin/python3.13")
NATIVE=Path("/mnt/d/DeadlycatCoding/stsrl-data/stsrl-spikes/build-issue71-8ded-py313/slaythespire.cpython-313-x86_64-linux-gnu.so")
PI1ROOT=Path("/mnt/d/DeadlycatCoding/stsrl-data/issue75/update0-00d6405b-UPDATE-0-4db46e9573a4405a")
STS=Path("/mnt/d/DeadlycatCoding/stsrl-data/issue81/sources/sts_lightspeed")
STSRL=Path("/mnt/d/DeadlycatCoding/stsrl-data/issue81/sources/STSRL")
BUNDLE=STUDY79/"artifacts/issue79-formal-review-bundle-968309e7de0132e3ec763c3c7dc036a2da472285.tar.gz"
RAW79=Path("/mnt/d/DeadlycatCoding/stsrl-data/issue79/formal-raw-968309e7de0132e3ec763c3c7dc036a2da472285.tar")
TRAIN=tuple(range(7600001,7600385)); EVAL=tuple(range(7640001,7640097))
ARMS=("BASE","PGONLY","JTRAIN"); ALLOWANCE=3784.5
SUPPLEMENTAL_CLOCK_ID="issue81-eval-supplement-20261030"; SUPPLEMENTAL_ALLOWANCE=1800.0
SMOKE_SEEDS=(7699001,7699002,7699003)
PGONLY_ROOT=ART/"formal"/"7ff0fcaf3fb53c6aaeb26a6b960345a320d25e51"
EVALUATION_ONLY_CONTRACT=STUDY/"evaluation-only-contract.json"
PREVIOUS_PACKAGES=("a7d6df2ee635ccd14b37365c21b5a4bafabb5379","a623cb1087221b953bd100099077c0effb6f34b3","7ff0fcaf3fb53c6aaeb26a6b960345a320d25e51")
PREVIOUS_EVALUATION_ONLY_PACKAGE_COMMIT="a484175828868da5de7147694122b5ff41da4d25"
PREVIOUS_BASE_SMOKE_RESULT_SHA256="b8b5a597c78f7ec1cda082374c3f30cd90299041a402a885bc6d2c3f0c560d37"
PREVIOUS_BASE_SMOKE_SOURCE_LOCK_SHA256="ba440d9a7f005affa4a3eb6703db73267fa773dbf9e747726f5597976bacc6ae"
EXPECT={"pi1_sha":"d36577e18b175930a275b4151ba2a93ca6c8fe1e3422512453d4017000c21978",
"pi1_digest":"33b3d3d891e53b7434919318ccb1c8411a0bebe1e2e48616ef040206261db557",
"pi1_opt":"7bdff6bdd14d8bf5bdcd1cf286e687b5a6df4ef6c11f80bad1ee18787e40f514",
"j_sha":"36e7dfdc78d3409eb65b2db05f6c6df5821330f502cbbd286bf191e39456eb57",
"j_digest":"521079ea9cb3e57f8b27057f37d6ecfc9181290f532713c18d307d79849704f5",
"j_opt":"622bbc288f2fa405060065af2a45ebdbc72dd8601ac7c3a257a2bf22a4020b6e",
"pg_sha":"ae90cd8f96c06f4cbe6cd5133e1c827530cba2d043cb02d07dcf53fe2945c78f",
"pg_digest":"3eac3177892778192309262e2c4a42734c3a30fa3b742f3ab2732bdd5b9810ce",
"pg_opt":"80d15b396083e0194544838bdb79728c83075e2c14d29257377307be424cea77",
"native":"bb22332a4a7108e8f9691482df2624b3b132d75ca07e1aa7b394b932193e5cad",
"sts":"8ded44c7ee341f48bb1bd49eeaae6cf6bc0384b2","stsr":"7d172f7c24c9d29429b6946ba44f6b59760b0d96"}
def canon(v): return json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()
def sha(b): return hashlib.sha256(b).hexdigest()
def fsha(p):
 h=hashlib.sha256()
 with Path(p).open("rb") as f:
  for b in iter(lambda:f.read(1048576),b""): h.update(b)
 return h.hexdigest()
def readj(p):
 x=json.loads(Path(p).read_text(encoding="utf-8"))
 if not isinstance(x,dict): raise ValueError(f"expected JSON object: {p}")
 return x
def write_once(p,b):
 p=Path(p); p.parent.mkdir(parents=True,exist_ok=True)
 if p.exists():
  if p.read_bytes()!=b: raise FileExistsError(f"immutable evidence differs: {p}")
  return sha(b)
 t=p.with_name(f".{p.name}.{os.getpid()}.tmp")
 try:
  with t.open("xb") as f: f.write(b); f.flush(); os.fsync(f.fileno())
  if p.exists():
   if p.read_bytes()!=b: raise FileExistsError(f"immutable evidence differs: {p}")
  else: os.replace(t,p)
  return sha(b)
 finally: t.unlink(missing_ok=True)
def writej(p,v): return write_once(p,canon(v)+b"\n")
def atomicj(p,v):
 p=Path(p); p.parent.mkdir(parents=True,exist_ok=True); t=p.with_name(f".{p.name}.{os.getpid()}.tmp")
 try:
  with t.open("xb") as f: f.write(canon(v)+b"\n"); f.flush(); os.fsync(f.fileno())
  os.replace(t,p)
 finally: t.unlink(missing_ok=True)

def loadbase():
 spec=importlib.util.spec_from_file_location("issue79_driver_reused",STUDY79/"driver.py")
 if spec is None or spec.loader is None: raise RuntimeError("retained #79 driver unavailable")
 m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
 m.__file__=str(STUDY/"driver.py")
 m.STUDY=STUDY; m.REPO=REPO; m.STUDY75=STUDY75; m.STAGE1=STAGE1
 m.RETAINED=REF; m.ARTIFACTS=ART; m.SEED_TABLE=SEEDS; m.SOURCE_LOCK=LOCK
 m.PINNED_PYTHON=PYTHON; m.FORMAL_TRAINING=TRAIN; m.FORMAL_EVALUATION=EVAL
 m.ARMS=ARMS; m.CONCURRENCY=8; m.write_once=write_once; m.write_json_once=writej; m.atomic_json=atomicj
 return m
D=loadbase(); ORIGINAL_LOAD=D.load_modules; ACTIVE_BLOCK=None; OPT_INIT={"loaded":False,"source":None}

def seed_table():
 rows=[]
 for i,s in enumerate(TRAIN):
  r=D._row(s,phase="training",arm="TRAIN",formal=True,block=1+i//128); r["search_invocation_seeds"]=[]; rows.append(r)
 for s in EVAL:
  for arm in ARMS:
   r=D._row(s,phase="heldout",arm=arm,formal=True,block=None); r["search_invocation_seeds"]=[]; rows.append(r)
 return {"schema_id":"issue81-fixed-seed-table-v1","study_id":"issue-81-pgonly-j-elite-ablation",
 "source_commit":BASE,"training_owner_ids":list(TRAIN),"evaluation_owner_ids":list(EVAL),
 "evaluation_arm_order":list(ARMS),"policy_rng_streams":"#79 domain-separated owner/arm streams fixed before outcomes",
 "search_invocation_seeds":[],"rows":rows}

def set_native(v):
 if isinstance(v,dict):
  for k,x in v.items():
   if k=="native_module_path": v[k]=str(NATIVE)
   else: set_native(x)
 elif isinstance(v,list):
  for x in v: set_native(x)

def contract():
 return {"schema_id":"issue81-pgonly-j-elite-ablation-v1","study_id":"issue-81-pgonly-j-elite-ablation",
 "scope":"STUDY_ONLY","baseline_commit":BASE,"sts_lightspeed_commit":EXPECT["sts"],"stsr_source_commit":EXPECT["stsr"],
 "native_sha256":EXPECT["native"],
 "training":{"owners":384,"owner_seeds":[TRAIN[0],TRAIN[-1]],"blocks":[128,128,128],"updates_per_block":64,
 "pi1_sha256":EXPECT["pi1_sha"],"pi1_digest":EXPECT["pi1_digest"],"pi1_optimizer_sha256":EXPECT["pi1_opt"],
 "objective":"L_pg - 0.01 H","elite_loss":"removed; exactly zero asserted for every step",
 "search":"no training or evaluation search; empty registered search seed streams"},
 "evaluation":{"owners":96,"owner_seeds":[EVAL[0],EVAL[-1]],"arms":{"BASE":"pi1","PGONLY":"new PG-only final pi4","JTRAIN":"unchanged retained #79 pi4"},
 "terminals":288,"primary":"JTRAIN-PGONLY terminal-floor mean, paired owner bootstrap, 20,000 percentile 95% CI",
 "secondaries":["PGONLY-BASE","JTRAIN-BASE","Act2/3/4","Heart"],"no_common_future_rng_path_claim":True},
 "resource":{"projection_seconds":1892.269837,"allowance_seconds":ALLOWANCE,
 "allowance_factor":ALLOWANCE/1892.269837,"one_cumulative_active_program_clock":True,
 "overlap_counted_once":True,"failed_attempts_included":True,"idle_gaps_excluded":True,"per_owner_cpu_kill":False},
 "raw_evidence_root":str(DATA),"limitations":["privileged full-state training/native surrogate only; no live-game/public transfer claim",
 "on-policy paths diverge; contrast is full procedure difference, not fixed-data pure-gradient causality",
 "floor 16 does not identify first-Boss cause; do not infer missing terminal HP/potion/deck"]}

def extract_ref(member,dest,expected):
 dest.parent.mkdir(parents=True,exist_ok=True)
 if dest.exists():
  if fsha(dest)!=expected: raise RuntimeError(f"durable model copy has wrong digest: {dest}")
  return
 p=subprocess.run(["tar","-xOzf",str(BUNDLE),member],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 if sha(p.stdout)!=expected: raise RuntimeError(f"bundle source digest mismatch: {member}")
 write_once(dest,p.stdout)

def evaluation_only_contract():
 return {"schema_id":"issue81-evaluation-only-amendment-v1","study_id":"issue-81-pgonly-j-elite-ablation",
 "authorization_comment":"https://github.com/lsmfttb/spire-research/issues/81#issuecomment-6505214689",
 "execution_mode":"evaluation_only","no_training_loop":True,"no_optimizer_updates":True,
 "original_failed_package_commit":"7ff0fcaf3fb53c6aaeb26a6b960345a320d25e51",
 "original_clock":{"id":"issue81-global","elapsed_seconds":3784.857659886001,"allowance_seconds":3784.5,"immutable":True},
 "supplemental_clock":{"id":SUPPLEMENTAL_CLOCK_ID,"allowance_seconds":SUPPLEMENTAL_ALLOWANCE,
 "one_clock_covers_smoke_heldout_failures_and_retries":True,"concurrent_overlap_counted_once":True},
 "diagnostic_smoke":{"owner_seeds":list(SMOKE_SEEDS),"arms":list(ARMS),"formal":False,
 "outside_training_and_heldout_populations":True,"efficacy_cohort":False},
 "heldout":{"seed_table_sha256":fsha(SEEDS),"owners":96,"arms":list(ARMS),"expected_terminals":288,
 "reuses_original_registered_rows_and_streams":True,"online_search":"none"},
 "models":{"BASE":{"path":str(PI1ROOT/"models/pi1.pt"),"sha256":EXPECT["pi1_sha"],"state_digest":EXPECT["pi1_digest"],
 "optimizer_sha256":EXPECT["pi1_opt"]},
 "PGONLY":{"path":str(PGONLY_ROOT/"models/pi4.pt"),"sha256":EXPECT["pg_sha"],"state_digest":EXPECT["pg_digest"],
 "optimizer_path":str(PGONLY_ROOT/"models/block-3-step.pt"),"optimizer_sha256":EXPECT["pg_opt"]},
 "JTRAIN":{"path":str(REF/"jtrain-pi4.pt"),"sha256":EXPECT["j_sha"],"state_digest":EXPECT["j_digest"],
 "optimizer_sha256":EXPECT["j_opt"]}},
 "native_module_sha256":EXPECT["native"],"sts_lightspeed_commit":EXPECT["sts"],"stsr_source_commit":EXPECT["stsr"],
 "preserve_prior_package_roots":list(PREVIOUS_PACKAGES)}

def prepare():
 mb=subprocess.run(["git","merge-base","HEAD",BASE],cwd=REPO,check=True,text=True,capture_output=True).stdout.strip()
 if mb!=BASE: raise RuntimeError(f"task branch not based on exact #79 SHA: {mb}")
 for p in (DATA,REF,ART,STAGE1,DATA/"cache/pycache",DATA/"cache/xdg",DATA/"cache/torch",DATA/"tmp"): p.mkdir(parents=True,exist_ok=True)
 extract_ref("models/pi4.pt",REF/"jtrain-pi4.pt",EXPECT["j_sha"])
 extract_ref("models/block-3-step.pt",REF/"jtrain-block-3-step.pt",EXPECT["j_opt"])
 stage_cfg=readj(STUDY75/"stage1/config.json"); set_native(stage_cfg); stage_bytes=canon(stage_cfg)+b"\n"
 cfg=readj(STUDY75/"config.json"); set_native(cfg); config_bytes=canon(cfg)+b"\n"
 write_once(STUDY/"config.json",config_bytes); write_once(STAGE1/"config.json",stage_bytes)
 write_once(SEEDS,canon(seed_table())+b"\n"); write_once(CONTRACT,canon(contract())+b"\n")
 pgo_model=PGONLY_ROOT/"models/pi4.pt"; pgo_opt=PGONLY_ROOT/"models/block-3-step.pt"
 if fsha(pgo_model)!=EXPECT["pg_sha"] or fsha(pgo_opt)!=EXPECT["pg_opt"]: raise RuntimeError("retained PGONLY model/checkpoint identity mismatch")
 pgo_audit=readj(PGONLY_ROOT/"models/issue81-update-3.json")
 if pgo_audit.get("model_sha256")!=EXPECT["pg_sha"] or pgo_audit.get("checkpoint_sha256")!=EXPECT["pg_opt"] or pgo_audit.get("result_model_digest")!=EXPECT["pg_digest"] or pgo_audit.get("completed_steps")!=64 or pgo_audit.get("elite_loss_sum")!=0.0 or pgo_audit.get("elite_gradient_norm_sum")!=0.0: raise RuntimeError("retained PGONLY training audit does not bind the approved frozen model")
 write_once(EVALUATION_ONLY_CONTRACT,canon(evaluation_only_contract())+b"\n")
 lock79=readj(STUDY79/"source-lock.json"); refs=lock79["source_reference"]["files_sha256"]
 actual={p:fsha(STUDY75/p) for p in refs}
 if actual!=refs: raise RuntimeError("#75 files differ from #79 locked source")
 runtime_source=cfg["source"]
 lock={"schema_id":"issue81-source-lock-v1","baseline_commit":BASE,
 "issue79_driver_sha256":fsha(STUDY79/"driver.py"),"issue79_source_lock_sha256":fsha(STUDY79/"source-lock.json"),
 "issue75_source_commit":lock79["source_reference"]["commit"],"issue75_files_sha256":actual,
 "issue75_config_sha256":fsha(STUDY75/"config.json"),"issue75_stage1_config_sha256":fsha(STUDY75/"stage1/config.json"),
 "runtime":{"sts_lightspeed_commit":runtime_source["sts_lightspeed_commit"],
 "stsr_source_commit":runtime_source["stsr_source_commit"],"native_module_sha256":fsha(NATIVE),"native_module_path":str(NATIVE)},
 "models":{"pi1_sha256":fsha(PI1ROOT/"models/pi1.pt"),"pi1_optimizer_sha256":fsha(PI1ROOT/"models/block-0-step.pt"),
 "retention_manifest_sha256":fsha(PI1ROOT/"retention-manifest.json"),"jtrain_sha256":fsha(REF/"jtrain-pi4.pt"),
 "jtrain_optimizer_sha256":fsha(REF/"jtrain-block-3-step.pt"),"pgonly_sha256":fsha(PGONLY_ROOT/"models/pi4.pt"),"pgonly_optimizer_sha256":fsha(PGONLY_ROOT/"models/block-3-step.pt")},
 "supplemental_evaluation":{"clock_id":SUPPLEMENTAL_CLOCK_ID,"allowance_seconds":SUPPLEMENTAL_ALLOWANCE,"evaluation_only":True,"smoke_seeds":list(SMOKE_SEEDS)},
 "code_sha256":fsha(STUDY/"driver.py"),"launcher_sha256":fsha(STUDY/"run_issue81.sh"),"contract_sha256":fsha(CONTRACT),
 "evaluation_only_contract_sha256":fsha(EVALUATION_ONLY_CONTRACT),
 "seed_table_sha256":fsha(SEEDS),"config_sha256":fsha(STUDY/"config.json"),
 "stage1_config_sha256":fsha(STAGE1/"config.json"),"allowance_seconds":ALLOWANCE,"strict_elite_only_removal":True}
 writej(LOCK,lock)
 return {"status":"PREPARED","baseline":BASE,"source_lock_sha256":fsha(LOCK),"seed_rows":len(seed_table()["rows"]),"data_path":str(DATA)}

def check_lock():
 l=readj(LOCK)
 for key,path in (("code_sha256",STUDY/"driver.py"),("launcher_sha256",STUDY/"run_issue81.sh"),("contract_sha256",CONTRACT),("seed_table_sha256",SEEDS),
 ("config_sha256",STUDY/"config.json"),("stage1_config_sha256",STAGE1/"config.json"),("evaluation_only_contract_sha256",EVALUATION_ONLY_CONTRACT),
 ("issue79_driver_sha256",STUDY79/"driver.py"),("issue79_source_lock_sha256",STUDY79/"source-lock.json")):
  if fsha(path)!=l[key]: raise RuntimeError(f"locked file changed: {path}")
 for p,h in l["issue75_files_sha256"].items():
  if fsha(STUDY75/p)!=h: raise RuntimeError(f"#75 source lock mismatch: {p}")
 if fsha(PGONLY_ROOT/"models/pi4.pt")!=l["models"]["pgonly_sha256"] or fsha(PGONLY_ROOT/"models/block-3-step.pt")!=l["models"]["pgonly_optimizer_sha256"]: raise RuntimeError("retained PGONLY model/checkpoint changed after source lock")
 return l

def registered_arm_policy(phase,arm,row,fallback):
 if row.get("phase")=="training": return f"pi{int(row['block_index'])}",False
 policies={"BASE":("pi1",False),"PGONLY":("pi4",False),"JTRAIN":("jtrain",False)}
 if arm in policies: return policies[arm]
 return fallback(phase,arm,row)

def native_controller_arm(arm):
 # The Issue 79 executor's registered labels are BASE/PGONLY/JTRAIN, but the
 # shared native controller accepts only P0/P1 (and the other Issue 79 arms).
 # JTRAIN uses P1 here because this evaluation loads the frozen JTRAIN model
 # without invoking the online J search procedure.
 policies={"BASE":"P0","PGONLY":"P1","JTRAIN":"P1"}
 try: return policies[arm]
 except KeyError as exc: raise ValueError(f"unregistered Issue 81 evaluation arm for native controller: {arm}") from exc

def patch_modules(root,*,pilot=False):
 global ACTIVE_BLOCK
 alg,rt,model,learn,ex=ORIGINAL_LOAD(root,pilot=pilot)
 rt.STUDY=STUDY; rt.REPO_ROOT=REPO
 ex.STAGE1=STAGE1; ex.ARTIFACT_ROOT=root; ex.ROOT=REPO; ex.STUDY_ID="issue-81-pgonly-j-elite-ablation"
 oldpath=ex._model_path
 def model_path(label):
  if label=="pi1": return PI1ROOT/"models/pi1.pt"
  if label=="jtrain": return REF/"jtrain-pi4.pt"
  if label=="pi4": return PGONLY_ROOT/"models/pi4.pt"
  return oldpath(label)
 ex._model_path=model_path
 oldarm=ex._arm_policy
 def arm_policy(phase,arm,row):
  return registered_arm_policy(phase,arm,row,oldarm)
 ex._arm_policy=arm_policy
 def save_features(directory,value):
  if value.get("search_receipts")!=[] or value.get("joint_trajectories")!=[]: raise RuntimeError("PGONLY artifact contains search labels")
  raw=canon(value)+b"\n"; zipped=zlib.compress(raw,9); path=directory/"training-features.json.zlib"; write_once(path,zipped)
  return {"path":path.resolve().relative_to(root.resolve()).as_posix(),"sha256":sha(zipped),
  "uncompressed_sha256":sha(raw),"compression":"zlib","uncompressed_bytes":len(raw),"stored_bytes":len(zipped)}
 ex._save_training_features=save_features
 def save_model(policy,path,digest):
  import torch
  path.parent.mkdir(parents=True,exist_ok=True)
  if path.exists():
   state=torch.load(path,map_location="cpu",weights_only=True)
   check=type(policy)(dimension=ex.POLICY_DIMENSION,seed=73507501).eval(); check.load_state_dict(state,strict=True)
   if digest(check)!=digest(policy): raise FileExistsError(f"model differs: {path}")
  else:
   tmp=path.with_name(f".{path.name}.{os.getpid()}.tmp")
   try:
    with tmp.open("xb") as f: torch.save(policy.state_dict(),f); f.flush(); os.fsync(f.fileno())
    if path.exists(): raise FileExistsError(f"model path appeared: {path}")
    os.replace(tmp,path)
   finally: tmp.unlink(missing_ok=True)
  return {"path":path.relative_to(ex.ARTIFACT_ROOT).as_posix(),"sha256":fsha(path),"model_digest":digest(policy)}
 ex._save_model_once=save_model
 alg.run_search=lambda *a,**k: (_ for _ in ()).throw(RuntimeError("Issue 81 forbids every search call"))
 if not getattr(learn,"_issue81_patched",False):
  oldloss=learn._owner_losses
  def pg_losses(policy,batch,mean,sd):
   if any(getattr(x,"joint_trajectories",()) for x in batch): raise RuntimeError("PGONLY batch contains J labels")
   pg,_elite,entropy,_eo,_er=oldloss(policy,batch,mean,sd)
   return pg,next(policy.parameters()).sum()*0.0,entropy,0,0
  learn._owner_losses=pg_losses
  oldupdate=learn._update
  def pg_update(*a,**k):
   d=oldupdate(*a,**k)
   if any((d.loss_elite!=0.,d.grad_norm_elite!=0.,d.elite_owner_count!=0,d.elite_root_count!=0)):
    raise RuntimeError("PGONLY performed a nonzero elite update")
   return d
  learn._update=pg_update
  oldopt=learn._new_optimizer
  def newopt(policy):
   opt=oldopt(policy)
   if ACTIVE_BLOCK==1:
    import torch
    p=PI1ROOT/"models/block-0-step.pt"; state=torch.load(p,map_location="cpu",weights_only=True)
    if state.get("completed_steps")!=64: raise RuntimeError("wrong inherited pi1 optimizer steps")
    opt.load_state_dict(state["optimizer_state_dict"]); OPT_INIT.update({"loaded":True,"source":fsha(p)})
   return opt
  learn._new_optimizer=newopt; learn._issue81_patched=True
 init=rt.initialize_native_runtime
 def no_search_init(out,*,sampling,reviewer_proof=None,stage1_binding=None):
  cfg,runner,native,encoder=init(out,sampling=False,reviewer_proof=None,stage1_binding=None); oldrun=runner._run_one
  def natural(*a,**kw):
   call=dict(kw); registered_arm=str(call.get("arm")); call["arm"]=native_controller_arm(registered_arm)
   call["root_callback"]=None; call["capture_checkpoints"]=False; call["capture_root_limit"]=None
   result=oldrun(*a,**call); attempt=Path(call["out"]).parent
   writej(attempt/"natural-owner-observation.json",{"schema_id":"issue81-natural-owner-observation-v1",
   "arm":registered_arm,"controller_arm":str(call.get("arm")),"seed":int(call["seed"]),"completed_transitions":int(result.get("completed_transitions",0)),
   "screen_counts":result.get("screen_counts") if isinstance(result.get("screen_counts"),dict) else None,"action_kind_counts":result.get("action_kind_counts") if isinstance(result.get("action_kind_counts"),dict) else None,"native_result_keys":sorted(str(k) for k in result),"search_root_callback":False})
   return result
  runner._run_one=natural; return cfg,runner,native,encoder
 rt.initialize_native_runtime=no_search_init
 return alg,rt,model,learn,ex
D.load_modules=patch_modules

def worker(job_path):
 job=readj(job_path); path=Path(job["attempt_dir"]); before=resource.getrusage(resource.RUSAGE_SELF); cb=resource.getrusage(resource.RUSAGE_CHILDREN)
 start=time.monotonic(); code=1
 try: code=int(D.worker_main(job_path)); return code
 finally:
  after=resource.getrusage(resource.RUSAGE_SELF); ca=resource.getrusage(resource.RUSAGE_CHILDREN)
  cpu=max(0.,after.ru_utime+after.ru_stime+ca.ru_utime+ca.ru_stime-before.ru_utime-before.ru_stime-cb.ru_utime-cb.ru_stime)
  writej(path/"attempt-resource.json",{"schema_id":"issue81-attempt-resource-v1","unit_id":job.get("unit_id"),
  "run_id":job.get("run_id"),"worker_pid":os.getpid(),"exit_code":code,"process_tree_cpu_seconds":cpu,
  "worker_wall_seconds":max(0.,time.monotonic()-start),"status":"SUCCEEDED" if code==0 else "FAILED"})

def previous_smoke_receipt(row,*,current_root):
 if row.get("unit_id")!="SMOKE:7699001:BASE" or row.get("owner_seed")!=7699001 or row.get("arm")!="BASE" or row.get("formal") is not False:
  return None
 current_attempts=current_root/"units"/D.unit_key(row["unit_id"])
 if current_attempts.exists() and any(current_attempts.glob("attempt-*")): return None
 prior_root=ART/"evaluation-only"/PREVIOUS_EVALUATION_ONLY_PACKAGE_COMMIT
 binding=readj(prior_root/"evaluation-only-package.json")
 models=binding.get("model_identities",{})
 if (models.get("pi1_sha256")!=EXPECT["pi1_sha"] or models.get("pi1_digest")!=EXPECT["pi1_digest"] or
     models.get("pi1_optimizer_sha256")!=EXPECT["pi1_opt"] or binding.get("optimizer_checkpoint_sha256")!=EXPECT["pg_opt"]):
  raise RuntimeError("prior BASE smoke package model/native identities do not match")
 if (binding.get("package_commit")!=PREVIOUS_EVALUATION_ONLY_PACKAGE_COMMIT or
     binding.get("runner_code_commit")!=PREVIOUS_EVALUATION_ONLY_PACKAGE_COMMIT or
     binding.get("source_lock_sha256")!=PREVIOUS_BASE_SMOKE_SOURCE_LOCK_SHA256):
  raise RuntimeError("prior BASE smoke package binding does not match its registered source")
 attempt=prior_root/"units"/D.unit_key(row["unit_id"])/"attempt-0001"
 result_path=attempt/"result.json"
 if fsha(result_path)!=PREVIOUS_BASE_SMOKE_RESULT_SHA256:
  raise RuntimeError("prior BASE smoke result is missing or its immutable digest changed")
 result=readj(result_path); finished=readj(attempt/"finished.json")
 resource_receipt=readj(attempt/"attempt-resource.json"); job=readj(attempt/"worker-job.json")
 if (finished.get("status")!="SUCCEEDED" or finished.get("result_sha256")!=PREVIOUS_BASE_SMOKE_RESULT_SHA256 or
     finished.get("unit_id")!=row["unit_id"] or resource_receipt.get("status")!="SUCCEEDED" or
     resource_receipt.get("unit_id")!=row["unit_id"] or resource_receipt.get("run_id")!="SMOKE:7699001:BASE-attempt-0001" or
     job.get("package_commit")!=PREVIOUS_EVALUATION_ONLY_PACKAGE_COMMIT or job.get("runner_code_commit")!=PREVIOUS_EVALUATION_ONLY_PACKAGE_COMMIT or
     job.get("unit_id")!=row["unit_id"] or job.get("phase")!="heldout" or job.get("row",{}).get("formal") is not False or
     job.get("row",{}).get("arm")!="BASE" or job.get("row",{}).get("owner_seed")!=7699001):
  raise RuntimeError("prior BASE smoke does not have a matching successful full-worker receipt chain")
 if (result.get("status")!="SUCCEEDED" or result.get("package_commit")!=PREVIOUS_EVALUATION_ONLY_PACKAGE_COMMIT or
     result.get("runner_code_commit")!=PREVIOUS_EVALUATION_ONLY_PACKAGE_COMMIT or
     result.get("source_lock_sha256")!=PREVIOUS_BASE_SMOKE_SOURCE_LOCK_SHA256 or
     result.get("unit_id")!=row["unit_id"] or result.get("owner_seed")!=7699001 or result.get("formal") is not False or
     result.get("phase")!="heldout" or result.get("arm")!="BASE" or result.get("model_digest")!=EXPECT["pi1_digest"] or
     result.get("policy_source",{}).get("source")!="pi1" or result.get("policy_source",{}).get("checkpoint_sha256")!=EXPECT["pi1_sha"] or
     result.get("native_module_sha256")!=EXPECT["native"] or result.get("native_source_commit")!=EXPECT["sts"] or
     result.get("stsr_source_commit")!=EXPECT["stsr"] or result.get("expert_decisions")!=0 or result.get("search")!=[] or
     not isinstance(result.get("terminal_floor"),int) or result.get("terminal_outcome") is None):
  raise RuntimeError("prior BASE smoke terminal does not match the registered model/native/no-search treatment")
 return result,{"package_commit":PREVIOUS_EVALUATION_ONLY_PACKAGE_COMMIT,
  "result_path_under_durable_data_root":result_path.relative_to(DATA).as_posix(),
  "result_sha256":PREVIOUS_BASE_SMOKE_RESULT_SHA256,"source_lock_sha256":PREVIOUS_BASE_SMOKE_SOURCE_LOCK_SHA256,
  "finished_receipt_sha256":fsha(attempt/"finished.json"),"resource_receipt_sha256":fsha(attempt/"attempt-resource.json")}

def run_smoke_once(rows,*,root,clock,package_commit):
 start=time.monotonic(); complete={}; row_wall={}; native_wall={}; tries={}
 reused_prior_receipts={}
 python=str(D.PINNED_PYTHON); env=os.environ.copy()
 for name in ("STS_LIGHTSPEED_SOURCE_DIR","STSRL_SOURCE_DIR"):
  if not env.get(name): raise RuntimeError(f"{name} must name the exact pinned source worktree")
 for row in rows:
  unit=row["unit_id"]; saved=D.existing_success(root,row,package_commit)
  if saved is not None:
   complete[unit]=saved; tries[unit]=1
   wall=float(saved.get("resource",{}).get("worker_wall_seconds",0.0)); row_wall[unit]=wall; native_wall[unit]=wall
   continue
  prior=previous_smoke_receipt(row,current_root=root)
  if prior is not None:
   saved,provenance=prior; complete[unit]=saved; tries[unit]=1; reused_prior_receipts[unit]=provenance
   wall=float(saved.get("resource",{}).get("worker_wall_seconds",0.0)); row_wall[unit]=wall; native_wall[unit]=wall
   continue
  base=root/"units"/D.unit_key(unit)
  prior=sorted(base.glob("attempt-*")) if base.exists() else []
  if prior:
   return {"status":"PRIOR_FAILED_ATTEMPT_EXISTS","completed":len(complete),"results":complete,
    "remaining":[r["unit_id"] for r in rows if r["unit_id"] not in complete],"tries":tries,
    "reused_prior_receipts":reused_prior_receipts,"elapsed_seconds":time.monotonic()-start}
  base.mkdir(parents=True,exist_ok=True); attempt=base/"attempt-0001"; attempt.mkdir(exist_ok=False)
  job={"schema_id":"issue79-owner-worker-job-v1","artifact_root":str(root),"attempt_dir":str(attempt),
   "result_path":str(attempt/"result.json"),"output_dir":str(attempt/"native"),
   "run_id":f"{unit}-attempt-0001","unit_id":unit,"phase":row["phase"],"row":row,"pilot":False,
   "package_commit":package_commit,"runner_code_commit":package_commit,"source_lock_sha256":D.source_lock_sha()}
  job_path=attempt/"worker-job.json"; D.write_json_once(job_path,job); log=(attempt/"worker.log").open("ab")
  process=subprocess.Popen([python,str(Path(__file__).resolve()),"--worker",str(job_path)],cwd=REPO,env=env,
   stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
  if clock is not None: clock.set_processes({process.pid})
  try:
   while True:
    if clock is not None and clock.expired():
     try: os.killpg(process.pid,signal.SIGTERM)
     except ProcessLookupError: pass
     try: process.wait(timeout=10)
     except subprocess.TimeoutExpired:
      try: os.killpg(process.pid,signal.SIGKILL)
      except ProcessLookupError: pass
      process.wait()
     D.write_json_once(attempt/"global-wall-stop.json",{"schema_id":"issue79-global-wall-stop-v1",
      "unit_id":unit,"run_id":job["run_id"],"allowance_seconds":clock.allowance,
      "cumulative_active_seconds":clock.elapsed()})
     return {"status":"GLOBAL_ACTIVE_WALL_ALLOWANCE_EXCEEDED","completed":len(complete),"results":complete,
      "remaining":[r["unit_id"] for r in rows if r["unit_id"] not in complete],"tries":tries,
      "reused_prior_receipts":reused_prior_receipts,"elapsed_seconds":time.monotonic()-start}
    code=process.poll()
    if code is not None: break
    time.sleep(0.25)
  finally:
   log.close()
   if clock is not None: clock.set_processes(set())
  result=D.read_json(Path(job["result_path"])) if Path(job["result_path"]).exists() else None
  if not result or result.get("status")!="SUCCEEDED":
   return {"status":"SMOKE_WORKER_FAILED","completed":len(complete),"results":complete,
    "remaining":[r["unit_id"] for r in rows if r["unit_id"] not in complete],"tries":{**tries,unit:1},
    "reused_prior_receipts":reused_prior_receipts,"failed_unit":unit,"worker_exit_code":code,"elapsed_seconds":time.monotonic()-start}
  complete[unit]=result; tries[unit]=1
  wall=float(result.get("resource",{}).get("worker_wall_seconds",0.0)); row_wall[unit]=wall; native_wall[unit]=wall
 return {"status":"SUCCEEDED","completed":len(complete),"results":complete,"tries":tries,
  "reused_prior_receipts":reused_prior_receipts,"row_wall_seconds":row_wall,
  "native_wall_seconds_by_unit":native_wall,"elapsed_seconds":time.monotonic()-start}

def verify_models(ex):
 import torch
 a,ad,_=ex._load_model("pi1"); p,pd,_=ex._load_model("pi4"); j,jd,_=ex._load_model("jtrain")
 ap=PI1ROOT/"models/block-0-step.pt"; pp=PGONLY_ROOT/"models/block-3-step.pt"; jp=REF/"jtrain-block-3-step.pt"
 ast=torch.load(ap,map_location="cpu",weights_only=True); pst=torch.load(pp,map_location="cpu",weights_only=True); jst=torch.load(jp,map_location="cpu",weights_only=True)
 audit=readj(PGONLY_ROOT/"models/issue81-update-3.json")
 if fsha(PI1ROOT/"models/pi1.pt")!=EXPECT["pi1_sha"] or ad!=EXPECT["pi1_digest"]: raise RuntimeError("pi1 identity mismatch")
 if fsha(PGONLY_ROOT/"models/pi4.pt")!=EXPECT["pg_sha"] or pd!=EXPECT["pg_digest"]: raise RuntimeError("PGONLY identity mismatch")
 if fsha(REF/"jtrain-pi4.pt")!=EXPECT["j_sha"] or jd!=EXPECT["j_digest"]: raise RuntimeError("JTRAIN identity mismatch")
 if fsha(pp)!=EXPECT["pg_opt"] or audit.get("model_sha256")!=EXPECT["pg_sha"] or audit.get("checkpoint_sha256")!=EXPECT["pg_opt"] or audit.get("result_model_digest")!=EXPECT["pg_digest"]: raise RuntimeError("PGONLY final update audit/model/checkpoint chain mismatch")
 if any(st.get("completed_steps")!=64 for st in (ast,pst,jst)): raise RuntimeError("optimizer step count mismatch")
 for model,state in ((a,ast),(p,pst),(j,jst)):
  for k,v in model.state_dict().items():
   if not torch.equal(v,state["model_state_dict"][k]): raise RuntimeError("optimizer embeds a model different from the frozen policy")
 if any(audit.get(k)!=0 for k in ("elite_loss_sum","elite_gradient_norm_sum","elite_owner_count_sum","elite_root_count_sum")): raise RuntimeError("PGONLY source update included elite supervision")
 return {"pi1_sha256":fsha(PI1ROOT/"models/pi1.pt"),"pi1_digest":ad,"pi1_optimizer_sha256":fsha(ap),
 "pgonly_sha256":fsha(PGONLY_ROOT/"models/pi4.pt"),"pgonly_digest":pd,"pgonly_optimizer_sha256":fsha(pp),
 "jtrain_sha256":fsha(REF/"jtrain-pi4.pt"),"jtrain_digest":jd,"jtrain_optimizer_sha256":fsha(jp),
 "embedded_models_match":True,"optimizer_steps":64,"pgonly_zero_elite_training_audit":True}

def verify():
 check_lock()
 if D.source_commit()==BASE: raise RuntimeError("commit locked package before registered execution")
 mb=subprocess.run(["git","merge-base","HEAD",BASE],cwd=REPO,check=True,text=True,capture_output=True).stdout.strip()
 if mb!=BASE: raise RuntimeError("package no longer descends from exact #79 baseline")
 if fsha(NATIVE)!=EXPECT["native"]: raise RuntimeError("native binary digest mismatch")
 cfg=readj(STUDY/"config.json"); alg,rt,model,learn,ex=D.load_modules(ART/"verify")
 rt.verify_source_repositories(cfg)
 _,runner,native,_=rt.initialize_native_runtime(ART/"verify/loaded-native",sampling=False)
 np=Path(native.__file__).resolve(strict=True)
 if np!=NATIVE.resolve() or fsha(np)!=EXPECT["native"]: raise RuntimeError("loaded native identity mismatch")
 models=verify_models(ex)
 return {"status":"VERIFIED","source_commit":D.source_commit(),"source_lock_sha256":fsha(LOCK),
 "native_module_path":str(np),"native_module_sha256":fsha(np),
 "sts_lightspeed_commit":cfg["source"]["sts_lightspeed_commit"],"stsr_source_commit":cfg["source"]["stsr_source_commit"],"models":models}

def train_update(block,examples,*,root,pilot,clock):
 global ACTIVE_BLOCK
 ACTIVE_BLOCK=block; OPT_INIT.update({"loaded":False,"source":None})
 receipt=D._original_train_update(block,examples,root=root,pilot=pilot,clock=clock)
 ds=receipt.get("diagnostics",[])
 if len(ds)!=64 or any(x.get("loss_elite")!=0. or x.get("grad_norm_elite")!=0. or x.get("elite_owner_count")!=0 or x.get("elite_root_count")!=0 for x in ds):
  raise RuntimeError(f"block {block} not exactly 64 zero-elite updates")
 if block==1 and not OPT_INIT["loaded"]: OPT_INIT.update({"loaded":True,"source":EXPECT["pi1_opt"],"recovered_receipt":True})
 audit={"schema_id":"issue81-pgonly-update-audit-v1","block_index":block,"owner_count":128,"completed_steps":64,
 "objective":"L_pg - 0.01 H","elite_loss_sum":sum(float(x["loss_elite"]) for x in ds),
 "elite_gradient_norm_sum":sum(float(x["grad_norm_elite"]) for x in ds),
 "elite_owner_count_sum":sum(int(x["elite_owner_count"]) for x in ds),"elite_root_count_sum":sum(int(x["elite_root_count"]) for x in ds),
 "source_model_digest":receipt.get("source_model_digest"),"result_model_digest":receipt.get("result_model_digest"),
 "pi1_optimizer_state_loaded":bool(OPT_INIT["loaded"]) if block==1 else None,
 "starting_optimizer_sha256":OPT_INIT["source"] if block==1 else "fresh AdamW per frozen #79 block",
 "update_receipt_sha256":fsha(root/"models"/f"update-{block}.json"),
 "model_sha256":fsha(root/"models"/f"pi{block+1}.pt"),"checkpoint_sha256":fsha(root/"models"/f"block-{block}-step.pt")}
 writej(root/"models"/f"issue81-update-{block}.json",audit); ACTIVE_BLOCK=None
 return receipt
D._original_train_update=D.train_update; D.train_update=train_update

def rows_for(table,phase,block=None):
 return [r for r in table["rows"] if r["phase"]==phase and r["formal"] is True and (block is None or r.get("block_index")==block)]

def evaluation_only_plan(table=None):
 table=readj(SEEDS) if table is None else table
 if table.get("source_commit")!=BASE or table.get("training_owner_ids")!=list(TRAIN) or table.get("evaluation_owner_ids")!=list(EVAL) or table.get("evaluation_arm_order")!=list(ARMS):
  raise RuntimeError("evaluation-only execution does not match the registered owner/arm streams")
 if len(table.get("rows",[]))!=384+288: raise RuntimeError("registered seed table row count changed")
 smoke=[]
 for seed,arm in zip(SMOKE_SEEDS,ARMS):
  row=D._row(seed,phase="heldout",arm=arm,formal=False,block=None)
  row["unit_id"]=f"SMOKE:{seed}:{arm}"; row["paired_owner_key"]=f"smoke-{seed}"; row["search_invocation_seeds"]=[]
  smoke.append(row)
 heldout=rows_for(table,"heldout")
 if len(heldout)!=288 or any(r.get("formal") is not True or r.get("phase")!="heldout" or r.get("search_invocation_seeds")!=[] for r in heldout):
  raise RuntimeError("heldout units differ from the original fixed no-search formal cohort")
 if {r["owner_seed"] for r in heldout}!=set(EVAL) or {r["arm"] for r in heldout}!={*ARMS}:
  raise RuntimeError("heldout owner/arm population differs from the preregistered cohort")
 if set(SMOKE_SEEDS)&(set(TRAIN)|set(EVAL)): raise RuntimeError("nonformal smoke seeds overlap a registered cohort")
 if any(r.get("phase")!="heldout" or r.get("formal") is not False or r.get("search_invocation_seeds")!=[] for r in smoke):
  raise RuntimeError("diagnostic smoke must be nonformal heldout-mode, no-search work")
 return {"smoke":smoke,"heldout":heldout}

def validate_terminal_batch(batch,rows,*,formal):
 if batch.get("status")!="SUCCEEDED" or batch.get("completed")!=len(rows):
  raise RuntimeError("worker batch did not produce all expected terminal receipts")
 expected_digest={"BASE":EXPECT["pi1_digest"],"PGONLY":EXPECT["pg_digest"],"JTRAIN":EXPECT["j_digest"]}
 terminals=[]
 for row in rows:
  report=batch.get("results",{}).get(row["unit_id"])
  if not isinstance(report,dict) or report.get("status")!="SUCCEEDED": raise RuntimeError(f"missing successful result for {row['unit_id']}")
  if report.get("phase")!="heldout" or bool(report.get("formal")) is not formal or report.get("arm")!=row["arm"] or int(report.get("owner_seed",-1))!=row["owner_seed"]: raise RuntimeError(f"terminal receipt is misbound: {row['unit_id']}")
  if report.get("terminal_outcome") is None or not isinstance(report.get("terminal_floor"),int): raise RuntimeError(f"native terminal evidence missing: {row['unit_id']}")
  if report.get("model_digest")!=expected_digest[row["arm"]]: raise RuntimeError(f"loaded model identity differs for {row['unit_id']}")
  if report.get("native_module_sha256")!=EXPECT["native"] or report.get("native_source_commit")!=EXPECT["sts"] or report.get("stsr_source_commit")!=EXPECT["stsr"]: raise RuntimeError(f"loaded native source identity differs for {row['unit_id']}")
  if report.get("expert_decisions")!=0 or report.get("search")!=[]: raise RuntimeError(f"forbidden Expert or online search call in {row['unit_id']}")
  terminals.append({"unit_id":row["unit_id"],"owner_seed":row["owner_seed"],"arm":row["arm"],
   "formal":formal,"terminal_outcome":report["terminal_outcome"],"terminal_floor":report["terminal_floor"],
   "terminal_act":report["terminal_act"],"act2_reached":bool(report.get("act2_plus",False)),
   "act3_reached":bool(report.get("act3_plus",False)),"act4_reached":bool(report.get("act4_reached",False)),
   "heart_victory":bool(report.get("heart_victory",False)),"model_digest":report["model_digest"]})
 return terminals

def elite_note():
 if not RAW79.is_file(): return {"status":"SOURCE_UNAVAILABLE"}
 alg,rt,m,l,ex=D.load_modules(ART/"verify"); found={}; roots=superior=labels=searches=0; packages=set()
 with tarfile.open(RAW79,"r:") as tar:
  members=tar.getmembers(); files={m.name:m for m in members if m.isfile()}
  feats=[m for m in members if m.isfile() and m.name.endswith("training-features.json.zlib") and any(f"TRAIN-{s}" in m.name for s in TRAIN)]
  for m in feats:
   prefix=m.name.rsplit("/",1)[0]; rp=files.get(prefix+"/result.json")
   if rp is None: continue
   report=json.loads(tar.extractfile(rp).read()); seed=int(report.get("owner_seed",-1))
   if report.get("status")!="SUCCEEDED" or report.get("phase")!="training" or report.get("arm")!="TRAIN" or report.get("formal") is not True or seed not in TRAIN: continue
   zipped=tar.extractfile(m).read()
   if report.get("training_features",{}).get("sha256")!=sha(zipped): continue
   payload=json.loads(zlib.decompress(zipped))
   if payload.get("owner_seed")!=seed or payload.get("unit_id")!=f"TRAIN:{seed}" or seed in found: continue
   packages.add(str(report.get("package_commit"))); found[seed]=(ex._training_example_from_payload(payload),payload)
 for seed,(example,payload) in found.items():
  byroot={}
  for t in example.joint_trajectories:
   if t.complete and t.decisions: byroot.setdefault(str(t.search_root_id or t.decisions[0].state_fingerprint),[]).append(t)
  searches+=len(payload.get("search_receipts",[]))
  for ts in byroot.values():
   roots+=1; candidate=alg.elite_candidate(example.base,ts)
   if candidate is not None: superior+=1; labels+=len(candidate.decisions)
 return {"status":"COMPLETE" if len(found)==384 else "INCOMPLETE_SOURCE_COVERAGE",
 "registered_training_owners":384,"owners_with_successful_source_features":len(found),"missing_owners":384-len(found),
 "source_package_commits":sorted(packages),"successful_J_search_root_receipts":searches,
 "complete_joint_roots_considered":roots,"roots_with_strictly_superior_elite":superior,
 "selected_strictly_superior_elite_decisions":labels,"method":"retained #79 successful training receipts and feature payloads; source elite_candidate grouped by original J root"}

def attempts_summary(root):
 arr=[readj(p) for p in sorted(root.glob("units/**/attempt-resource.json"))]
 unk=[p.relative_to(root).as_posix() for p in root.glob("units/**/attempt-*/global-wall-stop.json") if not (p.parent/"attempt-resource.json").is_file()]
 return {"sidecar_attempts":len(arr),"succeeded":sum(x["status"]=="SUCCEEDED" for x in arr),
 "failed":sum(x["status"]=="FAILED" for x in arr),"known_cpu_seconds":sum(float(x["process_tree_cpu_seconds"]) for x in arr),
 "known_worker_wall_seconds":sum(float(x["worker_wall_seconds"]) for x in arr),"unknown_wall_killed_attempts":unk}

def action_coverage(root,outcomes):
 sums={}
 for arm in ARMS:
  kinds={}; screens={}; owners=kind_owners=screen_owners=0
  for o in outcomes:
   if o["arm"]!=arm: continue
   row=D._row(o["owner_seed"],phase="heldout",arm=arm,formal=True,block=None)
   found=D.existing_success_record(root,row,D.source_commit())
   if not found: continue
   obs=found[1].parent/"natural-owner-observation.json"
   if not obs.is_file(): continue
   data=readj(obs); owners+=1
   if isinstance(data.get("action_kind_counts"),dict):
    kind_owners+=1
    for k,v in data["action_kind_counts"].items(): kinds[k]=kinds.get(k,0)+int(v)
   if isinstance(data.get("screen_counts"),dict):
    screen_owners+=1
    for k,v in data["screen_counts"].items(): screens[k]=screens.get(k,0)+int(v)
  sums[arm]={"owners_with_observation":owners,"owners_with_action_kind_counts":kind_owners,
   "owners_with_screen_counts":screen_owners,"reported_action_kind_counts":kinds,"reported_screen_counts":screens,
   "coverage_limitation":"counts are reported only when exposed by the pinned native result"}
 return sums

def preexecution_failure_summary():
 packages=[]
 for package in PREVIOUS_PACKAGES:
  root=ART/"formal"/package
  logs=sorted(root.glob("units/*/attempt-*/worker.log"))
  attempts_per_unit={}; log_hashes={}; cli_errors=loader_errors=0; resources=[]
  status_by_phase={}; failures={}; started_by_phase={}
  for path in logs:
   unit=path.parent.parent.name
   attempts_per_unit[unit]=attempts_per_unit.get(unit,0)+1
   digest=fsha(path); log_hashes[digest]=log_hashes.get(digest,0)+1
   body=path.read_text(encoding="utf-8",errors="replace")
   cli_errors+= "the following arguments are required: --mode" in body
   loader_errors+= "patch_modules() got an unexpected keyword argument 'pilot'" in body
  hist={}
  for count in attempts_per_unit.values():
   hist[str(count)]=hist.get(str(count),0)+1
  for path in sorted(root.glob("units/*/attempt-*/attempt-resource.json")):
   resources.append(readj(path))
  for path in sorted(root.glob("units/*/attempt-*/result.json")):
   result=readj(path); unit=path.parent.parent.name
   phase="TRAIN" if unit.startswith("TRAIN-") else "EVAL" if unit.startswith("EVAL-") else "OTHER"
   status=result.get("status","UNKNOWN")
   phase_counts=status_by_phase.setdefault(phase,{})
   phase_counts[status]=phase_counts.get(status,0)+1
   if status=="FAILED":
    key=(phase,str(result.get("failure_boundary","UNKNOWN")),str(result.get("failure_detail","")))
    failures[key]=failures.get(key,0)+1
  for path in sorted(root.glob("units/*/attempt-*/started.json")):
   unit=path.parent.parent.name
   phase="TRAIN" if unit.startswith("TRAIN-") else "EVAL" if unit.startswith("EVAL-") else "OTHER"
   started_by_phase[phase]=started_by_phase.get(phase,0)+1
  progress_path=root/"progress.json"
  progress=readj(progress_path) if progress_path.is_file() else {}
  empty_logs=sum(path.stat().st_size==0 for path in logs)
  results_total=sum(sum(counts.values()) for counts in status_by_phase.values())
  eval_failures={key:count for key,count in failures.items() if key[0]=="EVAL"}
  if logs and cli_errors==len(logs):
   classification="launcher CLI rejected every invocation before worker entry"
  elif logs and loader_errors==len(logs) and not started_by_phase and not results_total:
   classification="worker module adapter failed before worker start receipt and native runtime initialization"
  elif logs and loader_errors==len(logs)-empty_logs and empty_logs==max(0,len(logs)-len(resources)) and not started_by_phase and not results_total and progress.get("failure_boundary")=="KeyboardInterrupt":
   classification="all workers with error logs failed in module loading; parent interruption left only empty log attempts"
  elif eval_failures and not status_by_phase.get("EVAL",{}).get("SUCCEEDED",0) and all(
   boundary=="ValueError" and detail=="no registered policy source exists for this Stage 1 row"
   for (_phase,boundary,detail) in eval_failures):
   classification="every heldout attempt failed in Issue 81 arm-policy mapping before native simulation; no heldout terminal succeeded"
  else:
   classification="REVIEW_REQUIRED"
  packages.append({"package_commit":package,"progress_status":progress.get("status"),
   "active_wall_seconds":progress.get("active_wall_seconds"),"worker_log_count":len(logs),
   "distinct_units_with_worker_logs":len(attempts_per_unit),"attempts_per_unit_histogram":hist,
   "cli_missing_mode_error_count":cli_errors,"unexpected_pilot_keyword_error_count":loader_errors,
   "worker_log_sha256_counts":log_hashes,"empty_worker_log_count":empty_logs,
   "worker_start_receipts_by_phase":started_by_phase,"result_status_by_phase":status_by_phase,
   "failed_result_patterns":[{"phase":phase,"failure_boundary":boundary,"failure_detail":detail,"count":count}
    for (phase,boundary,detail),count in sorted(failures.items())],
   "attempt_resource_receipt_count":len(resources),
   "failed_attempt_count":sum(x.get("status")=="FAILED" for x in resources),
   "known_failed_cpu_seconds":sum(float(x.get("process_tree_cpu_seconds",0.)) for x in resources if x.get("status")=="FAILED"),
   "known_failed_worker_wall_seconds":sum(float(x.get("worker_wall_seconds",0.)) for x in resources if x.get("status")=="FAILED"),
   "worker_main_entered":bool(started_by_phase),"classification":classification})
 return {"schema_id":"issue81-preexecution-recovery-audit-v1","packages":packages}

def analyze(root,package):
 outcomes=[]
 for seed in EVAL:
  for arm in ARMS:
   found=D.existing_success_record(root,D._row(seed,phase="heldout",arm=arm,formal=True,block=None),package)
   if not found: continue
   r,path=found; q=r.get("resource",{})
   outcomes.append({"owner_seed":seed,"arm":arm,"terminal_floor":int(r["terminal_floor"]),
   "terminal_outcome":r.get("terminal_outcome"),"terminal_act":int(r["terminal_act"]),
   "act2_reached":bool(r.get("act2_plus",False)),"act3_reached":bool(r.get("act3_plus",False)),
   "act4_reached":bool(r.get("act4_reached",False)),"heart_victory":bool(r.get("heart_victory",False)),
   "worker_cpu_seconds":float(q.get("worker_process_cpu_seconds",0.)),
   "worker_wall_seconds":float(q.get("worker_wall_seconds",0.)),"receipt_sha256":fsha(path)})
 per={}
 for a in ARMS:
  rs=[x for x in outcomes if x["arm"]==a]; fs=[x["terminal_floor"] for x in rs]
  per[a]={"owners":len(rs),"mean_terminal_floor":None if not fs else sum(fs)/len(fs),
  "terminal_floor_counts":{str(k):fs.count(k) for k in sorted(set(fs))},
  "act2_reached":sum(x["act2_reached"] for x in rs),"act3_reached":sum(x["act3_reached"] for x in rs),
  "act4_reached":sum(x["act4_reached"] for x in rs),"heart_victories":sum(x["heart_victory"] for x in rs),
  "worker_cpu_seconds":sum(x["worker_cpu_seconds"] for x in rs),"worker_wall_seconds_summed":sum(x["worker_wall_seconds"] for x in rs)}
 contrasts={}
 for a,b,n in (("JTRAIN","PGONLY","JTRAIN-PGONLY"),("PGONLY","BASE","PGONLY-BASE"),("JTRAIN","BASE","JTRAIN-BASE")):
  contrasts[n]={m:D._paired_bootstrap(outcomes,a,b,m) for m in ("terminal_floor","act2_reached","act3_reached","act4_reached","heart_victory")}
 return {"schema_id":"issue81-complete-game-analysis-v1","status":"COMPLETE" if len(outcomes)==288 else "INCOMPLETE",
 "owner_arm_count":len(outcomes),"registered_owner_arm_count":288,"primary_contrast":"JTRAIN-PGONLY terminal-floor paired owner bootstrap",
 "per_arm":per,"contrasts":contrasts,"evaluation_owner_arm_outcomes":outcomes,
 "noncombat_action_coverage":action_coverage(root,outcomes),"attempt_resources":attempts_summary(root),
 "preexecution_failure_summary":preexecution_failure_summary(),
 "training_updates":[readj(root/"models"/f"issue81-update-{i}.json") for i in (1,2,3) if (root/"models"/f"issue81-update-{i}.json").is_file()],
 "first_boss_qualification":{"act2_reach_is_directly_reported":True,"floor16_proves_first_boss_death":False,
 "terminal_hp_potions_deck":"not retained in compact #79 terminal receipt; not inferred"},
 "j_elite_mechanism_note":elite_note(),
 "limitations":["native surrogate with privileged training state; no live-game/public transfer claim",
 "matching owner seeds does not imply common post-action RNG","contrast is full training procedure difference, not fixed-data causal isolation of L_elite",
 "CI spanning zero is not equivalence","terminal floor 16 does not identify first-Boss cause",
 "native result action-family counts are reported only when exposed; unavailable values are not inferred"]}

def saved_batch(root,rows,package):
 results={}
 for row in rows:
  found=D.existing_success_record(root,row,package)
  if found is None: return None
  results[row["unit_id"]]=found[0]
 return {"status":"SUCCEEDED","completed":len(rows),"results":results}

def partial_unit_summary(root,rows,package):
 successes={arm:0 for arm in ARMS}; missing=[]; failures={}
 for row in rows:
  if D.existing_success(root,row,package) is not None:
   successes[row["arm"]]+=1; continue
  missing.append(row["unit_id"])
  directory=root/"units"/D.unit_key(row["unit_id"])
  attempts=sorted(directory.glob("attempt-*")) if directory.exists() else []
  if not attempts: failures["NOT_STARTED"]=failures.get("NOT_STARTED",0)+1
  for attempt in attempts:
   result_path=attempt/"result.json"
   if result_path.is_file():
    result=readj(result_path)
    if result.get("status")=="FAILED":
     key=f"{result.get('failure_boundary','UNKNOWN')}: {result.get('failure_detail','')}"
     failures[key]=failures.get(key,0)+1
   elif (attempt/"global-wall-stop.json").is_file():
    failures["GLOBAL_WALL_STOP_WITHOUT_TERMINAL"]=failures.get("GLOBAL_WALL_STOP_WITHOUT_TERMINAL",0)+1
 return {"successful_terminals_by_arm":successes,"successful_terminal_count":sum(successes.values()),
  "missing_unit_count":len(missing),"missing_unit_ids":missing,
  "failure_signatures":dict(sorted(failures.items())),"attempt_resources":attempts_summary(root)}

def finalize_evaluation_only(package,root,identity,active_seconds,smoke_batch,heldout_batch,plan):
 smoke_terminals=validate_terminal_batch(smoke_batch,plan["smoke"],formal=False)
 heldout_terminals=validate_terminal_batch(heldout_batch,plan["heldout"],formal=True)
 writej(root/"diagnostic-smoke-terminals.json",{"schema_id":"issue81-evaluation-only-smoke-v1",
  "efficacy_cohort":False,"terminals":smoke_terminals,"reused_prior_receipts":smoke_batch.get("reused_prior_receipts",{}),
  "attempt_resources":attempts_summary(root)})
 report=analyze(root,package)
 report.update({"execution_mode":"evaluation_only","package_commit":package,"source_lock_sha256":fsha(LOCK),
  "active_wall_seconds_before_terminal_completion":active_seconds,"supplemental_active_wall_allowance_seconds":SUPPLEMENTAL_ALLOWANCE,
  "original_failed_execution_active_wall_seconds":3784.857659886001,
  "combined_active_wall_seconds":3784.857659886001+active_seconds,
  "diagnostic_smoke_terminals":smoke_terminals,"verified_heldout_terminal_count":len(heldout_terminals),
  "loaded_model_identities":identity["models"],"evaluation_only_contract_sha256":fsha(EVALUATION_ONLY_CONTRACT),
  "limitations":report["limitations"]+["The evaluation-only amendment reuses completed PGONLY training and does not re-estimate training cost."]})
 if report["status"]!="COMPLETE" or report["owner_arm_count"]!=288:
  atomicj(root/"analysis-progress.json",report)
  out={"status":"INCOMPLETE_ANALYSIS","owner_arm_count":report["owner_arm_count"],"active_wall_seconds":active_seconds}
  atomicj(root/"evaluation-only-progress.json",{"schema_id":"issue81-evaluation-only-progress-v1",**out,"package_commit":package})
  return out
 writej(root/f"evaluation-only-analysis-{package}.json",report)
 compact={k:report[k] for k in ("schema_id","status","execution_mode","package_commit","source_lock_sha256",
  "owner_arm_count","primary_contrast","per_arm","contrasts","noncombat_action_coverage","attempt_resources",
  "training_updates","first_boss_qualification","j_elite_mechanism_note","preexecution_failure_summary","limitations",
  "active_wall_seconds_before_terminal_completion","supplemental_active_wall_allowance_seconds",
  "original_failed_execution_active_wall_seconds","combined_active_wall_seconds","diagnostic_smoke_terminals",
  "verified_heldout_terminal_count","loaded_model_identities","evaluation_only_contract_sha256")}
 compact["retained_raw_root"]=str(root); compact["durable_data_root"]=str(DATA)
 write_once(STUDY/"evaluation-only-results-summary.json",canon(compact)+b"\n")
 atomicj(root/"evaluation-only-progress.json",{"schema_id":"issue81-evaluation-only-progress-v1","status":"COMPLETE",
  "package_commit":package,"owner_arm_count":288,"active_wall_seconds":active_seconds,
  "supplemental_clock_id":SUPPLEMENTAL_CLOCK_ID,"allowance_seconds":SUPPLEMENTAL_ALLOWANCE,
  "smoke_terminal_count":3,"analysis_sha256":fsha(root/f"evaluation-only-analysis-{package}.json")})
 return compact

def run_evaluation_only():
 package=D.source_commit(); root=ART/"evaluation-only"/package; root.mkdir(parents=True,exist_ok=True)
 progress=root/"evaluation-only-progress.json"; plan=evaluation_only_plan()
 complete_smoke=saved_batch(root,plan["smoke"],package)
 complete_heldout=saved_batch(root,plan["heldout"],package)
 if complete_smoke is not None and complete_heldout is not None:
  identity=verify()
  clock_path=ART/f"active-wall-{SUPPLEMENTAL_CLOCK_ID}.json"
  clock_state=readj(clock_path) if clock_path.exists() else {"elapsed_seconds":0.0,"active_session":None}
  if clock_state.get("active_session"):
   clock=D.ActiveClock(SUPPLEMENTAL_CLOCK_ID,SUPPLEMENTAL_ALLOWANCE)
   actual=clock.close()
  else: actual=float(clock_state.get("elapsed_seconds",0.0))
  atomicj(progress,{"schema_id":"issue81-evaluation-only-progress-v1","status":"TERMINALS_COMPLETE",
   "package_commit":package,"owner_arm_count":288,"active_wall_seconds":actual,
   "supplemental_clock_id":SUPPLEMENTAL_CLOCK_ID,"allowance_seconds":SUPPLEMENTAL_ALLOWANCE})
  return finalize_evaluation_only(package,root,identity,actual,complete_smoke,complete_heldout,plan)
 closed=False
 clock=D.ActiveClock(SUPPLEMENTAL_CLOCK_ID,SUPPLEMENTAL_ALLOWANCE)
 try:
  identity=verify()
  binding={"schema_id":"issue81-evaluation-only-package-v1","mode":"evaluation_only","package_commit":package,
   "runner_code_commit":package,"source_lock_sha256":fsha(LOCK),
   "evaluation_only_contract_sha256":fsha(EVALUATION_ONLY_CONTRACT),
   "parent_failed_package_commit":"7ff0fcaf3fb53c6aaeb26a6b960345a320d25e51",
   "previous_evaluation_only_package_commit":PREVIOUS_EVALUATION_ONLY_PACKAGE_COMMIT,
   "parent_training_model_sha256":EXPECT["pg_sha"],"optimizer_checkpoint_sha256":EXPECT["pg_opt"],
   "clock_id":SUPPLEMENTAL_CLOCK_ID,"allowance_seconds":SUPPLEMENTAL_ALLOWANCE,
   "model_identities":identity["models"],"registered_seed_table_sha256":fsha(SEEDS)}
  writej(root/"evaluation-only-package.json",binding)
  atomicj(progress,{"schema_id":"issue81-evaluation-only-progress-v1","status":"VERIFIED",
   "package_commit":package,"source_lock_sha256":fsha(LOCK),"supplemental_clock_id":SUPPLEMENTAL_CLOCK_ID,
   "allowance_seconds":SUPPLEMENTAL_ALLOWANCE,"active_wall_seconds":clock.elapsed(),"identity":identity,
   "smoke_seeds":list(SMOKE_SEEDS),"heldout_units":len(plan["heldout"])})
  smoke=run_smoke_once(plan["smoke"],root=root,clock=clock,package_commit=package)
  if smoke.get("status")!="SUCCEEDED" or smoke.get("completed")!=len(plan["smoke"]):
   out={"status":"SMOKE_INCOMPLETE","smoke_expected":len(plan["smoke"]),
    "active_wall_seconds":clock.elapsed(),"attempt_summary":partial_unit_summary(root,plan["smoke"],package),
    "reused_prior_receipts":smoke.get("reused_prior_receipts",{})}
   atomicj(progress,{"schema_id":"issue81-evaluation-only-progress-v1",**out,"package_commit":package,
    "supplemental_clock_id":SUPPLEMENTAL_CLOCK_ID,"allowance_seconds":SUPPLEMENTAL_ALLOWANCE})
   return out
  smoke_terminals=validate_terminal_batch(smoke,plan["smoke"],formal=False)
  writej(root/"diagnostic-smoke-terminals.json",{"schema_id":"issue81-evaluation-only-smoke-v1",
   "efficacy_cohort":False,"terminals":smoke_terminals,"reused_prior_receipts":smoke.get("reused_prior_receipts",{}),
   "attempt_resources":attempts_summary(root)})
  atomicj(progress,{"schema_id":"issue81-evaluation-only-progress-v1","status":"SMOKE_COMPLETE",
   "package_commit":package,"smoke_terminals":smoke_terminals,"active_wall_seconds":clock.elapsed(),
   "supplemental_clock_id":SUPPLEMENTAL_CLOCK_ID,"allowance_seconds":SUPPLEMENTAL_ALLOWANCE})
  heldout=D.run_units(plan["heldout"],root=root,pilot=False,clock=clock,package_commit=package)
  if heldout.get("status")!="SUCCEEDED" or heldout.get("completed")!=288:
   out={"status":"INCOMPLETE_HELDOUT","heldout_expected":288,
    "active_wall_seconds":clock.elapsed(),"allowance_seconds":SUPPLEMENTAL_ALLOWANCE,
    "smoke_terminal_count":len(smoke_terminals),
    "attempt_summary":partial_unit_summary(root,plan["heldout"],package)}
   atomicj(progress,{"schema_id":"issue81-evaluation-only-progress-v1",**out,"package_commit":package,
    "supplemental_clock_id":SUPPLEMENTAL_CLOCK_ID})
   return out
  heldout_terminals=validate_terminal_batch(heldout,plan["heldout"],formal=True)
  actual=clock.close(); closed=True
  atomicj(progress,{"schema_id":"issue81-evaluation-only-progress-v1","status":"TERMINALS_COMPLETE",
   "package_commit":package,"owner_arm_count":288,"active_wall_seconds":actual,
   "supplemental_clock_id":SUPPLEMENTAL_CLOCK_ID,"allowance_seconds":SUPPLEMENTAL_ALLOWANCE,
   "smoke_terminal_count":len(smoke_terminals),"heldout_terminal_count":len(heldout_terminals)})
  return finalize_evaluation_only(package,root,identity,actual,smoke,heldout,plan)
 except BaseException as e:
  atomicj(progress,{"schema_id":"issue81-evaluation-only-progress-v1","status":"FAILED",
   "package_commit":package,"active_wall_seconds":clock.elapsed(),"supplemental_clock_id":SUPPLEMENTAL_CLOCK_ID,
   "allowance_seconds":SUPPLEMENTAL_ALLOWANCE,"failure_boundary":type(e).__name__,
   "detail":str(e)[:4096],"traceback":traceback.format_exc()[-12000:]})
  raise
 finally:
  if not closed: clock.close()

def main():
 p=argparse.ArgumentParser(); p.add_argument("--mode",choices=("prepare","verify","evaluation-only")); p.add_argument("--worker",type=Path); a=p.parse_args()
 if a.worker: return worker(a.worker)
 if a.mode is None: p.error("--mode is required unless --worker is used")
 if a.mode=="prepare": print(json.dumps(prepare(),sort_keys=True)); return 0
 if a.mode=="verify": print(json.dumps(verify(),sort_keys=True)); return 0
 result=run_evaluation_only(); print(json.dumps(result,sort_keys=True)); return 0 if result.get("status")=="COMPLETE" else 2
if __name__=="__main__": raise SystemExit(main())
