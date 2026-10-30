from __future__ import annotations
import argparse, hashlib, importlib.util, json, math, os, shutil, statistics, subprocess, sys, time, traceback, zlib
from pathlib import Path

REPO=Path(__file__).resolve().parents[2]
STUDY=REPO/"studies/issue-82-pgonly-cycle"
STUDY81=REPO/"studies/issue-81-pg-only"
STUDY79=REPO/"studies/issue-79-successor"
STUDY75=REPO/"studies/issue-75-no-expert-oracle"
STAGE1=STUDY/"stage1"
DATA=Path("/mnt/d/DeadlycatCoding/stsrl-data/issue82")
ARTIFACTS=DATA/"artifacts"
SEED_TABLE=STUDY/"seed-table.json"
CONTRACT=STUDY/"contract.json"
SOURCE_LOCK=STUDY/"source-lock.json"
BASE_COMMIT="40758afa2adc34a9b401759fb8ade09d20654bf6"
PARENT_ROOT=Path("/mnt/d/DeadlycatCoding/stsrl-data/issue81/artifacts/formal/7ff0fcaf3fb53c6aaeb26a6b960345a320d25e51")
PARENT_MODEL=PARENT_ROOT/"models/pi4.pt"
PARENT_OPTIMIZER=PARENT_ROOT/"models/block-3-step.pt"
PARENT_UPDATE=PARENT_ROOT/"models/issue81-update-3.json"
PYTHON=Path("/mnt/d/DeadlycatCoding/stsrl-data/stsrl-spikes/py313-torch/bin/python3.13")
NATIVE=Path("/mnt/d/DeadlycatCoding/stsrl-data/stsrl-spikes/build-issue71-8ded-py313/slaythespire.cpython-313-x86_64-linux-gnu.so")
STS_SOURCE=Path("/mnt/d/DeadlycatCoding/stsrl-data/issue81/sources/sts_lightspeed")
STSRL_SOURCE=Path("/mnt/d/DeadlycatCoding/stsrl-data/issue81/sources/STSRL")
EXPECTED={
 "parent_model_sha256":"ae90cd8f96c06f4cbe6cd5133e1c827530cba2d043cb02d07dcf53fe2945c78f",
 "parent_model_digest":"3eac3177892778192309262e2c4a42734c3a30fa3b742f3ab2732bdd5b9810ce",
 "parent_optimizer_sha256":"80d15b396083e0194544838bdb79728c83075e2c14d29257377307be424cea77",
 "native_sha256":"bb22332a4a7108e8f9691482df2624b3b132d75ca07e1aa7b394b932193e5cad",
 "sts_commit":"8ded44c7ee341f48bb1bd49eeaae6cf6bc0384b2",
 "stsr_commit":"7d172f7c24c9d29429b6946ba44f6b59760b0d96",
}
PILOT_TRAINING=tuple(range(8220001,8220033))
PILOT_EVALUATION=tuple(range(8230001,8230009))
FORMAL_TRAINING=tuple(range(8200001,8201025))
FORMAL_EVALUATION=tuple(range(8210001,8210129))
ARMS=("PARENT","C256","C512","C768","C1024")
CONCURRENCY=8
ALLOWANCE_FACTOR=1.75
TRAINING_LATER_POLICY_FACTOR=1.35
EVALUATION_LATER_POLICY_FACTOR=1.25
ANALYSIS_MINIMUM_SECONDS=300.0
RETRY_OVERHEAD_FACTOR=1.10
ISSUE81=None
D=None
CURRENT_BLOCK=None
CURRENT_PILOT=False


def canonical(value):
 return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode("utf-8")
def sha256_bytes(payload): return hashlib.sha256(payload).hexdigest()
def file_sha256(path):
 h=hashlib.sha256()
 with Path(path).open("rb") as f:
  for chunk in iter(lambda:f.read(1048576),b""): h.update(chunk)
 return h.hexdigest()
def read_json(path):
 value=json.loads(Path(path).read_text(encoding="utf-8"))
 if not isinstance(value,dict): raise ValueError(f"expected JSON object: {path}")
 return value
