from __future__ import annotations
import argparse, hashlib, importlib.util, json, math, os, statistics, subprocess, sys, time, traceback
from pathlib import Path

REPO=Path(__file__).resolve().parents[2]
STUDY=REPO/"studies/issue-83-pgonly-scale"
STUDY81=REPO/"studies/issue-81-pg-only"
STUDY79=REPO/"studies/issue-79-successor"
STUDY75=REPO/"studies/issue-75-no-expert-oracle"
PYTHON=Path("/mnt/d/DeadlycatCoding/stsrl-data/stsrl-spikes/py313-torch/bin/python3.13")
NATIVE=Path("/mnt/d/DeadlycatCoding/stsrl-data/stsrl-spikes/build-issue71-8ded-py313/slaythespire.cpython-313-x86_64-linux-gnu.so")
STS_SOURCE=Path("/mnt/d/DeadlycatCoding/stsrl-data/issue81/sources/sts_lightspeed")
STSRL_SOURCE=Path("/mnt/d/DeadlycatCoding/stsrl-data/issue81/sources/STSRL")
ISSUE81=None
D=None


def canonical(value):
 return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode("utf-8")
def sha256_bytes(payload): return hashlib.sha256(payload).hexdigest()
def file_sha256(path):
 h=hashlib.sha256()
 with Path(path).open("rb") as f:
  for block in iter(lambda:f.read(1024*1024),b""): h.update(block)
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
 tmp=path.with_name(f".{path.name}.{os.getpid()}.tmp")
 try:
  with tmp.open("xb") as f: f.write(payload); f.flush(); os.fsync(f.fileno())
  if path.exists():
   if path.read_bytes()!=payload: raise FileExistsError(f"immutable evidence differs: {path}")
  else: os.replace(tmp,path)
  return sha256_bytes(payload)
 finally: tmp.unlink(missing_ok=True)
def write_json_once(path,value): return write_once(path,canonical(value)+b"\n")
def atomic_json(path,value):
 path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
 tmp=path.with_name(f".{path.name}.{os.getpid()}.tmp")
 try:
  with tmp.open("xb") as f: f.write(canonical(value)+b"\n"); f.flush(); os.fsync(f.fileno())
  os.replace(tmp,path)
 finally: tmp.unlink(missing_ok=True)


def configure_driver():
 global ISSUE81,D
 os.environ["STS_LIGHTSPEED_SOURCE_DIR"]=str(STS_SOURCE)
 os.environ["STSRL_SOURCE_DIR"]=str(STSRL_SOURCE)
 spec=importlib.util.spec_from_file_location("issue81_reused_for_issue83",STUDY81/"driver.py")
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
  "source_lock_sha":repair_source_lock_sha256,
  "write_once":write_once,"write_json_once":write_json_once,"atomic_json":atomic_json,
  "derive_seed":derive_seed,"__file__":str(STUDY/"driver.py"),
 }.items(): setattr(D,name,value)
 for name,value in {
  "REPO":REPO,"STUDY":STUDY,"STUDY75":STUDY75,"STUDY79":STUDY79,"STAGE1":STAGE1,
  "DATA":DATA,"ART":ARTIFACTS,"ARTIFACTS":ARTIFACTS,"LOCK":SOURCE_LOCK,"SOURCE_LOCK":SOURCE_LOCK,
  "SEEDS":SEED_TABLE,"SEED_TABLE":SEED_TABLE,"CONTRACT":CONTRACT,"PYTHON":PYTHON,
  "NATIVE":NATIVE,"PGONLY_ROOT":PARENT_ROOT,"BASE":BASE_COMMIT,"TRAIN":FORMAL_TRAINING,
  "EVAL":FORMAL_EVALUATION,"ARMS":ARMS,"CONCURRENCY":CONCURRENCY,
  "write_once":write_once,"writej":write_json_once,"canon":canonical,
  "sha":sha256_bytes,"fsha":file_sha256,
 }.items(): setattr(ISSUE81,name,value)
 ISSUE81.D=D
 ISSUE81.registered_arm_policy=registered_arm_policy
 ISSUE81.native_controller_arm=lambda arm:"P1"
 D.load_modules=load_modules


