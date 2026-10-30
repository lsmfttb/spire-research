from __future__ import annotations
import argparse, gc, hashlib, importlib.util, json, os, resource, subprocess, sys, tarfile, time, traceback, zlib
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
RESUME_CLOCK_ID="issue81-heldout-resume-20261030"; RESUME_ALLOWANCE=1500.0
SMOKE_SEEDS=(7699001,7699002,7699003)
PGONLY_ROOT=ART/"formal"/"7ff0fcaf3fb53c6aaeb26a6b960345a320d25e51"
EVALUATION_ONLY_CONTRACT=STUDY/"evaluation-only-contract.json"
PREVIOUS_PACKAGES=("a7d6df2ee635ccd14b37365c21b5a4bafabb5379","a623cb1087221b953bd100099077c0effb6f34b3","7ff0fcaf3fb53c6aaeb26a6b960345a320d25e51")
PREVIOUS_EVALUATION_ONLY_PACKAGE_COMMIT="a484175828868da5de7147694122b5ff41da4d25"
PREVIOUS_TERMINAL_PACKAGE_COMMIT="fa729cd82120e0836822b21c87899e72dffa7d38"
PREVIOUS_TERMINAL_LOCK_SHA256="89e10436b334806546631b5e7146fd50346c61d91012cb1278eb94e99d539c6a"
PREVIOUS_TERMINAL_COUNT=205
PREVIOUS_TERMINALS_BY_ARM={"BASE":69,"PGONLY":68,"JTRAIN":68}
CONTINUATION_CONTRACT=STUDY/"evaluation-only-continuation-contract.json"
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

def continuation_contract():
 return {"schema_id":"issue81-heldout-continuation-v1","study_id":"issue-81-pgonly-j-elite-ablation",
  "authorization_comment":"https://github.com/lsmfttb/spire-research/issues/81#issuecomment-6511295849",
  "execution_mode":"missing_heldout_units_only","clock":{"id":RESUME_CLOCK_ID,"allowance_seconds":RESUME_ALLOWANCE,
   "one_global_active_program_wall_clock":True,"includes_attempts_retries_and_closeout":True,"overlap_counted_once":True},
  "prior_package":{"commit":PREVIOUS_TERMINAL_PACKAGE_COMMIT,"source_lock_sha256":PREVIOUS_TERMINAL_LOCK_SHA256,
   "verified_terminal_count":PREVIOUS_TERMINAL_COUNT,"terminals_by_arm":PREVIOUS_TERMINALS_BY_ARM,
   "read_only_mapping":True,"preserve_all_prior_files":True},
  "heldout":{"seed_table_sha256":fsha(SEEDS),"owners":96,"arms":list(ARMS),"registered_terminal_count":288,
   "run_only_missing_count":83,"reuse_existing_successes":True,"no_seed_replacement":True,"online_search":"none"},
  "smoke":{"reuse_verified_existing_receipts":True,"run_new_smoke":False,"efficacy_cohort":False},
  "training":{"repeat_training":False,"optimizer_updates":False,"models_immutable":True},
  "stop":{"no_automatic_extension":True,"if_allowance_expires_stop_and_report_missing_units":True}}

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
 write_once(CONTINUATION_CONTRACT,canon(continuation_contract())+b"\n")
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
 "heldout_continuation":{"clock_id":RESUME_CLOCK_ID,"allowance_seconds":RESUME_ALLOWANCE,
  "prior_package_commit":PREVIOUS_TERMINAL_PACKAGE_COMMIT,"prior_source_lock_sha256":PREVIOUS_TERMINAL_LOCK_SHA256,
  "reuse_existing_terminals":True,"new_smoke":False,"new_training":False},
 "code_sha256":fsha(STUDY/"driver.py"),"launcher_sha256":fsha(STUDY/"run_issue81.sh"),"contract_sha256":fsha(CONTRACT),
 "evaluation_only_contract_sha256":fsha(EVALUATION_ONLY_CONTRACT),
 "continuation_contract_sha256":fsha(CONTINUATION_CONTRACT),
 "seed_table_sha256":fsha(SEEDS),"config_sha256":fsha(STUDY/"config.json"),
 "stage1_config_sha256":fsha(STAGE1/"config.json"),"allowance_seconds":ALLOWANCE,"strict_elite_only_removal":True}
 if LOCK.exists():
  rel=LOCK.resolve().relative_to(REPO.resolve()).as_posix()
  committed=subprocess.run(["git","show",f"HEAD:{rel}"],cwd=REPO,check=True,stdout=subprocess.PIPE).stdout
  if LOCK.read_bytes()!=committed:
   raise RuntimeError("refusing to replace a locally modified source lock")
 atomicj(LOCK,lock)
 return {"status":"PREPARED","baseline":BASE,"source_lock_sha256":fsha(LOCK),"seed_rows":len(seed_table()["rows"]),"data_path":str(DATA)}