def write_once(path,payload):
 path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
 if path.exists():
  if path.read_bytes()!=payload: raise FileExistsError(f"immutable evidence differs: {path}")
  return sha256_bytes(payload)
 temp=path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
 try:
  with temp.open("xb") as f: f.write(payload); f.flush(); os.fsync(f.fileno())
  try: os.link(temp,path)
  except FileExistsError:
   if path.read_bytes()!=payload: raise FileExistsError(f"immutable evidence differs: {path}")
  fd=os.open(path.parent,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
  try: os.fsync(fd)
  finally: os.close(fd)
 finally: temp.unlink(missing_ok=True)
 return sha256_bytes(payload)
def write_json_once(path,value): return write_once(path,canonical(value)+b"\n")
def atomic_json(path,value):
 path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
 temp=path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
 try:
  with temp.open("xb") as f: f.write(canonical(value)+b"\n"); f.flush(); os.fsync(f.fileno())
  os.replace(temp,path)
 finally: temp.unlink(missing_ok=True)
def derive_seed(*parts):
 text="issue82-pgonly-cycle-v1|"+"|".join(map(str,parts))
 return int.from_bytes(hashlib.sha256(text.encode()).digest()[:4],"big")&0x7fffffff


def configure_driver():
 global ISSUE81,D
 os.environ["STS_LIGHTSPEED_SOURCE_DIR"]=str(STS_SOURCE)
 os.environ["STSRL_SOURCE_DIR"]=str(STSRL_SOURCE)
 spec=importlib.util.spec_from_file_location("issue81_reused_for_issue82",STUDY81/"driver.py")
 if spec is None or spec.loader is None: raise RuntimeError("reviewed Issue 81 driver is unavailable")
 ISSUE81=importlib.util.module_from_spec(spec); sys.modules[spec.name]=ISSUE81; spec.loader.exec_module(ISSUE81)
 D=ISSUE81.D
 for name,value in {
  "REPO":REPO,"STUDY":STUDY,"STUDY75":STUDY75,"STAGE1":STAGE1,"ARTIFACTS":ARTIFACTS,
  "SEED_TABLE":SEED_TABLE,"SOURCE_LOCK":SOURCE_LOCK,"PINNED_PYTHON":PYTHON,
  "PILOT_TRAINING":PILOT_TRAINING,"PILOT_EVALUATION":PILOT_EVALUATION,
  "FORMAL_TRAINING":FORMAL_TRAINING,"FORMAL_EVALUATION":FORMAL_EVALUATION,
  "ARMS":ARMS,"CONCURRENCY":CONCURRENCY,"HEARTBEAT_SECONDS":2.0,
  "canonical":canonical,"sha256":sha256_bytes,"file_sha256":file_sha256,
  "write_once":write_once,"write_json_once":write_json_once,"atomic_json":atomic_json,
  "derive_seed":derive_seed,"__file__":str(STUDY/"driver.py"),
 }.items(): setattr(D,name,value)
 for name,value in {
  "REPO":REPO,"STUDY":STUDY,"STUDY75":STUDY75,"STUDY79":STUDY79,"STAGE1":STAGE1,
  "DATA":DATA,"ART":ARTIFACTS,"ARTIFACTS":ARTIFACTS,"LOCK":SOURCE_LOCK,"SOURCE_LOCK":SOURCE_LOCK,
  "SEEDS":SEED_TABLE,"SEED_TABLE":SEED_TABLE,"CONTRACT":CONTRACT,"PYTHON":PYTHON,
  "NATIVE":NATIVE,"PGONLY_ROOT":PARENT_ROOT,"BASE":BASE_COMMIT,"TRAIN":FORMAL_TRAINING,
  "EVAL":FORMAL_EVALUATION,"ARMS":ARMS,"write_once":write_once,"writej":write_json_once,
  "canon":canonical,"sha":sha256_bytes,"fsha":file_sha256,"seed_table":make_seed_table,
 }.items(): setattr(ISSUE81,name,value)
 ISSUE81.D=D
 ISSUE81.registered_arm_policy=registered_arm_policy
 ISSUE81.native_controller_arm=lambda arm:"P1"
 D.load_modules=load_modules


def registered_arm_policy(phase,arm,row,fallback):
 if phase=="training" or row.get("phase")=="training": return f"pi{int(row['block_index'])}",False
 if arm=="PARENT": return "pi4",False
 if row.get("formal") is not True and arm in ARMS[1:]: return "pi5",False
 labels={"C256":"pi6","C512":"pi8","C768":"pi10","C1024":"pi12"}
 if arm in labels: return labels[arm],False
 return fallback(phase,arm,row)


def current_source_commit():
 return subprocess.run(["git","rev-parse","HEAD"],cwd=REPO,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True).stdout.strip()
def source_identity():
 for root,expected in ((STS_SOURCE,EXPECTED["sts_commit"]),(STSRL_SOURCE,EXPECTED["stsr_commit"])):
  actual=subprocess.run(["git","-C",str(root),"rev-parse","HEAD"],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True).stdout.strip()
  dirty=subprocess.run(["git","-C",str(root),"status","--porcelain"],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True).stdout.strip()
  if actual!=expected or dirty: raise RuntimeError(f"source checkout is not the exact clean pinned commit: {root} {actual} dirty={bool(dirty)}")
 for path,key in ((NATIVE,"native_sha256"),(PARENT_MODEL,"parent_model_sha256"),(PARENT_OPTIMIZER,"parent_optimizer_sha256")):
  if file_sha256(path)!=EXPECTED[key]: raise RuntimeError(f"locked source/model hash mismatch: {path}")
 if not PARENT_UPDATE.is_file(): raise FileNotFoundError("retained #81 final update audit is missing")
 return {
  "base_commit":BASE_COMMIT,"sts_lightspeed_commit":EXPECTED["sts_commit"],
  "stsr_source_commit":EXPECTED["stsr_commit"],"native_module_path":str(NATIVE),
  "native_module_sha256":file_sha256(NATIVE),"python_path":str(PYTHON),
  "python_sha256":file_sha256(PYTHON),"runtime_libraries_sha256":{p.name:file_sha256(p) for p in (Path("/mnt/d/DeadlycatCoding/stsrl-data/stsrl-spikes/py313-torch/lib/libffi.so.8"),Path("/mnt/d/DeadlycatCoding/stsrl-data/stsrl-spikes/py313-torch/lib/libz.so.1"),Path("/mnt/d/DeadlycatCoding/stsrl-data/stsrl-spikes/py313-torch/lib/libstdc++.so.6"))},"parent_model_path":str(PARENT_MODEL),
  "parent_model_sha256":file_sha256(PARENT_MODEL),"parent_optimizer_path":str(PARENT_OPTIMIZER),
  "parent_optimizer_sha256":file_sha256(PARENT_OPTIMIZER),"parent_update_audit_path":str(PARENT_UPDATE),
  "parent_update_audit_sha256":file_sha256(PARENT_UPDATE),
  "source_worktrees":{str(STS_SOURCE):EXPECTED["sts_commit"],str(STSRL_SOURCE):EXPECTED["stsr_commit"]},
 }


def make_seed_table():
 rows=[]
 for owner in PILOT_TRAINING:
  row=D._row(owner,phase="training",arm="TRAIN",formal=False,block=4); row["search_invocation_seeds"]=[]; rows.append(row)
 for owner in PILOT_EVALUATION:
  for arm in ARMS:
   row=D._row(owner,phase="heldout",arm=arm,formal=False,block=None); row["search_invocation_seeds"]=[]; rows.append(row)
 for index,owner in enumerate(FORMAL_TRAINING):
  row=D._row(owner,phase="training",arm="TRAIN",formal=True,block=4+index//128); row["search_invocation_seeds"]=[]; rows.append(row)
 for owner in FORMAL_EVALUATION:
  for arm in ARMS:
   row=D._row(owner,phase="heldout",arm=arm,formal=True,block=None); row["search_invocation_seeds"]=[]; rows.append(row)
 return {
  "schema_id":"issue82-preregistered-seed-table-v1","study_id":"issue-82-pgonly-cycle",
  "seed_domains":{
   "debug_training":{"first":PILOT_TRAINING[0],"last":PILOT_TRAINING[-1],"count":len(PILOT_TRAINING)},
   "debug_evaluation":{"first":PILOT_EVALUATION[0],"last":PILOT_EVALUATION[-1],"count":len(PILOT_EVALUATION)},
   "formal_training":{"first":FORMAL_TRAINING[0],"last":FORMAL_TRAINING[-1],"count":len(FORMAL_TRAINING)},
   "formal_evaluation":{"first":FORMAL_EVALUATION[0],"last":FORMAL_EVALUATION[-1],"count":len(FORMAL_EVALUATION)},
  },
  "formal_training_blocks":[
   {"block_index":4+i,"owners":128,"first_seed":FORMAL_TRAINING[i*128],
    "last_seed":FORMAL_TRAINING[(i+1)*128-1],"policy_before_update":f"pi{4+i}",
    "policy_after_update":f"pi{5+i}"} for i in range(8)],
  "evaluation_arm_order":list(ARMS),
  "policy_rng_seed_derivation":"issue82-pgonly-cycle-v1|policy|owner_seed|arm; seed recorded per row",
  "bootstrap_seed":derive_seed("bootstrap","C1024","PARENT","terminal_floor"),
  "search_invocation_seeds":[],"rows":rows,
 }


def contract_document():
 return {
  "schema_id":"issue82-pgonly-cycle-contract-v1","study_id":"issue-82-pgonly-cycle",
  "issue_url":"https://github.com/lsmfttb/spire-research/issues/82","scope":"STUDY_ONLY",
  "question":"Does one fixed PGONLY cycle from reviewed #81 pi4 produce a second-cycle natural A20 terminal-floor gain, and how does it evolve through four predefined exposure checkpoints?",
  "population":"Natural standard-start Ironclad A20 games in the source-locked native surrogate; privileged native state is permitted for training projections only.",
  "parent":{"model_sha256":EXPECTED["parent_model_sha256"],"model_digest":"3eac3177892778192309262e2c4a42734c3a30fa3b742f3ab2732bdd5b9810ce",
   "optimizer_checkpoint_sha256":EXPECTED["parent_optimizer_sha256"],"optimizer_step_before_first_new_block":64,
   "final_update_audit_sha256":file_sha256(PARENT_UPDATE)},
  "fixed_treatment":{
   "battle_controller":"Battle Search-v2@400 in every arm",
   "optimizer":"AdamW lr=0.0003 betas=(0.9,0.999) eps=1e-8 weight_decay=0 gradient_norm_clip=1",
   "objective":"L_pg - 0.01 H; zero elite losses and gradients; 32 owners/minibatch; 64 updates/block",
   "architecture":"2048-dimensional signed-hash semantic encoder and existing 128->64->1 action-conditioned MLP",
   "training_owners":1024,"block_sizes":[128]*8,
   "optimizer_lineage":"first new block resumes exact #81 AdamW step-64 state and advances to 128; seven later blocks start fresh and end at 64",
   "search":"no J search; any search call raises; all registered search streams, expert actions and elite contributions are zero",
   "checkpoints":{"C256":"pi6","C512":"pi8","C768":"pi10","C1024":"pi12"},
  },
  "heldout":{"owners":128,"arm_order":list(ARMS),"complete_natural_terminals":640,
   "paired_owner_is_not_shared_future_rng_path":True,"no_heldout_adaptation_or_selection":True},
  "primary":{"contrast":"C1024-PARENT","metric":"natural terminal_floor","paired_owner_bootstrap_replicates":20000,"interval":"percentile 95%"},
  "secondary":["descriptive C256/C512/C768 contrasts","direct act2_plus, act3_plus, act4_reached and heart_victory receipt fields",
   "natural terminal floor by arm","native noncombat action_kind_counts","fixed training-state policy response","whole-task active wall"],
  "limitations":["bounded operator study, not RL convergence","privileged native-surrogate training does not establish public-information or live-client efficacy",
   "checkpoint contrasts are descriptive and unadjusted","pilot clone is discarded and never enters formal training"],
  "durable_artifact_root":str(DATA),"no_copy_of_prior_large_search_artifacts":True,
  "no_delete_or_move_existing_evidence":True,
 }


def source_files():
 paths=[STUDY81/"driver.py",STUDY81/"source-lock.json",STUDY79/"driver.py",STUDY79/"source-lock.json",
  STUDY75/"algorithm.py",STUDY75/"learning.py",STUDY75/"model.py",STUDY75/"runtime.py",
  STUDY75/"config.json",STUDY75/"stage1/config.json",STUDY75/"stage1/executor.py"]
 return {path.relative_to(REPO).as_posix():file_sha256(path) for path in paths}
def study_file_hashes():
 return {"driver.py":file_sha256(STUDY/"driver.py"),"run_issue82.sh":file_sha256(STUDY/"run_issue82.sh"),
  "config.json":file_sha256(STUDY/"config.json"),"stage1/config.json":file_sha256(STAGE1/"config.json"),
  "contract.json":file_sha256(CONTRACT),"seed-table.json":file_sha256(SEED_TABLE)}
def source_lock_document():
 return {"schema_id":"issue82-source-lock-v1","study_id":"issue-82-pgonly-cycle","base_commit":BASE_COMMIT,
  "task_branch":"codex/issue82-pgonly-cycle","study_files_sha256":study_file_hashes(),
  "source_files_sha256":source_files(),"runtime_identity":source_identity(),
  "seed_table_sha256":file_sha256(SEED_TABLE),"contract_sha256":file_sha256(CONTRACT),
  "registered_policy_rng_arm_order":list(ARMS),"concurrency":CONCURRENCY,
  "time_policy":{"one_global_accumulated_active_wall_allowance":True,"allowance_factor":ALLOWANCE_FACTOR,
   "no_cpu_or_per_stage_limit":True,"set_after_actual_debug_pilot":True,"pilot_wall_carried_into_global_clock":True},
  "raw_evidence_root":str(DATA)}


def prepare():
 merge=subprocess.run(["git","merge-base","HEAD",BASE_COMMIT],cwd=REPO,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True).stdout.strip()
 if merge!=BASE_COMMIT: raise RuntimeError(f"task branch is not based on exact source {BASE_COMMIT}: {merge}")
 source_identity()
 for path in (DATA,ARTIFACTS,ARTIFACTS/"pilot",ARTIFACTS/"formal",ARTIFACTS/"verify",STAGE1): path.mkdir(parents=True,exist_ok=True)
 shutil.copyfile(STUDY81/"config.json",STUDY/"config.json")
 shutil.copyfile(STUDY81/"stage1/config.json",STAGE1/"config.json")
 write_json_once(SEED_TABLE,make_seed_table())
 write_json_once(CONTRACT,contract_document())
 lock=source_lock_document()
 if SOURCE_LOCK.exists() and read_json(SOURCE_LOCK)!=lock:
  formal_root=ARTIFACTS/"formal"
  if formal_root.exists() and any(formal_root.glob("*/units/*/attempt-*")):
   raise RuntimeError("refusing source-lock refresh after formal owner attempts exist")
  atomic_json(SOURCE_LOCK,lock)
 else:
  write_json_once(SOURCE_LOCK,lock)
 check_lock()
 return {"status":"PREPARED","base_commit":BASE_COMMIT,"source_lock_sha256":file_sha256(SOURCE_LOCK),
  "seed_table_sha256":file_sha256(SEED_TABLE),"seed_rows":len(read_json(SEED_TABLE)["rows"]),
  "pilot_training_rows":len(PILOT_TRAINING),"pilot_evaluation_arm_rows":len(PILOT_EVALUATION)*len(ARMS),
  "formal_training_rows":len(FORMAL_TRAINING),"formal_heldout_arm_rows":len(FORMAL_EVALUATION)*len(ARMS),
  "parent_model_sha256":file_sha256(PARENT_MODEL),"parent_optimizer_sha256":file_sha256(PARENT_OPTIMIZER),
  "parent_update_audit_sha256":file_sha256(PARENT_UPDATE),"durable_root":str(DATA)}


def check_lock():
 lock=read_json(SOURCE_LOCK)
 if lock.get("schema_id")!="issue82-source-lock-v1": raise RuntimeError("source lock schema mismatch")
 if study_file_hashes()!=lock.get("study_files_sha256"): raise RuntimeError("locked Issue 82 source file changed")
 if source_files()!=lock.get("source_files_sha256"): raise RuntimeError("locked #75/#79/#81 source changed")
 if source_identity()!=lock.get("runtime_identity"): raise RuntimeError("native/source/model identity changed after lock")
 if file_sha256(SEED_TABLE)!=lock.get("seed_table_sha256") or file_sha256(CONTRACT)!=lock.get("contract_sha256"):
  raise RuntimeError("registered seed table or scientific contract changed")
 return lock


def load_modules(root,*,pilot):
 global CURRENT_PILOT
 CURRENT_PILOT=bool(pilot)
 ISSUE81.ACTIVE_BLOCK=CURRENT_BLOCK
 algorithm,runtime,model,learning,executor=ISSUE81.patch_modules(root,pilot=pilot)
 executor.STUDY_ID="issue-82-pgonly-cycle"
 old_path=executor._model_path
 def model_path(label):
  if label=="pi4": return PARENT_MODEL
  if label.startswith("pi") and label[2:].isdigit() and int(label[2:])>=5: return root/"models"/f"{label}.pt"
  return old_path(label)
 executor._model_path=model_path
 learning.FORMAL_BLOCK_OWNERS=32 if pilot else 128
 learning.FORMAL_MINIBATCH_OWNERS=32
 learning.FORMAL_STEPS_PER_BLOCK=64
 original=getattr(learning,"_issue82_original_new_optimizer",learning._new_optimizer)
 learning._issue82_original_new_optimizer=original
 def new_optimizer(policy):
  optimizer=original(policy)
  if CURRENT_BLOCK==4:
   state=__import__("torch").load(PARENT_OPTIMIZER,map_location="cpu",weights_only=True)
   optimizer.load_state_dict(state["optimizer_state_dict"])
  return optimizer
 learning._new_optimizer=new_optimizer
 return algorithm,runtime,model,learning,executor


def verify_parent_and_runtime(output_dir):
 check_lock()
 algorithm,runtime,model,learning,executor=D.load_modules(output_dir,pilot=True)
 policy,digest,_=executor._load_model("pi4")
 import torch
 model_state=torch.load(PARENT_MODEL,map_location="cpu",weights_only=True)
 opt_state=torch.load(PARENT_OPTIMIZER,map_location="cpu",weights_only=True)
 embedded=opt_state.get("model_state_dict")
 weights_match=isinstance(embedded,dict) and embedded.keys()==model_state.keys() and all(torch.equal(embedded[k],model_state[k]) for k in model_state)
 steps=sorted(int(v["step"].item()) for v in opt_state.get("optimizer_state_dict",{}).get("state",{}).values() if "step" in v)
 audit=read_json(PARENT_UPDATE)
 for key,value in {"completed_steps":64,"elite_loss_sum":0.0,"elite_gradient_norm_sum":0.0,
  "model_sha256":EXPECTED["parent_model_sha256"],"checkpoint_sha256":EXPECTED["parent_optimizer_sha256"],
  "result_model_digest":EXPECTED["parent_model_digest"]}.items():
  if audit.get(key)!=value: raise RuntimeError(f"#81 update-3 audit mismatch: {key}")
 if digest!=EXPECTED["parent_model_digest"] or not weights_match: raise RuntimeError("parent π4 weights do not match inherited optimizer")
 if opt_state.get("completed_steps")!=64 or len(steps)!=6 or set(steps)!={64}: raise RuntimeError(f"#81 AdamW step state invalid: {steps}")
 config,runner,native,encoder=runtime.initialize_native_runtime(output_dir/"native-import",sampling=False,reviewer_proof=None,stage1_binding=None)
 loaded=Path(native.__file__).resolve()
 if loaded!=NATIVE.resolve() or file_sha256(loaded)!=EXPECTED["native_sha256"]: raise RuntimeError("loaded native module identity mismatch")
 receipt={"schema_id":"issue82-parent-native-preflight-v1","status":"PASS_IMPORT_ONLY","formal_seeds_consumed":0,
  "simulator_instances_created":0,"expert_instances_created_or_called":0,"parent_model_sha256":file_sha256(PARENT_MODEL),
  "parent_model_digest":digest,"parent_optimizer_sha256":file_sha256(PARENT_OPTIMIZER),"optimizer_step":64,
  "optimizer_model_weights_match":weights_match,"native_module_path":str(loaded),"native_module_sha256":file_sha256(loaded),
  "sts_lightspeed_commit":EXPECTED["sts_commit"],"stsr_source_commit":EXPECTED["stsr_commit"],
  "python_version":sys.version.split()[0],"torch_version":torch.__version__,"numpy_version":__import__("numpy").__version__}
 write_json_once(ARTIFACTS/f"parent-native-preflight-{current_source_commit()}.json",receipt)
 return receipt


def worker_main(job_path):
 global CURRENT_BLOCK,CURRENT_PILOT
 job=read_json(job_path); row=job["row"]
 CURRENT_PILOT=bool(job.get("pilot"))
 CURRENT_BLOCK=int(row["block_index"]) if row.get("phase")=="training" else None
 return int(D.worker_main(job_path))


def optimizer_steps(state):
 return sorted(int(v["step"].item()) for v in state.get("state",{}).values() if "step" in v)
class GlobalWallExpired(RuntimeError): pass


def train_update(block,examples,*,root,pilot,clock=None):
 global CURRENT_BLOCK
 CURRENT_BLOCK=int(block)
 algorithm,runtime,model,learning,executor=D.load_modules(root,pilot=pilot)
 expected_count=32 if pilot else 128
 frozen,source_digest,_=executor._load_model(f"pi{block}")
 owner_sha=hashlib.sha256(",".join(str(int(x.owner_seed)) for x in examples).encode("ascii")).hexdigest()
 order_seed=derive_seed("pilot-block-order" if pilot else "formal-block-order",block)
 checkpoint=root/"models"/f"block-{block}-step.pt"
 target=executor._model_path(f"pi{block+1}")
 if len(examples)!=expected_count: raise RuntimeError(f"block {block} has {len(examples)} owners, expected {expected_count}")
 if block==4: D._save_policy_probes(examples,root)
 receipt_path=root/"models"/f"issue82-update-{block}.json"
 if receipt_path.exists():
  receipt=read_json(receipt_path); _,digest,_=executor._load_model(f"pi{block+1}")
  if receipt.get("source_model_digest")!=source_digest or receipt.get("result_model_digest")!=digest:
   raise RuntimeError(f"existing update {block} does not match its saved model chain")
  return receipt
 resume=None
 if checkpoint.exists():
  import torch
  resume=torch.load(checkpoint,map_location="cpu",weights_only=True)
 def save_step(step,trained,optimizer,rng_state,order,diagnostics):
  if clock is not None and clock.expired(): raise GlobalWallExpired("global active-wall allowance expired during 64-step update")
  executor._atomic_torch_save(checkpoint,{"schema_id":executor.RESUME_SCHEMA,"block_index":block,"completed_steps":step,
   "block_order_seed":order_seed,"source_model_digest":source_digest,"owner_seed_sha256":owner_sha,
   "model_state_dict":trained.state_dict(),"optimizer_state_dict":optimizer.state_dict(),"python_rng_state":rng_state,
   "owner_order":[int(v) for v in order],"diagnostics":[vars(v) for v in diagnostics]})
 began=time.monotonic()
 result=learning.train_policy_iteration_block(frozen,examples,block_order_seed=order_seed,step_callback=save_step,resume_state=resume)
 diagnostics=[vars(v) for v in result.diagnostics]
 if len(diagnostics)!=64 or any(d["loss_elite"]!=0.0 or d["grad_norm_elite"]!=0.0 or d["elite_owner_count"]!=0 or d["elite_root_count"]!=0 for d in diagnostics):
  raise RuntimeError("PGONLY update contains nonzero elite loss/gradient")
 model_receipt=executor._save_model_once(result.model,target,model.model_digest)
 import torch
 saved=torch.load(checkpoint,map_location="cpu",weights_only=True)
 steps=optimizer_steps(saved["optimizer_state_dict"])
 expected_step=128 if block==4 else 64
 if len(steps)!=6 or set(steps)!={expected_step}: raise RuntimeError(f"AdamW step count mismatch in block {block}: {steps}")
 receipt={"schema_id":"issue82-policy-update-v1","status":"SUCCEEDED","block_index":block,"owner_count":expected_count,
  "completed_steps_in_this_block":64,"optimizer_step_before_block":64 if block==4 else 0,"optimizer_step_after_block":expected_step,
  "optimizer_lineage":"exact #81 PGONLY AdamW state" if block==4 else "fresh AdamW",
  "optimizer_inherited_state_sha256":file_sha256(PARENT_OPTIMIZER) if block==4 else None,
  "block_order_seed":order_seed,"owner_seed_sha256":owner_sha,"source_model_digest":result.source_model_digest,
  "result_model_digest":result.result_model_digest,"model":model_receipt,
  "optimizer_checkpoint":{"path":checkpoint.relative_to(root).as_posix(),"sha256":file_sha256(checkpoint),"internal_parameter_steps":steps},
  "diagnostics":diagnostics,"elapsed_seconds":time.monotonic()-began,"expert_decisions":0,"search_invocations":0,"pilot_nonformal":bool(pilot)}
 write_json_once(receipt_path,receipt)
 return receipt


def rows_for(table,*,phase,formal,block=None):
 return [r for r in table["rows"] if r["phase"]==phase and bool(r["formal"]) is bool(formal) and (block is None or r.get("block_index")==block)]


def run_phase(name,clock,operation,package,root):
 receipt_path=root/f"pilot-phase-{name}.json"
 progress_path=root/f"pilot-phase-{name}-progress.json"
 if receipt_path.exists(): return read_json(receipt_path)
 if progress_path.exists():
  progress=read_json(progress_path); started=float(progress["phase_start_cumulative_seconds"])
 else:
  started=clock.elapsed(); progress={"schema_id":"issue82-pilot-phase-progress-v1","phase":name,"status":"ACTIVE",
   "package_commit":package,"phase_start_cumulative_seconds":started,"started_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
  atomic_json(progress_path,progress)
 result=operation()
 if result.get("status")!="SUCCEEDED": raise RuntimeError(f"pilot phase {name} did not finish: {result}")
 receipt={"schema_id":"issue82-pilot-phase-v1","phase":name,"package_commit":package,"status":"SUCCEEDED",
  "elapsed_active_seconds":clock.elapsed()-started,"result_summary":{k:result[k] for k in
   ("completed","row_wall_seconds","tries","block_index","owner_count","completed_steps","result_model_digest","native_wall_seconds_by_unit") if k in result},
  "finished_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
 write_json_once(receipt_path,receipt); progress["status"]="COMPLETE"; progress["phase_elapsed_active_seconds"]=receipt["elapsed_active_seconds"]
 progress["finished_utc"]=receipt["finished_utc"]; atomic_json(progress_path,progress)
 return receipt


def distribution(values):
 ordered=sorted(map(float,values))
 if not ordered: return {"count":0,"mean":None,"median":None,"p90":None,"p95":None,"max":None}
 def q(x):
  pos=(len(ordered)-1)*x; lo=math.floor(pos); hi=math.ceil(pos)
  return ordered[lo] if lo==hi else ordered[lo]*(hi-pos)+ordered[hi]*(pos-lo)
 return {"count":len(ordered),"mean":statistics.fmean(ordered),"median":q(.5),"p90":q(.9),"p95":q(.95),"max":ordered[-1]}


def pilot():
 check_lock()
 package=current_source_commit()
 root=ARTIFACTS/"pilot"/package
 report_path=root/"pilot-report.json"
 if report_path.exists(): return read_json(report_path)
 clock=D.ActiveClock(f"pilot-{package[:12]}",None)
 try:
  preflight=verify_parent_and_runtime(output_dir=root/"verify")
  table=read_json(SEED_TABLE)
  train_rows=rows_for(table,phase="training",formal=False)
  training=run_phase("training",clock,lambda:D.run_units(train_rows,root=root,pilot=True,clock=clock,package_commit=package),package,root)
  def update_op():
   check_lock()
   a,r,m,l,e=D.load_modules(root,pilot=True)
   examples=D._training_examples(train_rows,root=root,executor=e,package_commit=package)
   receipt=train_update(4,examples,root=root,pilot=True,clock=clock)
   del examples
   return {"status":"SUCCEEDED","block_index":4,"owner_count":receipt["owner_count"],
    "completed_steps":receipt["completed_steps_in_this_block"],"result_model_digest":receipt["result_model_digest"]}
  update=run_phase("update",clock,update_op,package,root)
  eval_rows=rows_for(table,phase="heldout",formal=False)
  evaluation=run_phase("evaluation",clock,lambda:D.run_units(eval_rows,root=root,pilot=True,clock=clock,package_commit=package),package,root)
  train_reports=[D.existing_success(root,row,package) for row in train_rows]
  eval_reports=[D.existing_success(root,row,package) for row in eval_rows]
  if any(row is None for row in train_reports+eval_reports): raise RuntimeError("pilot phase does not bind all immutable terminals")
  all_reports=train_reports+eval_reports
  if any(row.get("expert_decisions")!=0 or row.get("search")!=[] for row in all_reports): raise RuntimeError("pilot used Expert or online search")
  if any(row.get("terminal_outcome") is None or row.get("terminal_floor") is None for row in all_reports): raise RuntimeError("pilot contains nonterminal owners")
  measured=clock.elapsed()
  train_wall=float(training["elapsed_active_seconds"]); update_wall=float(update["elapsed_active_seconds"]); eval_wall=float(evaluation["elapsed_active_seconds"])
  train_proj=train_wall*(len(FORMAL_TRAINING)/len(train_rows))*TRAINING_LATER_POLICY_FACTOR
  update_proj=update_wall*8
  eval_proj=eval_wall*(len(FORMAL_EVALUATION)*len(ARMS)/len(eval_rows))*EVALUATION_LATER_POLICY_FACTOR
  subtotal=train_proj+update_proj+eval_proj
  retry_margin=subtotal*(RETRY_OVERHEAD_FACTOR-1.0)
  closeout_margin=max(ANALYSIS_MINIMUM_SECONDS,subtotal*.05)
  projection=measured+subtotal+retry_margin+closeout_margin
  allowance=math.ceil(projection*ALLOWANCE_FACTOR)
  train_summary=training["result_summary"]; eval_summary=evaluation["result_summary"]
  train_concurrency=D._max_concurrent_workers(root,train_rows); eval_concurrency=D._max_concurrent_workers(root,eval_rows)
  if max(train_concurrency,eval_concurrency)>CONCURRENCY: raise RuntimeError("pilot concurrency exceeded the registered launch width")
  update_receipt=read_json(root/"models/issue82-update-4.json")
  policy={"schema_id":"issue82-single-global-wall-policy-v1","source_branch_sha":package,
   "source_lock_sha256":file_sha256(SOURCE_LOCK),"seed_table_sha256":file_sha256(SEED_TABLE),
   "actual_pilot_active_seconds":measured,"configured_concurrency":CONCURRENCY,
   "actual_max_concurrent_training_workers":train_concurrency,"actual_max_concurrent_evaluation_workers":eval_concurrency,
   "projection_components_seconds":{"pilot_actual_active_wall_carried_forward":measured,
    "1024_training_games_at_1_35_later_policy_factor":train_proj,"eight_64_step_updates":update_proj,
    "640_heldout_arm_games_at_1_25_later_policy_factor":eval_proj,"retry_failure_uncertainty_margin_10_percent":retry_margin,
    "end_stage_analysis_closeout_margin":closeout_margin},
   "complete_task_projection_seconds":projection,"complete_task_projection_hours":projection/3600,
   "one_global_active_wall_allowance_seconds":allowance,"allowance_factor":ALLOWANCE_FACTOR,
   "clock_rule":"pilot plus formal active program wall; overlapping processes counted once; idle gaps excluded; no CPU or per-stage caps",
   "pilot_training_game_wall_distribution_seconds":distribution(list(train_summary.get("row_wall_seconds",{}).values())),
   "pilot_evaluation_game_wall_distribution_seconds":distribution(list(eval_summary.get("row_wall_seconds",{}).values())),
   "pilot_training_retry_counts_by_unit":train_summary.get("tries",{}),"pilot_evaluation_retry_counts_by_unit":eval_summary.get("tries",{}),
   "training_retries":sum(max(0,int(v)-1) for v in train_summary.get("tries",{}).values()),
   "evaluation_retries":sum(max(0,int(v)-1) for v in eval_summary.get("tries",{}).values()),
   "disposable_update_receipt":update_receipt,
   "limitations":["32 debug training owners and 40 debug evaluation owner-arm games; throughput extrapolation assumes same concurrency",
    "later-policy and retry uncertainty factors are projections, not measured future timings","pilot-trained pi5 aliases all four checkpoint routes and is not efficacy evidence"]}
  write_json_once(root/"time-policy.json",policy)
  result={"schema_id":"issue82-real-native-pilot-v1","status":"PILOT_COMPLETE_NONFORMAL",
   "source_branch_sha":package,"source_lock_sha256":file_sha256(SOURCE_LOCK),"seed_table_sha256":file_sha256(SEED_TABLE),
   "parent_native_preflight":preflight,
   "training_games":{"registered":len(train_rows),"complete_terminals":len(train_reports),"elapsed_active_seconds":train_wall,
    "actual_max_concurrent_workers":train_concurrency,"row_wall_seconds":train_summary.get("row_wall_seconds",{}),
    "tries":train_summary.get("tries",{}),"terminals":[{"owner_seed":r["owner_seed"],"terminal_floor":t["terminal_floor"],
     "terminal_outcome":t["terminal_outcome"],"expert_decisions":t["expert_decisions"]} for r,t in zip(train_rows,train_reports)]},
   "disposable_update":{"elapsed_active_seconds":update_wall,"owners":update_receipt["owner_count"],
    "steps":update_receipt["completed_steps_in_this_block"],"optimizer_step_before":update_receipt["optimizer_step_before_block"],
    "optimizer_step_after":update_receipt["optimizer_step_after_block"],"inherited_optimizer_sha256":update_receipt["optimizer_inherited_state_sha256"],
    "result_model_digest":update_receipt["result_model_digest"],"formal_checkpoint":False},
   "five_arm_evaluation_paths":{"registered_owner_arm_count":len(eval_rows),"complete_terminals":len(eval_reports),
    "elapsed_active_seconds":eval_wall,"actual_max_concurrent_workers":eval_concurrency,
    "row_wall_seconds":eval_summary.get("row_wall_seconds",{}),"tries":eval_summary.get("tries",{}),
    "all_checkpoint_arms_use_pilot_pi5_alias":True,"efficacy_inference":False},
   "pilot_active_seconds":measured,"complete_task_projection_seconds":projection,"complete_task_projection_hours":projection/3600,
   "single_global_active_wall_allowance_seconds":allowance,"allowance_factor":ALLOWANCE_FACTOR,
   "time_policy_path":(root/"time-policy.json").relative_to(STUDY).as_posix(),"formal_results":False,"efficacy_inference":False}
  write_json_once(report_path,result)
  return result
 finally: clock.close()


def ensure_formal_clock_initial_state(pilot_seconds):
 path=ARTIFACTS/"active-wall-formal.json"
 if path.exists():
  prior=read_json(path)
  if prior.get("active_session"): raise RuntimeError("formal active-wall clock still has an active process")
  if float(prior.get("elapsed_seconds",0))<pilot_seconds: raise RuntimeError("formal clock lost pilot active time")
  return
 write_json_once(path,{"schema_id":"issue79-active-wall-clock-v1","mode":"formal","elapsed_seconds":pilot_seconds,
  "active_session":None,"process_ids":[],"heartbeat_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
  "counting_rule":"Issue 82 pilot plus formal active program intervals; idle gaps excluded"})


def verify_update(block,rows,root):
 path=root/"models"/f"issue82-update-{block}.json"
 if not path.is_file(): return None
 receipt=read_json(path); seeds=sorted(int(r["owner_seed"]) for r in rows)
 owner_sha=hashlib.sha256(",".join(map(str,seeds)).encode("ascii")).hexdigest()
 if receipt.get("status")!="SUCCEEDED" or receipt.get("owner_count")!=128 or receipt.get("completed_steps_in_this_block")!=64 or receipt.get("owner_seed_sha256")!=owner_sha or receipt.get("expert_decisions")!=0 or receipt.get("search_invocations")!=0:
  raise RuntimeError(f"update {block} receipt is not the registered complete block")
 global CURRENT_BLOCK
 CURRENT_BLOCK=block
 a,r,m,l,e=D.load_modules(root,pilot=False)
 _,source_digest,_=e._load_model(f"pi{block}")
 _,result_digest,_=e._load_model(f"pi{block+1}")
 checkpoint=root/"models"/f"block-{block}-step.pt"
 if receipt.get("source_model_digest")!=source_digest or receipt.get("result_model_digest")!=result_digest or file_sha256(checkpoint)!=receipt["optimizer_checkpoint"]["sha256"]:
  raise RuntimeError(f"update {block} model or optimizer checkpoint hash mismatch")
 return receipt


def save_training_progress(root,package,clock,updates,current_block=None):
 atomic_json(root/"training-progress.json",{"schema_id":"issue82-training-progress-v1","source_branch_sha":package,
  "status":"ACTIVE","completed_training_owners":sum(int(x["owner_count"]) for x in updates),
  "completed_blocks":updates,"current_block":current_block,"active_wall_seconds":clock.elapsed(),
  "allowance_seconds":clock.allowance,"updated_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())})


def analyze(root,table,package):
 outcomes=[]; action_counts={arm:{} for arm in ARMS}; per_unit_actions=[]; by_arm={arm:[] for arm in ARMS}
 for row in rows_for(table,phase="heldout",formal=True):
  record=D.existing_success_record(root,row,package)
  if record is None: raise RuntimeError(f"missing heldout natural terminal: {row['unit_id']}")
  report,result_path=record
  if report.get("expert_decisions")!=0 or report.get("search")!=[]: raise RuntimeError("heldout result has expert/search activity")
  if report.get("terminal_outcome") is None or report.get("terminal_floor") is None: raise RuntimeError("heldout result is nonterminal")
  obs_path=result_path.parent/"natural-owner-observation.json"; obs=read_json(obs_path)
  if obs.get("seed")!=row["owner_seed"] or obs.get("arm")!=row["arm"] or obs.get("search_root_callback") is not False:
   raise RuntimeError(f"natural observation mismatch: {row['unit_id']}")
  counts=obs.get("action_kind_counts",{})
  for name,count in counts.items(): action_counts[row["arm"]][name]=action_counts[row["arm"]].get(name,0)+int(count)
  result={"owner_seed":int(row["owner_seed"]),"arm":row["arm"],"terminal_floor":int(report["terminal_floor"]),
   "terminal_outcome":report["terminal_outcome"],"act2_plus":bool(report["act2_plus"]),"act3_plus":bool(report["act3_plus"]),
   "act4_reached":bool(report["act4_reached"]),"heart_victory":bool(report["heart_victory"]),
   "completed_transitions":int(report["completed_transitions"]),"native_noncombat_action_kind_counts":counts,
   "result_path":result_path.relative_to(root).as_posix(),"result_sha256":file_sha256(result_path),
   "observation_path":obs_path.relative_to(root).as_posix(),"observation_sha256":file_sha256(obs_path)}
  outcomes.append(result); by_arm[row["arm"]].append(report)
  per_unit_actions.append({"owner_seed":row["owner_seed"],"arm":row["arm"],"counts":counts})
 if len(outcomes)!=640: raise RuntimeError(f"heldout complete terminal count is {len(outcomes)}, not 640")
 per_arm={}
 for arm,reports in by_arm.items():
  per_arm[arm]={"owners":len(reports),"mean_terminal_floor":statistics.fmean(float(x["terminal_floor"]) for x in reports),
   "act2_plus":sum(bool(x["act2_plus"]) for x in reports),"act3_plus":sum(bool(x["act3_plus"]) for x in reports),
   "act4_reached":sum(bool(x["act4_reached"]) for x in reports),"heart_victory":sum(bool(x["heart_victory"]) for x in reports),
   "completed_transitions_total":sum(int(x["completed_transitions"]) for x in reports),
   "native_noncombat_action_kind_counts":action_counts[arm]}
 contrasts={label:D._paired_bootstrap(outcomes,arm,"PARENT","terminal_floor") for arm,label in
  (("C256","C256-PARENT"),("C512","C512-PARENT"),("C768","C768-PARENT"),("C1024","C1024-PARENT"))}
 algorithm,runtime,model,learning,executor=D.load_modules(root,pilot=False)
 probe_file=root/"diagnostics/policy-probes.json.zlib"; probes=D._load_policy_probes(probe_file)
 parent,_,_=executor._load_model("pi4"); response={}
 for arm,label in (("C256","pi6"),("C512","pi8"),("C768","pi10"),("C1024","pi12")):
  later,_,_=executor._load_model(label); response[arm]=learning.policy_probe_diagnostics(parent,later,probes)
 return {"schema_id":"issue82-complete-game-analysis-v1","status":"COMPLETE","source_branch_sha":package,
  "source_lock_sha256":file_sha256(SOURCE_LOCK),"seed_table_sha256":file_sha256(SEED_TABLE),
  "registered_owner_arm_count":640,"complete_owner_arm_count":len(outcomes),"contrasts":contrasts,"per_arm":per_arm,
  "fixed_training_state_policy_response":{"probe_source":"first actual multichoice decision in each of 128 new block-4 training owners",
   "feature_file_sha256":file_sha256(probe_file),"diagnostics":response,"heldout_outcomes_not_used_for_policy_selection":True,
   "interpretation":"descriptive policy change on fixed training states, not heldout causal effect"},
  "owner_arm_outcomes":outcomes,"limitations":["privileged native-surrogate training only; no public-information/live-client transfer claim",
   "only C1024-PARENT is primary; other contrasts are descriptive and unadjusted",
   "paired owner does not imply shared future hidden RNG path","no automatic follow-on training is registered"],
  "efficacy_inference_scope":"study-only"}


def formal():
 check_lock(); package=current_source_commit()
 pilot_root=ARTIFACTS/"pilot"/package; pilot_path=pilot_root/"pilot-report.json"; policy_path=pilot_root/"time-policy.json"
 report=read_json(pilot_path); policy=read_json(policy_path)
 if report.get("source_branch_sha")!=package or report.get("status")!="PILOT_COMPLETE_NONFORMAL": raise RuntimeError("matching complete source-locked pilot is required")
 if report.get("seed_table_sha256")!=file_sha256(SEED_TABLE) or policy.get("seed_table_sha256")!=file_sha256(SEED_TABLE): raise RuntimeError("pilot used a different seed table")
 allowance=int(policy["one_global_active_wall_allowance_seconds"]); pilot_seconds=float(policy["actual_pilot_active_seconds"])
 ensure_formal_clock_initial_state(pilot_seconds)
 root=ARTIFACTS/"formal"/package; root.mkdir(parents=True,exist_ok=True)
 clock=D.ActiveClock("formal",allowance); updates=[]; table=read_json(SEED_TABLE)
 try:
  check_lock()
  for block in range(4,12):
   rows=rows_for(table,phase="training",formal=True,block=block)
   games=D.run_units(rows,root=root,pilot=False,clock=clock,package_commit=package)
   if games.get("status")!="SUCCEEDED":
    result={"schema_id":"issue82-formal-progress-v1","status":games.get("status"),"phase":"training-games",
     "block_index":block,"completed_in_block":games.get("completed"),"remaining_unit_ids":games.get("remaining"),
     "completed_training_owners":sum(int(x["owner_count"]) for x in updates),"completed_updates":updates,
     "active_wall_seconds":clock.elapsed(),"active_wall_allowance_seconds":allowance}
    atomic_json(root/"formal-progress.json",result); return result
   update=verify_update(block,rows,root)
   if update is None:
    a,r,m,l,e=D.load_modules(root,pilot=False); examples=D._training_examples(rows,root=root,executor=e,package_commit=package)
    update=train_update(block,examples,root=root,pilot=False,clock=clock); del examples
   updates.append(update); save_training_progress(root,package,clock,updates)
   if clock.expired():
    result={"schema_id":"issue82-formal-progress-v1","status":"GLOBAL_ACTIVE_WALL_ALLOWANCE_EXCEEDED",
     "phase":"after-training-block","block_index":block,"completed_training_owners":sum(int(x["owner_count"]) for x in updates),
     "completed_updates":updates,"active_wall_seconds":clock.elapsed(),"active_wall_allowance_seconds":allowance}
    atomic_json(root/"formal-progress.json",result); return result
  evaluation=D.run_units(rows_for(table,phase="heldout",formal=True),root=root,pilot=False,clock=clock,package_commit=package)
  if evaluation.get("status")!="SUCCEEDED":
   result={"schema_id":"issue82-formal-progress-v1","status":evaluation.get("status"),"phase":"heldout",
    "completed_heldout_owner_arms":evaluation.get("completed"),"remaining_unit_ids":evaluation.get("remaining"),
    "completed_training_owners":1024,"active_wall_seconds":clock.elapsed(),"active_wall_allowance_seconds":allowance}
   atomic_json(root/"formal-progress.json",result); return result
  check_lock(); final=analyze(root,table,package)
  final["pilot_report_sha256"]=file_sha256(pilot_path); final["time_policy_sha256"]=file_sha256(policy_path)
  final["training_updates"]=updates; final["pilot_active_wall_seconds_carried_forward"]=pilot_seconds
  final["active_wall_seconds_before_closeout"]=clock.elapsed(); final["active_wall_allowance_seconds"]=allowance
  final_path=root/"final-analysis.json"; write_json_once(final_path,final)
  atomic_json(root/"formal-progress.json",{"schema_id":"issue82-formal-progress-v1","status":"COMPLETE",
   "completed_training_owners":1024,"completed_training_blocks":8,"completed_heldout_owner_arms":640,
   "active_wall_seconds":clock.elapsed(),"active_wall_allowance_seconds":allowance,
   "final_analysis_path":final_path.relative_to(root).as_posix(),"final_analysis_sha256":file_sha256(final_path)})
  return final
 except GlobalWallExpired as exc:
  result={"schema_id":"issue82-formal-progress-v1","status":"GLOBAL_ACTIVE_WALL_ALLOWANCE_EXCEEDED",
   "phase":"training-update","failure":str(exc),"completed_training_owners":sum(int(x["owner_count"]) for x in updates),
   "completed_updates":updates,"active_wall_seconds":clock.elapsed(),"active_wall_allowance_seconds":allowance}
  atomic_json(root/"formal-progress.json",result); return result
 finally: clock.close()


def main():
 parser=argparse.ArgumentParser()
 parser.add_argument("--mode",choices=("prepare","verify","pilot","formal"))
 parser.add_argument("--worker",type=Path)
 args=parser.parse_args(); configure_driver()
 if args.worker is not None: return worker_main(args.worker)
 if args.mode=="prepare": result=prepare()
 elif args.mode=="verify": result=verify_parent_and_runtime(ARTIFACTS/"verify"/"native-import")
 elif args.mode=="pilot": result=pilot()
 elif args.mode=="formal": result=formal()
 else: parser.error("select --mode or --worker")
 print(json.dumps(result,sort_keys=True,separators=(",",":"),ensure_ascii=False))
 return 0 if result.get("status") in {"PREPARED","PASS_IMPORT_ONLY","PILOT_COMPLETE_NONFORMAL","COMPLETE"} else 2
if __name__=="__main__":
 try: raise SystemExit(main())
 except Exception: traceback.print_exc(); raise