def current_source_commit():
 return subprocess.run(["git","rev-parse","HEAD"],cwd=REPO,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True).stdout.strip()


class GlobalWallExpired(RuntimeError): pass


def run_phase(name,clock,operation,package,root):
 receipt_path=root/f"pilot-phase-{name}.json"; progress_path=root/f"pilot-phase-{name}-progress.json"
 if receipt_path.exists():
  old=read_json(receipt_path)
  if old.get("package_commit")!=package or old.get("status")!="SUCCEEDED": raise RuntimeError(f"pilot phase receipt mismatch: {name}")
  return old
 if progress_path.exists():
  progress=read_json(progress_path)
  if progress.get("package_commit")!=package: raise RuntimeError(f"pilot phase source mismatch: {name}")
  started=float(progress["phase_start_cumulative_seconds"])
 else:
  started=clock.elapsed(); progress={"schema_id":"issue83-pilot-phase-progress-v1","phase":name,"status":"ACTIVE",
   "package_commit":package,"phase_start_cumulative_seconds":started,"started_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
  atomic_json(progress_path,progress)
 result=operation()
 if result.get("status")!="SUCCEEDED": raise RuntimeError(f"pilot phase {name} did not finish: {result}")
 receipt={"schema_id":"issue83-pilot-phase-v1","phase":name,"package_commit":package,"status":"SUCCEEDED",
  "elapsed_active_seconds":clock.elapsed()-started,"result_summary":{k:result[k] for k in
  ("completed","row_wall_seconds","tries","block_index","owner_count","completed_steps","result_model_digest","native_wall_seconds_by_unit") if k in result},
  "finished_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
 write_json_once(receipt_path,receipt); progress["status"]="COMPLETE"; progress["phase_elapsed_active_seconds"]=receipt["elapsed_active_seconds"]
 progress["finished_utc"]=receipt["finished_utc"]; atomic_json(progress_path,progress)
 return receipt


def distribution(values):
 ordered=sorted(map(float,values))
 if not ordered: return {"count":0,"mean":None,"median":None,"p90":None,"p95":None,"max":None}
 def q(fraction):
  pos=(len(ordered)-1)*fraction; lo=math.floor(pos); hi=math.ceil(pos)
  return ordered[lo] if lo==hi else ordered[lo]*(hi-pos)+ordered[hi]*(pos-lo)
 return {"count":len(ordered),"mean":statistics.fmean(ordered),"median":q(.5),"p90":q(.9),"p95":q(.95),"max":ordered[-1]}


def finalize_pilot_clock_outputs(report_path,policy_path,clock_mode,closed_seconds):
 if not policy_path.is_file(): return None
 clock_path=ARTIFACTS/f"active-wall-{clock_mode}.json"; policy=read_json(policy_path)
 components=policy.get("projection_components_seconds")
 if not isinstance(components,dict) or not components: raise RuntimeError("pilot projection components are missing")
 components["pilot_actual"]=float(closed_seconds)
 projection=math.fsum(float(v) for v in components.values())
 policy.update({"pre_closeout_pilot_active_seconds":policy.get("actual_pilot_active_seconds"),
  "actual_pilot_active_seconds":float(closed_seconds),"complete_task_projection_seconds":projection,
  "complete_task_projection_hours":projection/3600.0,
  "one_global_active_wall_allowance_seconds":math.ceil(projection*float(policy["allowance_factor"])),
  "pilot_active_wall_clock_path":str(clock_path),"pilot_active_wall_clock_sha256":file_sha256(clock_path),
  "post_clock_closeout_active_wall_status":"UNMEASURED","post_clock_closeout_active_wall_seconds":None,
  "exact_complete_task_active_wall_seconds":None})
 atomic_json(policy_path,policy)
 if not report_path.is_file(): return None
 report=read_json(report_path)
 report.update({"pre_closeout_pilot_active_seconds":report.get("pilot_active_seconds"),
  "pilot_active_seconds":float(closed_seconds),"complete_task_projection_seconds":projection,
  "complete_task_projection_hours":projection/3600.0,
  "single_global_active_wall_allowance_seconds":policy["one_global_active_wall_allowance_seconds"],
  "time_policy_sha256":file_sha256(policy_path),"active_wall_clock_path":str(clock_path),
  "active_wall_clock_sha256":file_sha256(clock_path),"post_clock_closeout_active_wall_status":"UNMEASURED",
  "post_clock_closeout_active_wall_seconds":None,"exact_complete_task_active_wall_seconds":None})
 atomic_json(report_path,report); return report


def closed_active_wall_clock(mode,*,required=True):
 path=ARTIFACTS/f"active-wall-{mode}.json"
 if not path.is_file():
  if required: raise FileNotFoundError(f"closed active-wall clock is missing: {path}")
  return None
 record=read_json(path)
 if record.get("mode")!=mode: raise RuntimeError(f"active-wall clock mode mismatch: {path}")
 if record.get("active_session") is not None or record.get("process_ids"):
  raise RuntimeError(f"active-wall clock is not closed: {path}")
 elapsed=float(record.get("elapsed_seconds",-1.0))
 if not math.isfinite(elapsed) or elapsed<0: raise RuntimeError(f"invalid closed active-wall total: {path}")
 return {"path":path,"record":record,"elapsed_seconds":elapsed,"sha256":file_sha256(path)}


def pilot_report_recovery_status(report,recovery_clock):
 if recovery_clock is not None: return "MEASURED"
 finalization=report.get("finalization") or {}
 return "UNMEASURED" if finalization.get("original_failure") else "NOT_REQUIRED"


def ensure_formal_clock_initial_state(pilot_seconds,*,pilot_clock_sha256,recovery_seconds,
                                      recovery_clock_sha256,recovery_status):
 path=ARTIFACTS/"active-wall-formal.json"; reconciliation_path=ARTIFACTS/"formal-clock-carry-forward.json"
 exists=path.exists(); prior=read_json(path) if exists else {"schema_id":"issue83-active-wall-clock-v1","mode":"formal",
  "elapsed_seconds":0.0,"session_base_seconds":0.0,"active_session":None,"process_ids":[]}
 if prior.get("active_session") or prior.get("process_ids"): raise RuntimeError("formal active-wall clock still has an active process")
 current=float(prior.get("elapsed_seconds",0.0)); reconciliation=read_json(reconciliation_path) if reconciliation_path.exists() else None
 if reconciliation is None and exists and current>0:
  raise RuntimeError("formal clock has prior time without its pilot carry-forward receipt")
 baseline=float(reconciliation["initial_formal_clock_baseline_seconds"]) if reconciliation else current
 applied=float(reconciliation.get("applied_carry_forward_seconds",0.0)) if reconciliation else 0.0
 required=float(pilot_seconds)+(float(recovery_seconds) if recovery_seconds is not None else 0.0)
 additional=max(0.0,required-applied)
 if additional:
  prior["pre_carry_forward_elapsed_seconds"]=current; prior["elapsed_seconds"]=current+additional
  prior["session_base_seconds"]=current+additional
  prior["carry_forward_reconciliation"]={"initial_formal_clock_baseline_seconds":baseline,
   "required_closed_pilot_and_measured_recovery_seconds":required,"added_seconds":additional,
   "reason":"reconcile the closed pilot and measured receipt-recovery intervals before formal execution"}
  atomic_json(path,prior)
 elif not exists:
  prior["elapsed_seconds"]=required; prior["session_base_seconds"]=required
  prior["heartbeat_utc"]=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
  prior["counting_rule"]="Issue 83 closed pilot, measured receipt-recovery, and formal active program intervals; idle gaps excluded"
  atomic_json(path,prior)
 history=[] if reconciliation is None else list(reconciliation.get("adjustment_history",[]))
 if additional or reconciliation is None:
  history.append({"required_known_pilot_plus_recovery_seconds":required,"added_seconds":additional,
   "pilot_active_wall_clock_sha256":pilot_clock_sha256,"pilot_report_recovery_status":recovery_status,
   "pilot_report_recovery_active_wall_clock_sha256":recovery_clock_sha256})
 reconciliation={"schema_id":"issue83-formal-clock-carry-forward-v1","initial_formal_clock_baseline_seconds":baseline,
  "applied_carry_forward_seconds":applied+additional,"latest_required_known_pilot_plus_recovery_seconds":required,
  "pilot_report_recovery_status":recovery_status,"pilot_report_recovery_seconds":recovery_seconds,
  "unmeasured_recovery_interval_is_not_zero":recovery_status=="UNMEASURED","adjustment_history":history}
 atomic_json(reconciliation_path,reconciliation); return reconciliation


def find_pilot_package(current_package,table_sha,lock_sha):
 roots=[]; root_dir=ARTIFACTS/"pilot"
 if not root_dir.is_dir(): raise RuntimeError("no completed Issue 83 pilot package exists")
 for directory in sorted(root_dir.iterdir()):
  report=directory/"pilot-report.json"; policy=directory/"time-policy.json"
  if not report.is_file() or not policy.is_file(): continue
  r=read_json(report); t=read_json(policy); pilot_sha=r.get("source_branch_sha")
  if (r.get("status")!="PILOT_COMPLETE_NONFORMAL" or r.get("seed_table_sha256")!=table_sha or
      r.get("source_lock_sha256")!=lock_sha or t.get("source_lock_sha256")!=lock_sha or
      pilot_sha!=t.get("source_branch_sha")): continue
  ancestor=subprocess.run(["git","merge-base","--is-ancestor",pilot_sha,current_package],cwd=REPO,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  if ancestor.returncode==0: roots.append((pilot_sha,report,policy))
 if not roots: raise RuntimeError("no complete source-ancestor pilot matches the exact source lock and seed table")
 exact=[item for item in roots if item[0]==current_package]; chosen=exact if exact else roots
 if len(chosen)!=1: raise RuntimeError("multiple complete ancestor pilots match; refuse ambiguous selection")
 return chosen[0]


exec(Path(__file__).with_name("issue83_contract.py").read_text(encoding="utf-8-sig"),globals())


def main():
 parser=argparse.ArgumentParser()
 parser.add_argument("--mode",choices=("prepare","verify","pilot","finalize-pilot","formal"))
 parser.add_argument("--pilot-package-sha")
 parser.add_argument("--worker",type=Path)
 args=parser.parse_args(); configure_driver()
 if args.worker is not None:
  global CURRENT_WORKER_PACKAGE_ROOT
  job=read_json(args.worker)
  package=job.get("package_commit")
  if not isinstance(package,str) or len(package)!=40 or any(c not in "0123456789abcdef" for c in package):
   raise RuntimeError("worker job lacks the exact registered package commit")
  if job.get("source_lock_sha256")!=repair_source_lock_sha256():
   raise RuntimeError("worker job source-lock SHA differs from the active Issue 83 epoch")
  artifact_phase="pilot" if job.get("pilot") is True else "formal"
  CURRENT_WORKER_PACKAGE_ROOT=ARTIFACTS/artifact_phase/package
  if not CURRENT_WORKER_PACKAGE_ROOT.is_dir():
   raise FileNotFoundError(f"registered Issue 83 policy package root is missing: {CURRENT_WORKER_PACKAGE_ROOT}")
  return worker_main(args.worker)
 if args.mode=="prepare": result=prepare()
 elif args.mode=="verify": result=verify_parent_and_runtime(ARTIFACTS/"verify"/"native-import")
 elif args.mode=="pilot": result=pilot()
 elif args.mode=="finalize-pilot":
  if not args.pilot_package_sha: parser.error("--pilot-package-sha is required with --mode finalize-pilot")
  result=finalize_pilot(args.pilot_package_sha)
 elif args.mode=="formal": result=formal()
 else: parser.error("select --mode or --worker")
 print(json.dumps(result,sort_keys=True,separators=(",",":"),ensure_ascii=False))
 return 0 if result.get("status") in {"PREPARED","PASS_IMPORT_ONLY","PILOT_COMPLETE_NONFORMAL","COMPLETE"} else 2


if __name__=="__main__":
 try: raise SystemExit(main())
 except Exception:
  traceback.print_exc(); raise