def check_lock():
 l=readj(LOCK)
 for key,path in (("code_sha256",STUDY/"driver.py"),("launcher_sha256",STUDY/"run_issue81.sh"),("contract_sha256",CONTRACT),("seed_table_sha256",SEEDS),
 ("config_sha256",STUDY/"config.json"),("stage1_config_sha256",STAGE1/"config.json"),("evaluation_only_contract_sha256",EVALUATION_ONLY_CONTRACT),("continuation_contract_sha256",CONTINUATION_CONTRACT),
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

def git_file_bytes(commit,path):
 rel=Path(path).resolve().relative_to(REPO).as_posix()
 return subprocess.run(["git","show",f"{commit}:{rel}"],cwd=REPO,check=True,stdout=subprocess.PIPE).stdout

def verify_historical_source_lock(package_commit,expected_lock_sha256):
 raw=git_file_bytes(package_commit,LOCK)
 if sha(raw)!=expected_lock_sha256:
  raise RuntimeError(f"historical source lock SHA mismatch for {package_commit}: {sha(raw)}")
 lock=json.loads(raw)
 for key,path in (("code_sha256",STUDY/"driver.py"),("launcher_sha256",STUDY/"run_issue81.sh"),
                  ("contract_sha256",CONTRACT),("evaluation_only_contract_sha256",EVALUATION_ONLY_CONTRACT),
                  ("seed_table_sha256",SEEDS),("config_sha256",STUDY/"config.json"),
                  ("stage1_config_sha256",STAGE1/"config.json")):
  if key in lock and sha(git_file_bytes(package_commit,path))!=lock[key]:
   raise RuntimeError(f"historical source file does not match {package_commit} lock: {key}")
 return lock

def expected_arm_identity(arm):
 return {"BASE":("pi1",EXPECT["pi1_sha"],EXPECT["pi1_digest"]),
         "PGONLY":("pi4",EXPECT["pg_sha"],EXPECT["pg_digest"]),
         "JTRAIN":("jtrain",EXPECT["j_sha"],EXPECT["j_digest"])}[arm]

def audit_saved_success(root,row,package_commit,lock_sha256,package_binding):
 unit=row["unit_id"]; base=root/"units"/D.unit_key(unit)
 results=[]
 for result_path in sorted(base.glob("attempt-*/result.json")) if base.exists() else []:
  result=readj(result_path)
  if result.get("status")=="SUCCEEDED": results.append((result_path,result))
 if len(results)>1: raise RuntimeError(f"multiple successful receipts for {unit} in {root}")
 if not results: return None
 result_path,result=results[0]; attempt=result_path.parent
 source,checkpoint,model_digest=expected_arm_identity(row["arm"])
 for key,value in (("unit_id",unit),("owner_seed",row["owner_seed"]),("game_seed",row["game_seed"]),
                   ("phase",row["phase"]),("arm",row["arm"]),("formal",row["formal"]),
                   ("paired_owner_key",row["paired_owner_key"]),("package_commit",package_commit),
                   ("runner_code_commit",package_commit),("source_lock_sha256",lock_sha256),
                   ("model_digest",model_digest),("native_module_sha256",EXPECT["native"]),
                   ("native_source_commit",EXPECT["sts"]),("stsr_source_commit",EXPECT["stsr"])):
  if result.get(key)!=value: raise RuntimeError(f"successful receipt field {key} mismatch for {unit}: {result_path}")
 policy=result.get("policy_source")
 if not isinstance(policy,dict) or policy.get("source")!=source or policy.get("checkpoint_sha256")!=checkpoint or policy.get("formal_use") is not True:
  raise RuntimeError(f"policy/checkpoint binding mismatch for {unit}: {result_path}")
 if result.get("expert_decisions")!=0 or result.get("search")!=[]:
  raise RuntimeError(f"forbidden Expert or online-search evidence for {unit}: {result_path}")
 if result.get("terminal_outcome") is None or isinstance(result.get("terminal_floor"),bool) or not isinstance(result.get("terminal_floor"),int):
  raise RuntimeError(f"actual terminal evidence missing for {unit}: {result_path}")
 if isinstance(result.get("terminal_act"),bool) or not isinstance(result.get("terminal_act"),int):
  raise RuntimeError(f"terminal Act missing for {unit}: {result_path}")
 job_path=attempt/"worker-job.json"; started_path=attempt/"started.json"
 finished_path=attempt/"finished.json"; resource_path=attempt/"attempt-resource.json"
 for path in (job_path,started_path,finished_path,resource_path):
  if not path.is_file(): raise RuntimeError(f"receipt chain missing {path.name} for {unit}: {result_path}")
 job=readj(job_path); finished=readj(finished_path); resource=readj(resource_path); started=readj(started_path)
 expected_root=str(root.resolve()); expected_attempt=str(attempt.resolve())
 if job.get("row")!=row or job.get("unit_id")!=unit or job.get("package_commit")!=package_commit or job.get("runner_code_commit")!=package_commit or job.get("source_lock_sha256")!=lock_sha256:
  raise RuntimeError(f"registered stream or worker source binding mismatch for {unit}: {job_path}")
 if job.get("artifact_root")!=expected_root or job.get("attempt_dir")!=expected_attempt or job.get("result_path")!=str(result_path.resolve()):
  raise RuntimeError(f"worker paths do not identify immutable receipt files for {unit}: {job_path}")
 if job.get("run_id")!=finished.get("run_id") or job.get("run_id")!=resource.get("run_id") or job.get("run_id")!=result.get("resource",{}).get("ledger_run_id"):
  raise RuntimeError(f"worker run ID chain mismatch for {unit}: {job_path}")
 if started.get("unit_id")!=unit or started.get("run_id")!=job.get("run_id") or started.get("package_commit")!=package_commit:
  raise RuntimeError(f"worker start receipt mismatch for {unit}: {started_path}")
 if finished.get("status")!="SUCCEEDED" or finished.get("unit_id")!=unit or finished.get("result_sha256")!=fsha(result_path):
  raise RuntimeError(f"finished receipt hash/status mismatch for {unit}: {finished_path}")
 if resource.get("status")!="SUCCEEDED" or resource.get("unit_id")!=unit or resource.get("exit_code")!=0:
  raise RuntimeError(f"worker resource receipt mismatch for {unit}: {resource_path}")
 provenance={"unit_id":unit,"owner_seed":row["owner_seed"],"arm":row["arm"],
  "source_package_commit":package_commit,"source_lock_sha256":lock_sha256,
  "result_path_under_data_root":result_path.resolve().relative_to(DATA.resolve()).as_posix(),
  "result_sha256":fsha(result_path),"worker_job_sha256":fsha(job_path),
  "started_receipt_sha256":fsha(started_path),"finished_receipt_sha256":fsha(finished_path),
  "attempt_resource_sha256":fsha(resource_path),
  "natural_owner_observation_sha256":fsha(attempt/"natural-owner-observation.json") if (attempt/"natural-owner-observation.json").is_file() else None}
 return result,result_path,provenance

def verify_package_binding(root,package_commit,lock_sha256):
 binding=readj(root/"evaluation-only-package.json")
 if binding.get("schema_id")!="issue81-evaluation-only-package-v1" or binding.get("package_commit")!=package_commit or binding.get("runner_code_commit")!=package_commit or binding.get("source_lock_sha256")!=lock_sha256:
  raise RuntimeError(f"package/source binding mismatch: {root}")
 lock=verify_historical_source_lock(package_commit,lock_sha256)
 if lock.get("runtime",{}).get("native_module_sha256")!=EXPECT["native"] or lock.get("runtime",{}).get("sts_lightspeed_commit")!=EXPECT["sts"] or lock.get("runtime",{}).get("stsr_source_commit")!=EXPECT["stsr"]:
  raise RuntimeError(f"historical package native/source identity mismatch: {package_commit}")
 if binding.get("registered_seed_table_sha256")!=fsha(SEEDS): raise RuntimeError("package seed table mismatch")
 models=binding.get("model_identities",{})
 expected={"pi1_sha256":EXPECT["pi1_sha"],"pi1_digest":EXPECT["pi1_digest"],
  "pi1_optimizer_sha256":EXPECT["pi1_opt"],"pgonly_sha256":EXPECT["pg_sha"],
  "pgonly_digest":EXPECT["pg_digest"],"pgonly_optimizer_sha256":EXPECT["pg_opt"],
  "jtrain_sha256":EXPECT["j_sha"],"jtrain_digest":EXPECT["j_digest"],
  "jtrain_optimizer_sha256":EXPECT["j_opt"]}
 if any(models.get(k)!=v for k,v in expected.items()): raise RuntimeError(f"package model identities mismatch: {package_commit}")
 if package_commit not in (PREVIOUS_TERMINAL_PACKAGE_COMMIT,PREVIOUS_EVALUATION_ONLY_PACKAGE_COMMIT):
  if binding.get("continuation_contract_sha256")!=fsha(CONTINUATION_CONTRACT) or binding.get("clock_id")!=RESUME_CLOCK_ID or binding.get("allowance_seconds")!=RESUME_ALLOWANCE:
   raise RuntimeError("continuation package clock/contract binding mismatch")
 return binding

def prior_heldout_records(rows):
 root=ART/"evaluation-only"/PREVIOUS_TERMINAL_PACKAGE_COMMIT
 binding=verify_package_binding(root,PREVIOUS_TERMINAL_PACKAGE_COMMIT,PREVIOUS_TERMINAL_LOCK_SHA256)
 progress=readj(root/"evaluation-only-progress.json")
 summary=progress.get("attempt_summary",{})
 if progress.get("status")!="INCOMPLETE_HELDOUT" or summary.get("successful_terminal_count")!=PREVIOUS_TERMINAL_COUNT or summary.get("successful_terminals_by_arm")!=PREVIOUS_TERMINALS_BY_ARM:
  raise RuntimeError("previous package progress does not bind the registered 205-terminal stop state")
 records={}; missing=[]; manifest=[]; by_arm={a:0 for a in ARMS}
 for row in rows:
  found=audit_saved_success(root,row,PREVIOUS_TERMINAL_PACKAGE_COMMIT,PREVIOUS_TERMINAL_LOCK_SHA256,binding)
  if found is None:
   missing.append(row["unit_id"]); continue
  result,path,provenance=found; records[row["unit_id"]]=(result,path,provenance); manifest.append(provenance); by_arm[row["arm"]]+=1
 if len(records)!=PREVIOUS_TERMINAL_COUNT or by_arm!=PREVIOUS_TERMINALS_BY_ARM:
  raise RuntimeError(f"previous terminal audit count mismatch: total={len(records)} by_arm={by_arm}")
 all_success_paths={}
 for path in root.glob("units/*/attempt-*/result.json"):
  report=readj(path)
  if report.get("status")=="SUCCEEDED":
   unit=report.get("unit_id")
   if unit in all_success_paths: raise RuntimeError(f"duplicate successful terminal unit in historical package: {unit}")
   all_success_paths[unit]=path
 registered_smoke_ids={r["unit_id"] for r in evaluation_only_plan()["smoke"]
  if r["arm"] in ("PGONLY","JTRAIN")}
 expected_success_ids=set(records)|registered_smoke_ids
 if set(all_success_paths)!=expected_success_ids or any(all_success_paths[k]!=records[k][1] for k in records):
  raise RuntimeError("historical package contains an unregistered or unmapped successful terminal receipt")
 declared=summary.get("missing_unit_ids",[])
 if sorted(missing)!=sorted(declared) or len(missing)!=83:
  raise RuntimeError("previous terminal audit missing-unit set differs from its immutable progress receipt")
 return records,manifest,missing,{"package_commit":PREVIOUS_TERMINAL_PACKAGE_COMMIT,
  "source_lock_sha256":PREVIOUS_TERMINAL_LOCK_SHA256,"progress_sha256":fsha(root/"evaluation-only-progress.json"),
  "package_manifest_sha256":fsha(root/"evaluation-only-package.json"),"terminal_mapping_count":len(records),
  "terminal_mapping_sha256":sha(canon(sorted(manifest,key=lambda x:x["unit_id"])))}

def prior_smoke_batch(rows):
 root=ART/"evaluation-only"/PREVIOUS_TERMINAL_PACKAGE_COMMIT
 smoke_manifest=readj(root/"diagnostic-smoke-terminals.json")
 if smoke_manifest.get("efficacy_cohort") is not False: raise RuntimeError("prior diagnostic smoke is incorrectly marked as efficacy data")
 terminal_rows={x["unit_id"]:x for x in smoke_manifest.get("terminals",[])}
 if set(terminal_rows)!={r["unit_id"] for r in rows}: raise RuntimeError("prior smoke terminal set differs from registered nonformal arms")
 results={}; provenance=[]
 for row in rows:
  if row["arm"]=="BASE":
   source_root=ART/"evaluation-only"/PREVIOUS_EVALUATION_ONLY_PACKAGE_COMMIT
   source_commit=PREVIOUS_EVALUATION_ONLY_PACKAGE_COMMIT; lock_sha=PREVIOUS_BASE_SMOKE_SOURCE_LOCK_SHA256
  else:
   source_root=root; source_commit=PREVIOUS_TERMINAL_PACKAGE_COMMIT; lock_sha=PREVIOUS_TERMINAL_LOCK_SHA256
  binding=verify_package_binding(source_root,source_commit,lock_sha)
  found=audit_saved_success(source_root,row,source_commit,lock_sha,binding)
  if found is None: raise RuntimeError(f"prior smoke terminal missing for {row['unit_id']}")
  result,path,prov=found
  summary=terminal_rows[row["unit_id"]]
  for key in ("unit_id","owner_seed","arm","formal","terminal_outcome","terminal_floor","terminal_act","model_digest"):
   actual=result.get(key); displayed=summary.get(key)
   if actual!=displayed: raise RuntimeError(f"prior smoke terminal summary mismatch for {row['unit_id']}: {key}")
  results[row["unit_id"]]=result; provenance.append(prov)
  if row["arm"]=="BASE":
   old=smoke_manifest.get("reused_prior_receipts",{}).get(row["unit_id"],{})
   if old.get("result_sha256")!=prov["result_sha256"] or old.get("source_lock_sha256")!=lock_sha:
    raise RuntimeError("reused BASE smoke provenance differs from its immutable manifest")
 batch={"status":"SUCCEEDED","completed":len(rows),"results":results,
  "reused_prior_receipts":{x["unit_id"]:x for x in provenance}}
 validate_terminal_batch(batch,rows,formal=False)
 return batch,{"prior_smoke_manifest_sha256":fsha(root/"diagnostic-smoke-terminals.json"),
  "terminal_mapping_count":len(provenance),"terminal_mapping":provenance}

def current_heldout_records(root,rows,package,package_binding):
 records={}
 for row in rows:
  found=D.existing_success_record(root,row,package)
  if found is None: continue
  result,path=found
  checked=audit_saved_success(root,row,package,D.source_lock_sha(),package_binding)
  if checked is None or checked[1]!=path: raise RuntimeError(f"current package receipt verification failed: {row['unit_id']}")
  records[row["unit_id"]]=checked
 all_success_paths={}
 for path in root.glob("units/*/attempt-*/result.json"):
  report=readj(path)
  if report.get("status")=="SUCCEEDED":
   unit=report.get("unit_id")
   if unit in all_success_paths: raise RuntimeError(f"duplicate current terminal receipt: {unit}")
   all_success_paths[unit]=path
 if set(all_success_paths)!=set(records) or any(all_success_paths[k]!=records[k][1] for k in records):
  raise RuntimeError("current package contains an unregistered or unmapped successful result")
 return records

def combined_attempt_summary(current_root):
 roots=(ART/"evaluation-only"/PREVIOUS_TERMINAL_PACKAGE_COMMIT,current_root)
 out={"sidecar_attempts":0,"succeeded":0,"failed":0,"known_cpu_seconds":0.0,"known_worker_wall_seconds":0.0,
  "unknown_wall_killed_attempts":[],"packages":{}}
 for root in roots:
  package=root.name; item={"sidecar_attempts":0,"succeeded":0,"failed":0,"known_cpu_seconds":0.0,"known_worker_wall_seconds":0.0,"unknown_wall_killed_attempts":[]}
  for row_seed in EVAL:
   for arm in ARMS:
    unit=f"EVAL:{row_seed}:{arm}"; directory=root/"units"/D.unit_key(unit)
    for attempt in sorted(directory.glob("attempt-*")) if directory.exists() else []:
     resource_path=attempt/"attempt-resource.json"
     if resource_path.is_file():
      r=readj(resource_path); item["sidecar_attempts"]+=1
      item["succeeded"]+=r.get("status")=="SUCCEEDED"; item["failed"]+=r.get("status")=="FAILED"
      item["known_cpu_seconds"]+=float(r.get("process_tree_cpu_seconds",0.0))
      item["known_worker_wall_seconds"]+=float(r.get("worker_wall_seconds",0.0))
     elif (attempt/"global-wall-stop.json").is_file():
      item["unknown_wall_killed_attempts"].append((attempt/"global-wall-stop.json").relative_to(root).as_posix())
  out["packages"][package]=item
  for k in ("sidecar_attempts","succeeded","failed","known_cpu_seconds","known_worker_wall_seconds"):
   out[k]+=item[k]
  out["unknown_wall_killed_attempts"].extend(item["unknown_wall_killed_attempts"])
 return out

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

def summarize_training_feature(payload, alg, ex):
 example=ex._training_example_from_payload(payload); byroot={}; roots=superior=labels=0
 for trajectory in example.joint_trajectories:
  if trajectory.complete and trajectory.decisions:
   byroot.setdefault(str(trajectory.search_root_id or trajectory.decisions[0].state_fingerprint),[]).append(trajectory)
 for trajectories in byroot.values():
  roots+=1; candidate=alg.elite_candidate(example.base,trajectories)
  if candidate is not None: superior+=1; labels+=len(candidate.decisions)
 return roots,superior,labels

def elite_note():
 if not RAW79.is_file(): return {"status":"SOURCE_UNAVAILABLE"}
 alg,rt,m,l,ex=D.load_modules(ART/"verify"); seen=set(); roots=superior=labels=searches=0; packages=set()
 with tarfile.open(RAW79,"r:") as tar:
  members=tar.getmembers(); files={member.name:member for member in members if member.isfile()}
  feats=[member for member in members if member.isfile() and member.name.endswith("training-features.json.zlib") and any(f"TRAIN-{seed}" in member.name for seed in TRAIN)]
  for feature_member in feats:
   prefix=feature_member.name.rsplit("/",1)[0]; result_member=files.get(prefix+"/result.json")
   if result_member is None: continue
   report=json.loads(tar.extractfile(result_member).read()); seed=int(report.get("owner_seed",-1))
   if report.get("status")!="SUCCEEDED" or report.get("phase")!="training" or report.get("arm")!="TRAIN" or report.get("formal") is not True or seed not in TRAIN or seed in seen: continue
   zipped=tar.extractfile(feature_member).read()
   if report.get("training_features",{}).get("sha256")!=sha(zipped): continue
   payload=json.loads(zlib.decompress(zipped))
   if payload.get("owner_seed")!=seed or payload.get("unit_id")!=f"TRAIN:{seed}": continue
   root_count,superior_count,label_count=summarize_training_feature(payload,alg,ex)
   roots+=root_count; superior+=superior_count; labels+=label_count
   searches+=len(payload.get("search_receipts",[])); packages.add(str(report.get("package_commit"))); seen.add(seed)
   del payload,zipped,report
   if len(seen)%8==0: gc.collect()
 analysis_commit=subprocess.run(["git","rev-parse","HEAD"],cwd=REPO,check=True,text=True,capture_output=True).stdout.strip()
 return {"status":"COMPLETE" if len(seen)==384 else "INCOMPLETE_SOURCE_COVERAGE",
 "registered_training_owners":384,"owners_with_successful_source_features":len(seen),"missing_owners":384-len(seen),
 "source_package_commits":sorted(packages),"successful_J_search_root_receipts":searches,
 "complete_joint_roots_considered":roots,"roots_with_strictly_superior_elite":superior,
 "selected_strictly_superior_elite_decisions":labels,"method":"retained #79 successful training receipts and feature payloads; source elite_candidate grouped by original J root",
 "analysis_implementation":{"commit":analysis_commit,"driver_sha256":fsha(Path(__file__).resolve()),
  "memory_strategy":"process one decompressed owner payload at a time"}}

def attempts_summary(root):
 arr=[readj(p) for p in sorted(root.glob("units/**/attempt-resource.json"))]
 unk=[p.relative_to(root).as_posix() for p in root.glob("units/**/attempt-*/global-wall-stop.json") if not (p.parent/"attempt-resource.json").is_file()]
 return {"sidecar_attempts":len(arr),"succeeded":sum(x["status"]=="SUCCEEDED" for x in arr),
 "failed":sum(x["status"]=="FAILED" for x in arr),"known_cpu_seconds":sum(float(x["process_tree_cpu_seconds"]) for x in arr),
 "known_worker_wall_seconds":sum(float(x["worker_wall_seconds"]) for x in arr),"unknown_wall_killed_attempts":unk}

def action_coverage(root,outcomes,records):
 sums={}
 for arm in ARMS:
  kinds={}; screens={}; owners=kind_owners=screen_owners=0
  for o in outcomes:
   if o["arm"]!=arm: continue
   unit=f"EVAL:{o['owner_seed']}:{arm}"
   found=records.get(unit)
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

def analyze_continuation(root,package,records,smoke_batch,mapping_manifest):
 outcomes=[]
 for seed in EVAL:
  for arm in ARMS:
   unit=f"EVAL:{seed}:{arm}"; found=records.get(unit)
   if not found: continue
   r,path,provenance=found; q=r.get("resource",{})
   outcomes.append({"owner_seed":seed,"arm":arm,"terminal_floor":int(r["terminal_floor"]),
    "terminal_outcome":r.get("terminal_outcome"),"terminal_act":int(r["terminal_act"]),
    "act2_reached":bool(r.get("act2_plus",False)),"act3_reached":bool(r.get("act3_plus",False)),
    "act4_reached":bool(r.get("act4_reached",False)),"heart_victory":bool(r.get("heart_victory",False)),
    "worker_cpu_seconds":float(q.get("worker_process_cpu_seconds",0.)),
    "worker_wall_seconds":float(q.get("worker_wall_seconds",0.)),
    "receipt_sha256":fsha(path),"source_package_commit":provenance["source_package_commit"],
    "source_lock_sha256":provenance["source_lock_sha256"]})
 per={}
 for a in ARMS:
  rs=[x for x in outcomes if x["arm"]==a]; fs=[x["terminal_floor"] for x in rs]
  per[a]={"owners":len(rs),"mean_terminal_floor":None if not fs else sum(fs)/len(fs),
   "terminal_floor_counts":{str(k):fs.count(k) for k in sorted(set(fs))},
   "act2_reached":sum(x["act2_reached"] for x in rs),"act3_reached":sum(x["act3_reached"] for x in rs),
   "act4_reached":sum(x["act4_reached"] for x in rs),"heart_victories":sum(x["heart_victory"] for x in rs),
   "worker_cpu_seconds":sum(x["worker_cpu_seconds"] for x in rs),
   "worker_wall_seconds_summed":sum(x["worker_wall_seconds"] for x in rs)}
 contrasts={}
 for a,b,n in (("JTRAIN","PGONLY","JTRAIN-PGONLY"),("PGONLY","BASE","PGONLY-BASE"),("JTRAIN","BASE","JTRAIN-BASE")):
  contrasts[n]={m:D._paired_bootstrap(outcomes,a,b,m) for m in ("terminal_floor","act2_reached","act3_reached","act4_reached","heart_victory")}
 smoke_terminals=validate_terminal_batch(smoke_batch,evaluation_only_plan()["smoke"],formal=False)
 return {"schema_id":"issue81-complete-game-analysis-v1",
  "status":"COMPLETE" if len(outcomes)==288 else "INCOMPLETE",
  "execution_mode":"missing_heldout_continuation","package_commit":package,
  "owner_arm_count":len(outcomes),"registered_owner_arm_count":288,
  "primary_contrast":"JTRAIN-PGONLY terminal-floor paired owner bootstrap",
  "per_arm":per,"contrasts":contrasts,"evaluation_owner_arm_outcomes":outcomes,
  "noncombat_action_coverage":action_coverage(root,outcomes,records),
  "attempt_resources":combined_attempt_summary(root),
  "preexecution_failure_summary":preexecution_failure_summary(),
  "training_updates":[readj(PGONLY_ROOT/"models"/f"issue81-update-{i}.json")
   for i in (1,2,3) if (PGONLY_ROOT/"models"/f"issue81-update-{i}.json").is_file()],
  "first_boss_qualification":{"act2_reach_is_directly_reported":True,"floor16_proves_first_boss_death":False,
   "terminal_hp_potions_deck":"not retained in compact #79 terminal receipt; not inferred"},
  "j_elite_mechanism_note":elite_note(),"diagnostic_smoke_terminals":smoke_terminals,
  "prior_terminal_mapping":{"count":len(mapping_manifest),"sha256":sha(canon(sorted(mapping_manifest,key=lambda x:x["unit_id"])))},
  "limitations":["native surrogate with privileged training state; no live-game/public transfer claim",
   "matching owner seeds does not imply common post-action RNG","contrast is full training procedure difference, not fixed-data causal isolation of L_elite",
   "CI spanning zero is not equivalence","terminal floor 16 does not identify first-Boss cause",
   "native result action-family counts are reported only when exposed; unavailable values are not inferred"]}

def partial_continuation_summary(root,rows,records):
 missing=[r["unit_id"] for r in rows if r["unit_id"] not in records]
 failures={}
 for unit in missing:
  directory=root/"units"/D.unit_key(unit)
  attempts=sorted(directory.glob("attempt-*")) if directory.exists() else []
  if not attempts:
   failures["NOT_STARTED"]=failures.get("NOT_STARTED",0)+1
  for attempt in attempts:
   result_path=attempt/"result.json"
   if result_path.is_file():
    result=readj(result_path)
    if result.get("status")=="FAILED":
     key=f"{result.get('failure_boundary','UNKNOWN')}: {result.get('failure_detail','')}"
     failures[key]=failures.get(key,0)+1
   elif (attempt/"global-wall-stop.json").is_file():
    failures["GLOBAL_WALL_STOP_WITHOUT_TERMINAL"]=failures.get("GLOBAL_WALL_STOP_WITHOUT_TERMINAL",0)+1
 return {"successful_terminals_by_arm":{a:sum(1 for x in records.values() if x[0].get("arm")==a) for a in ARMS},
  "successful_terminal_count":len(records),"registered_terminal_count":len(rows),
  "missing_unit_count":len(missing),"missing_unit_ids":missing,
  "failure_signatures":dict(sorted(failures.items())),"attempt_resources":combined_attempt_summary(root)}

def finalize_continuation(package,root,identity,clock,prior_records,current_records,prior_mapping,smoke_batch,plan):
 all_records=dict(prior_records)
 overlap=set(all_records).intersection(current_records)
 if overlap: raise RuntimeError(f"duplicate prior/current terminal receipts: {sorted(overlap)[:3]}")
 all_records.update(current_records)
 missing=[r["unit_id"] for r in plan["heldout"] if r["unit_id"] not in all_records]
 if len(all_records)+len(missing)!=288: raise RuntimeError("merged terminal accounting is not exactly 288")
 mapping=[v[2] for v in all_records.values()]
 report=analyze_continuation(root,package,all_records,smoke_batch,[v[2] for v in prior_records.values()])
 if report["owner_arm_count"]!=len(all_records): raise RuntimeError("analysis and receipt mapping counts diverged")
 if missing:
  return {"status":"INCOMPLETE_HELDOUT","package_commit":package,"source_lock_sha256":fsha(LOCK),
   "resume_clock_id":RESUME_CLOCK_ID,"resume_allowance_seconds":RESUME_ALLOWANCE,
   "new_active_wall_seconds":clock.elapsed(),"new_active_wall_allowance_exceeded":clock.expired(),
   "previous_terminal_count":len(prior_records),"new_package_terminal_count":len(current_records),
   "combined_terminal_count":len(all_records),"missing_unit_count":len(missing),"missing_unit_ids":missing,
   "attempt_summary":partial_continuation_summary(root,plan["heldout"],all_records),
   "prior_terminal_mapping_sha256":sha(canon(sorted(
    [v[2] for v in prior_records.values()],key=lambda x:x["unit_id"]))),"smoke_reused":True}
 active_before_close=clock.elapsed()
 actual=clock.close()
 mapping_doc={"schema_id":"issue81-combined-terminal-mapping-v1","package_commit":package,
  "source_lock_sha256":fsha(LOCK),"registered_seed_table_sha256":fsha(SEEDS),
  "previous_terminal_package_commit":PREVIOUS_TERMINAL_PACKAGE_COMMIT,
  "previous_source_lock_sha256":PREVIOUS_TERMINAL_LOCK_SHA256,
  "terminal_count":len(mapping),"terminals":sorted(mapping,key=lambda x:x["unit_id"])}
 write_once(root/"continuation-terminal-mapping.json",canon(mapping_doc)+b"\n")
 report.update({"status":"COMPLETE","execution_mode":"missing_heldout_continuation",
  "package_commit":package,"source_lock_sha256":fsha(LOCK),
  "resume_clock_id":RESUME_CLOCK_ID,"resume_allowance_seconds":RESUME_ALLOWANCE,
  "new_active_wall_seconds":actual,"active_wall_before_closeout_persist":active_before_close,
  "original_active_wall_seconds":3784.857659886001,
  "previous_supplemental_active_wall_seconds":1810.9480466440036,
  "combined_active_wall_seconds":3784.857659886001+1810.9480466440036+actual,
  "previous_terminal_count":len(prior_records),"new_package_terminal_count":len(current_records),
  "verified_heldout_terminal_count":288,"diagnostic_smoke_reused":True,
  "diagnostic_smoke_manifest_sha256":fsha(ART/"evaluation-only"/PREVIOUS_TERMINAL_PACKAGE_COMMIT/"diagnostic-smoke-terminals.json"),
  "combined_terminal_mapping_sha256":fsha(root/"continuation-terminal-mapping.json"),
  "loaded_model_identities":identity["models"],
  "continuation_contract_sha256":fsha(CONTINUATION_CONTRACT),
  "limitations":report["limitations"]+["Continuation reuses completed PGONLY training; it does not re-estimate training cost."]})
 write_once(root/f"continuation-analysis-{package}.json",canon(report)+b"\n")
 compact={k:report[k] for k in ("schema_id","status","execution_mode","package_commit","source_lock_sha256",
  "owner_arm_count","primary_contrast","per_arm","contrasts","noncombat_action_coverage","attempt_resources",
  "training_updates","first_boss_qualification","j_elite_mechanism_note","preexecution_failure_summary","limitations",
  "original_active_wall_seconds","previous_supplemental_active_wall_seconds","new_active_wall_seconds",
  "active_wall_before_closeout_persist","combined_active_wall_seconds","previous_terminal_count","new_package_terminal_count",
  "verified_heldout_terminal_count","diagnostic_smoke_reused","diagnostic_smoke_manifest_sha256",
  "combined_terminal_mapping_sha256","loaded_model_identities","continuation_contract_sha256")}
 compact["retained_raw_root"]=str(root); compact["durable_data_root"]=str(DATA)
 write_once(STUDY/"continuation-results-summary.json",canon(compact)+b"\n")
 atomicj(root/"evaluation-only-progress.json",{"schema_id":"issue81-heldout-continuation-progress-v1",
  "status":"COMPLETE","package_commit":package,"source_lock_sha256":fsha(LOCK),
  "registered_terminal_count":288,"previous_terminal_count":len(prior_records),
  "new_package_terminal_count":len(current_records),"new_active_wall_seconds":actual,
  "allowance_seconds":RESUME_ALLOWANCE,"clock_id":RESUME_CLOCK_ID,
  "combined_active_wall_seconds":compact["combined_active_wall_seconds"],
  "combined_terminal_mapping_sha256":fsha(root/"continuation-terminal-mapping.json"),
  "analysis_sha256":fsha(root/f"continuation-analysis-{package}.json")})
 return compact

def run_missing_heldout_continuation():
 package=D.source_commit(); root=ART/"evaluation-only"/package
 root.mkdir(parents=True,exist_ok=True); progress=root/"evaluation-only-progress.json"
 clock=D.ActiveClock(RESUME_CLOCK_ID,RESUME_ALLOWANCE); closed=False
 try:
  plan=evaluation_only_plan(); identity=verify()
  prior_records,prior_mapping,missing,prior_binding=prior_heldout_records(plan["heldout"])
  smoke_batch,smoke_provenance=prior_smoke_batch(plan["smoke"])
  prior_mapping_doc={"schema_id":"issue81-prior-terminal-mapping-v1",
   "package_commit":package,"source_lock_sha256":fsha(LOCK),
   "previous_terminal_package_commit":PREVIOUS_TERMINAL_PACKAGE_COMMIT,
   "previous_source_lock_sha256":PREVIOUS_TERMINAL_LOCK_SHA256,
   "terminal_count":len(prior_mapping),"terminals":sorted(prior_mapping,key=lambda x:x["unit_id"])}
  write_once(root/"prior-terminal-mapping.json",canon(prior_mapping_doc)+b"\n")
  package_binding={"schema_id":"issue81-evaluation-only-package-v1","mode":"missing_heldout_continuation",
   "package_commit":package,"runner_code_commit":package,"source_lock_sha256":fsha(LOCK),
   "continuation_contract_sha256":fsha(CONTINUATION_CONTRACT),
   "prior_terminal_package_commit":PREVIOUS_TERMINAL_PACKAGE_COMMIT,
   "prior_source_lock_sha256":PREVIOUS_TERMINAL_LOCK_SHA256,
   "prior_terminal_count":len(prior_records),"prior_terminal_mapping_sha256":fsha(root/"prior-terminal-mapping.json"),
   "prior_progress_sha256":prior_binding["progress_sha256"],
   "prior_package_manifest_sha256":prior_binding["package_manifest_sha256"],
   "prior_smoke_manifest_sha256":smoke_provenance["prior_smoke_manifest_sha256"],
   "clock_id":RESUME_CLOCK_ID,"allowance_seconds":RESUME_ALLOWANCE,
   "model_identities":identity["models"],"registered_seed_table_sha256":fsha(SEEDS),
   "new_smoke":False,"new_training":False}
  write_once(root/"evaluation-only-package.json",canon(package_binding)+b"\n")
  verify_package_binding(root,package,fsha(LOCK))
  existing=current_heldout_records(root,plan["heldout"],package,package_binding)
  overlap=set(prior_records).intersection(existing)
  if overlap: raise RuntimeError(f"prior/current terminal duplicate would violate no-rerun rule: {sorted(overlap)[:3]}")
  run_rows=[r for r in plan["heldout"] if r["unit_id"] in missing and r["unit_id"] not in existing]
  if len(run_rows)!=len(missing)-len(existing): raise RuntimeError("continuation scheduler input is not exactly missing registered units")
  atomicj(progress,{"schema_id":"issue81-heldout-continuation-progress-v1","status":"VERIFIED",
   "package_commit":package,"source_lock_sha256":fsha(LOCK),
   "prior_terminal_count":len(prior_records),"prior_terminal_count_by_arm":PREVIOUS_TERMINALS_BY_ARM,
   "current_package_terminal_count":len(existing),"scheduled_missing_count":len(run_rows),
   "missing_unit_ids":[r["unit_id"] for r in run_rows],"clock_id":RESUME_CLOCK_ID,
   "allowance_seconds":RESUME_ALLOWANCE,"active_wall_seconds":clock.elapsed(),
   "prior_mapping_sha256":fsha(root/"prior-terminal-mapping.json"),
   "smoke_reused":True,"smoke_manifest_sha256":smoke_provenance["prior_smoke_manifest_sha256"]})
  if run_rows:
   batch=D.run_units(run_rows,root=root,pilot=False,clock=clock,package_commit=package)
  else:
   batch={"status":"SUCCEEDED","completed":0,"results":{}}
  current=current_heldout_records(root,plan["heldout"],package,package_binding)
  all_records=dict(prior_records); all_records.update(current)
  if len(all_records)==288:
   result=finalize_continuation(package,root,identity,clock,prior_records,current,prior_mapping,smoke_batch,plan)
   closed=True
   return result
  summary=partial_continuation_summary(root,plan["heldout"],all_records)
  actual=clock.close(); closed=True
  out={"status":"INCOMPLETE_HELDOUT","package_commit":package,"source_lock_sha256":fsha(LOCK),
   "clock_id":RESUME_CLOCK_ID,"allowance_seconds":RESUME_ALLOWANCE,"new_active_wall_seconds":actual,
   "new_active_wall_allowance_exceeded":actual>=RESUME_ALLOWANCE,
   "prior_terminal_count":len(prior_records),"new_package_terminal_count":len(current),
   "combined_terminal_count":len(all_records),"missing_unit_count":summary["missing_unit_count"],
   "missing_unit_ids":summary["missing_unit_ids"],"attempt_summary":summary,
   "prior_terminal_mapping_sha256":fsha(root/"prior-terminal-mapping.json"),
   "smoke_reused":True,"run_units_status":batch.get("status"),"run_units_completed":batch.get("completed")}
  atomicj(progress,{"schema_id":"issue81-heldout-continuation-progress-v1",**out})
  return out
 except BaseException as exc:
  atomicj(progress,{"schema_id":"issue81-heldout-continuation-progress-v1","status":"FAILED",
   "package_commit":package,"source_lock_sha256":fsha(LOCK),"clock_id":RESUME_CLOCK_ID,
   "allowance_seconds":RESUME_ALLOWANCE,"active_wall_seconds":clock.elapsed(),
   "failure_boundary":type(exc).__name__,"detail":str(exc)[:4096],
   "traceback":traceback.format_exc()[-12000:]})
  raise
 finally:
  if not closed: clock.close()

def main():
 p=argparse.ArgumentParser(); p.add_argument("--mode",choices=("prepare","verify","continue-heldout")); p.add_argument("--worker",type=Path); a=p.parse_args()
 if a.worker: return worker(a.worker)
 if a.mode is None: p.error("--mode is required unless --worker is used")
 if a.mode=="prepare": print(json.dumps(prepare(),sort_keys=True)); return 0
 if a.mode=="verify": print(json.dumps(verify(),sort_keys=True)); return 0
 result=run_missing_heldout_continuation(); print(json.dumps(result,sort_keys=True)); return 0 if result.get("status")=="COMPLETE" else 2
if __name__=="__main__": raise SystemExit(main())
