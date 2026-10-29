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
PREVIOUS_PACKAGES=("a7d6df2ee635ccd14b37365c21b5a4bafabb5379","a623cb1087221b953bd100099077c0effb6f34b3","7ff0fcaf3fb53c6aaeb26a6b960345a320d25e51")
EXPECT={"pi1_sha":"d36577e18b175930a275b4151ba2a93ca6c8fe1e3422512453d4017000c21978",
"pi1_digest":"33b3d3d891e53b7434919318ccb1c8411a0bebe1e2e48616ef040206261db557",
"pi1_opt":"7bdff6bdd14d8bf5bdcd1cf286e687b5a6df4ef6c11f80bad1ee18787e40f514",
"j_sha":"36e7dfdc78d3409eb65b2db05f6c6df5821330f502cbbd286bf191e39456eb57",
"j_digest":"521079ea9cb3e57f8b27057f37d6ecfc9181290f532713c18d307d79849704f5",
"j_opt":"622bbc288f2fa405060065af2a45ebdbc72dd8601ac7c3a257a2bf22a4020b6e",
"native":"bb22332a4a7108e8f9691482df2624b3b132d75ca07e1aa7b394b932193e5cad",
"sts":"8ded44c7ee341f48bb1bd49eeaa6cf6bc0384b2","stsr":"7d172f7c24c9d29429b6946ba44f6b59760b0d96"}
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
 "jtrain_optimizer_sha256":fsha(REF/"jtrain-block-3-step.pt")},
 "code_sha256":fsha(STUDY/"driver.py"),"launcher_sha256":fsha(STUDY/"run_issue81.sh"),"contract_sha256":fsha(CONTRACT),
 "seed_table_sha256":fsha(SEEDS),"config_sha256":fsha(STUDY/"config.json"),
 "stage1_config_sha256":fsha(STAGE1/"config.json"),"allowance_seconds":ALLOWANCE,"strict_elite_only_removal":True}
 writej(LOCK,lock)
 return {"status":"PREPARED","baseline":BASE,"source_lock_sha256":fsha(LOCK),"seed_rows":len(seed_table()["rows"]),"data_path":str(DATA)}

def check_lock():
 l=readj(LOCK)
 for key,path in (("code_sha256",STUDY/"driver.py"),("launcher_sha256",STUDY/"run_issue81.sh"),("contract_sha256",CONTRACT),("seed_table_sha256",SEEDS),
 ("config_sha256",STUDY/"config.json"),("stage1_config_sha256",STAGE1/"config.json"),
 ("issue79_driver_sha256",STUDY79/"driver.py"),("issue79_source_lock_sha256",STUDY79/"source-lock.json")):
  if fsha(path)!=l[key]: raise RuntimeError(f"locked file changed: {path}")
 for p,h in l["issue75_files_sha256"].items():
  if fsha(STUDY75/p)!=h: raise RuntimeError(f"#75 source lock mismatch: {p}")
 return l

def registered_arm_policy(phase,arm,row,fallback):
 if row.get("phase")=="training": return f"pi{int(row['block_index'])}",False
 policies={"BASE":("pi1",False),"PGONLY":("pi4",False),"JTRAIN":("jtrain",False)}
 if arm in policies: return policies[arm]
 return fallback(phase,arm,row)

def patch_modules(root,*,pilot=False):
 global ACTIVE_BLOCK
 alg,rt,model,learn,ex=ORIGINAL_LOAD(root,pilot=pilot)
 rt.STUDY=STUDY; rt.REPO_ROOT=REPO
 ex.STAGE1=STAGE1; ex.ARTIFACT_ROOT=root; ex.ROOT=REPO; ex.STUDY_ID="issue-81-pgonly-j-elite-ablation"
 oldpath=ex._model_path
 def model_path(label):
  if label=="pi1": return PI1ROOT/"models/pi1.pt"
  if label=="jtrain": return REF/"jtrain-pi4.pt"
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
   call=dict(kw); call["root_callback"]=None; call["capture_checkpoints"]=False; call["capture_root_limit"]=None
   result=oldrun(*a,**call); attempt=Path(call["out"]).parent
   writej(attempt/"natural-owner-observation.json",{"schema_id":"issue81-natural-owner-observation-v1",
   "arm":str(call.get("arm")),"seed":int(call["seed"]),"completed_transitions":int(result.get("completed_transitions",0)),
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
 a,ad,_=ex._load_model("pi1"); j,jd,_=ex._load_model("jtrain")
 ap=PI1ROOT/"models/block-0-step.pt"; jp=REF/"jtrain-block-3-step.pt"
 ast=torch.load(ap,map_location="cpu",weights_only=True); jst=torch.load(jp,map_location="cpu",weights_only=True)
 if fsha(PI1ROOT/"models/pi1.pt")!=EXPECT["pi1_sha"] or ad!=EXPECT["pi1_digest"]: raise RuntimeError("pi1 identity mismatch")
 if fsha(REF/"jtrain-pi4.pt")!=EXPECT["j_sha"] or jd!=EXPECT["j_digest"]: raise RuntimeError("JTRAIN identity mismatch")
 if ast.get("completed_steps")!=64 or jst.get("completed_steps")!=64: raise RuntimeError("optimizer step count mismatch")
 for k,v in a.state_dict().items():
  if not torch.equal(v,ast["model_state_dict"][k]): raise RuntimeError("pi1 optimizer embeds a different model")
 for k,v in j.state_dict().items():
  if not torch.equal(v,jst["model_state_dict"][k]): raise RuntimeError("JTRAIN optimizer embeds a different model")
 return {"pi1_sha256":fsha(PI1ROOT/"models/pi1.pt"),"pi1_digest":ad,"pi1_optimizer_sha256":fsha(ap),
 "jtrain_sha256":fsha(REF/"jtrain-pi4.pt"),"jtrain_digest":jd,"jtrain_optimizer_sha256":fsha(jp),
 "embedded_models_match":True,"optimizer_steps":64}

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

def run_formal():
 package=D.source_commit(); root=ART/"formal"/package; root.mkdir(parents=True,exist_ok=True); progress=root/"progress.json"
 clock=D.ActiveClock("issue81-global",ALLOWANCE)
 try:
  identity=verify(); atomicj(progress,{"schema_id":"issue81-progress-v1","status":"VERIFIED","package_commit":package,"identity":identity,"allowance":ALLOWANCE})
  table=readj(SEEDS); updates=[]
  for block in (1,2,3):
   rows=rows_for(table,"training",block)
   phase=D.run_units(rows,root=root,pilot=False,clock=clock,package_commit=package)
   if phase.get("status")!="SUCCEEDED" or phase.get("completed")!=128:
    out={"status":phase.get("status","INCOMPLETE_TRAINING"),"block":block,"completed":phase.get("completed"),"remaining":phase.get("remaining")}
    atomicj(progress,{"schema_id":"issue81-progress-v1",**out,"package_commit":package,"active_wall_seconds":clock.elapsed()}); return out
   alg,rt,m,l,ex=D.load_modules(root,pilot=False); examples=D._training_examples(rows,root=root,executor=ex,package_commit=package)
   try: update=D.train_update(block,examples,root=root,pilot=False,clock=clock)
   finally: del examples
   updates.append({"block":block,"steps":update.get("completed_steps"),"model_digest":update.get("result_model_digest")})
   atomicj(progress,{"schema_id":"issue81-progress-v1","status":"BLOCK_COMPLETE","block":block,"updates":updates,"package_commit":package,"active_wall_seconds":clock.elapsed()})
   if clock.expired(): return {"status":"GLOBAL_ACTIVE_WALL_ALLOWANCE_EXCEEDED","block":block}
  ev=D.run_units(rows_for(table,"heldout"),root=root,pilot=False,clock=clock,package_commit=package)
  if ev.get("status")!="SUCCEEDED" or ev.get("completed")!=288:
   out={"status":ev.get("status","INCOMPLETE_EVALUATION"),"completed":ev.get("completed"),"remaining":ev.get("remaining")}
   atomicj(progress,{"schema_id":"issue81-progress-v1",**out,"package_commit":package,"active_wall_seconds":clock.elapsed()}); return out
  report=analyze(root,package)
  report.update({"package_commit":package,"source_lock_sha256":fsha(LOCK),
   "active_wall_seconds_before_close":clock.elapsed(),"active_wall_allowance_seconds":ALLOWANCE,
   "optimizer_identity":{"pi1_optimizer_sha256":EXPECT["pi1_opt"],"jtrain_optimizer_sha256":EXPECT["j_opt"],"block1_initialized_from_pi1_optimizer":True}})
  if report["status"]!="COMPLETE":
   atomicj(root/"analysis-progress.json",report); return {"status":"INCOMPLETE_ANALYSIS","owner_arm_count":report["owner_arm_count"]}
  writej(root/f"analysis-{package}.json",report)
  compact={k:report[k] for k in ("schema_id","status","package_commit","source_lock_sha256","owner_arm_count",
   "primary_contrast","per_arm","contrasts","noncombat_action_coverage","attempt_resources","training_updates",
   "first_boss_qualification","j_elite_mechanism_note","preexecution_failure_summary","limitations")}
  compact["retained_raw_root"]=str(root); compact["durable_data_root"]=str(DATA)
  write_once(STUDY/"results-summary.json",canon(compact)+b"\n")
  atomicj(progress,{"schema_id":"issue81-progress-v1","status":"COMPLETE","package_commit":package,"owner_arm_count":288,
   "active_wall_seconds":clock.elapsed(),"allowance":ALLOWANCE})
  return compact
 except BaseException as e:
  atomicj(progress,{"schema_id":"issue81-progress-v1","status":"FAILED","package_commit":package,
   "active_wall_seconds":clock.elapsed(),"failure_boundary":type(e).__name__,"detail":str(e)[:4096],
   "traceback":traceback.format_exc()[-12000:]}); raise
 finally: clock.close()

def main():
 p=argparse.ArgumentParser(); p.add_argument("--mode",choices=("prepare","verify","formal")); p.add_argument("--worker",type=Path); a=p.parse_args()
 if a.worker: return worker(a.worker)
 if a.mode is None: p.error("--mode is required unless --worker is used")
 if a.mode=="prepare": print(json.dumps(prepare(),sort_keys=True)); return 0
 if a.mode=="verify": print(json.dumps(verify(),sort_keys=True)); return 0
 result=run_formal(); print(json.dumps(result,sort_keys=True)); return 0 if result.get("status")=="COMPLETE" else 2
if __name__=="__main__": raise SystemExit(main())
