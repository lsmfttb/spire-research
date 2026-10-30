from __future__ import annotations
import argparse, base64, copy, hashlib, importlib.util, json, os, sys, time, zlib
from pathlib import Path

DATA=Path("/mnt/d/DeadlycatCoding/stsrl-data/issue83")
LEGACY=DATA/"artifacts/formal/3951ee0749d8e9c3dfcba33590ceb1f880047acc"
ORIGINAL="3951ee0749d8e9c3dfcba33590ceb1f880047acc"
OLD_LOCK="224c7a6e2cbd46d986294e68b31a07ec6518ea6f82eda4636ae0c98ef4fa63b2"
NATIVE="bb22332a4a7108e8f9691482df2624b3b132d75ca07e1aa7b394b932193e5cad"
OWNERS=(8300001,8300002)
ALLOWANCE=67272

def canon(v): return json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()
def sha_bytes(b): return hashlib.sha256(b).hexdigest()
def sha_file(p): return sha_bytes(Path(p).read_bytes())
def canonical_text_sha(p):
 raw=Path(p).read_bytes();raw.decode("utf-8-sig")
 raw=raw.replace(bytes((13,10)),bytes((10,))).replace(bytes((13,)),bytes((10,)))
 if raw.startswith(bytes((239,187,191))):raw=raw[3:]
 return sha_bytes(raw)
def readj(p): return json.loads(Path(p).read_text(encoding="utf-8"))
def write_once(p,v):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);raw=canon(v)+b"\n"
 if p.exists():
  if p.read_bytes()!=raw: raise FileExistsError(str(p))
 else:
  with p.open("xb") as f:f.write(raw);f.flush();os.fsync(f.fileno())
 return sha_bytes(raw)
def atomicj(p,v):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);t=p.with_name("."+p.name+".tmp")
 raw=canon(v)+b"\n"
 with t.open("wb") as f:f.write(raw);f.flush();os.fsync(f.fileno())
 os.replace(t,p)

def load_driver(root,label):
 path=Path(root)/"studies/issue-83-pgonly-scale/driver.py"
 spec=importlib.util.spec_from_file_location("issue83_equiv_"+label,path)
 mod=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mod;spec.loader.exec_module(mod);mod.configure_driver()
 package=mod.current_source_commit();lock=canonical_text_sha(mod.SOURCE_LOCK)
 mod._equiv_base_load_modules=mod.load_modules
 if label=="original":
  if package!=ORIGINAL or lock!=OLD_LOCK:raise RuntimeError("original source epoch differs from registered source")
  manifest=readj(mod.SOURCE_LOCK)
  study_mismatch=[k for k,v in manifest["study_files_sha256"].items() if canonical_text_sha(mod.STUDY/k)!=v]
  source_mismatch=[k for k,v in manifest["source_files_sha256"].items() if sha_file(mod.REPO/k)!=v]
  if study_mismatch or source_mismatch or canonical_text_sha(mod.STUDY/"contract.json")!=manifest["contract_sha256"] or canonical_text_sha(mod.SEED_TABLE)!=manifest["seed_table_sha256"] or mod.source_identity()!=manifest["runtime_identity"]:
   raise RuntimeError(f"original source-lock verification failed after text normalization: study={study_mismatch}; source={source_mismatch}")
 else:
  if package==ORIGINAL:raise RuntimeError("repair source is not a new execution epoch")
  mod.check_lock()
  if lock!=mod.repair_source_lock_sha256():raise RuntimeError("repair source-lock identity differs from canonical source-lock helper")
 if sha_file(mod.NATIVE)!=NATIVE:raise RuntimeError("native binary changed")
 return mod,package,lock

def install_binding_audit(mod,audit_events,progress):
 original_load=mod._equiv_base_load_modules
 original_writej=mod.ISSUE81.writej
 def separate_nonformal_observation(path,value):
  path=Path(path)
  if path.name=="natural-owner-observation.json" and value.get("arm")=="P1" and value.get("controller_arm")=="P1":
   path=path.with_name("natural-owner-observation-nonformal-"+sha_bytes(canon(value))[:12]+".json")
  return original_writej(path,value)
 mod.ISSUE81.writej=separate_nonformal_observation
 mod._equiv_runtime_runners=[]
 def audited_load(root,*,pilot):
  alg,rt,model,learn,executor=original_load(root,pilot=pilot)
  init=rt.initialize_native_runtime
  def audited_init(*args,**kwargs):
   cfg,runner,native,encoder=init(*args,**kwargs)
   mod._equiv_runtime_runners.append(runner)
   validate=runner.validate_live_native_candidate_bindings
   def audited_validate(projection,actions):
    before=[x.get("candidate_binding") for x in projection.get("candidates",[])]
    from sts_combat_rl.sim.decision_record import action_identity_dicts_for_actions
    identities=action_identity_dicts_for_actions(actions)
    result=validate(projection,actions)
    after=[x.get("candidate_binding") for x in projection.get("candidates",[])]
    if canon(before)!=canon(after):raise RuntimeError("adapter repair mutated legal candidate bindings")
    bindings=[sha_bytes(canon(x)) for x in after]
    action_hashes=[sha_bytes(canon(x)) for x in identities]
    if len(bindings)!=len(action_hashes):raise RuntimeError("candidate binding/action counts differ")
    for i,candidate in enumerate(projection.get("candidates",[])):
     value=(candidate.get("candidate_binding") or {}).get("value") or {}
     if value.get("candidate_index")!=i:raise RuntimeError("candidate binding order differs from native action order")
    event={"ordinal":len(audit_events),"candidate_count":len(bindings),
     "ordered_candidate_binding_sha256":bindings,"ordered_legal_action_identity_sha256":action_hashes,
     "binding_bytes_unchanged":True,"coverage_after_validation":projection.get("coverage_status")}
    audit_events.append(event)
    atomicj(progress,{"schema_id":"issue83-native-binding-audit-progress-v1","status":"ACTIVE","events":audit_events})
    return result
   runner.validate_live_native_candidate_bindings=audited_validate
   return cfg,runner,native,encoder
  rt.initialize_native_runtime=audited_init
  return alg,rt,model,learn,executor
 mod.load_modules=audited_load
 mod.D.load_modules=audited_load
 return audited_load

def decision_rows(payload):
 decisions=payload["base"]["decisions"]
 out=[]
 for d in decisions:
  compressed=base64.b64decode(d["candidate_features_f32le_zlib_base64"],validate=True)
  raw=zlib.decompress(compressed)
  rows,cols=d["candidate_features_shape"]
  if len(raw)!=int(rows)*int(cols)*4:raise RuntimeError("candidate feature matrix has unexpected f32 byte size")
  out.append({"matrix_f32le_sha256":sha_bytes(raw),"matrix_bytes":len(raw),
   "candidate_features_sha256":d["candidate_features_sha256"],"shape":d["candidate_features_shape"],
   "legal_identity_sha256":d["legal_identity_sha256"],"state_fingerprint":d["state_fingerprint"],
   "chosen_index":d["chosen_index"],"is_root":d["is_root"],"step_index":d["step_index"]})
 return out

def feature_payload(root,result):
 pointer=result.get("training_features")
 if not isinstance(pointer,dict):raise RuntimeError("worker receipt lacks training feature payload")
 p=Path(root)/pointer["path"];compressed=p.read_bytes()
 if sha_bytes(compressed)!=pointer["sha256"]:raise RuntimeError("training feature compressed hash mismatch")
 raw=zlib.decompress(compressed)
 if sha_bytes(raw)!=pointer["uncompressed_sha256"]:raise RuntimeError("training feature decompressed hash mismatch")
 return json.loads(raw)

def run_owner(mod,package,lock,epoch,owner,root,clock):
 table=readj(mod.SEED_TABLE)
 candidates=[x for x in table["rows"] if x["owner_seed"]==owner and x["phase"]=="training" and x["arm"]=="TRAIN"]
 if len(candidates)!=1:raise RuntimeError("owner does not map to exactly one registered training row")
 row=copy.deepcopy(candidates[0])
 if row.get("formal") is not True or row.get("block_index")!=12 or row.get("search_invocation_seeds")!=[]:
  raise RuntimeError("representative row is not the original frozen pi12 PGONLY training row")
 row["formal"]=False;row["unit_id"]=f"DIAG-{epoch}-{owner}";row["cohort"]="diagnostic-equivalence"
 base=Path(root)/"units"/mod.D.unit_key(row["unit_id"]);base.mkdir(parents=True,exist_ok=True)
 prior=sorted(base.glob("attempt-*"));attempt=base/f"attempt-{len(prior)+1:04d}";attempt.mkdir(parents=True,exist_ok=False)
 progress=attempt/"native-binding-audit-progress.json";audit=[];install_binding_audit(mod,audit,progress)
 job={"schema_id":"issue79-owner-worker-job-v1","artifact_root":str(root),"attempt_dir":str(attempt),
  "result_path":str(attempt/"result.json"),"output_dir":str(attempt/"native"),
  "run_id":row["unit_id"]+"-attempt-"+str(len(prior)+1),"unit_id":row["unit_id"],
  "phase":row["phase"],"row":row,"pilot":False,"package_commit":package,
  "runner_code_commit":package,"source_lock_sha256":lock}
 mod.write_json_once(attempt/"worker-job.json",job)
 clock.set_processes({os.getpid()})
 status=mod.worker_main(attempt/"worker-job.json")
 clock.set_processes(set())
 report=readj(attempt/"result.json")
 if status!=0 or report.get("status")!="SUCCEEDED":raise RuntimeError(f"{epoch} diagnostic owner {owner} failed; see {attempt}")
 final_audit={"schema_id":"issue83-native-binding-audit-v1","status":"COMPLETE",
  "source_commit":package,"source_lock_sha256":lock,"owner_seed":owner,"unit_id":row["unit_id"],
  "validation_count":len(audit),"events":audit}
 audit_sha=write_once(attempt/"native-binding-audit.json",final_audit)
 features=feature_payload(root,report)
 record={"source_commit":package,"source_lock_sha256":lock,"owner_seed":owner,"unit_id":row["unit_id"],
  "result_path":str(attempt/"result.json"),"result_sha256":sha_file(attempt/"result.json"),
  "feature_path":str(Path(root)/report["training_features"]["path"]),
  "feature_payload_sha256":report["training_features"]["uncompressed_sha256"],
  "binding_audit_path":str(attempt/"native-binding-audit.json"),"binding_audit_sha256":audit_sha,
  "native_binding_events":len(audit),"decisions":decision_rows(features),
  "terminal_floor":report.get("terminal_floor"),"terminal_outcome":report.get("terminal_outcome"),
  "terminal_act":report.get("terminal_act"),"completed_transitions":report.get("completed_transitions"),
  "formal":report.get("formal"),"expert_decisions":report.get("expert_decisions"),"search":report.get("search"),
  "source_repair_events":sum(len(getattr(r,"_issue83_phase3_repair_events",[])) for r in mod._equiv_runtime_runners)}
 if report.get("formal") is not False or report.get("expert_decisions")!=0 or report.get("search")!=[]:
  raise RuntimeError("diagnostic replay was formal or used a forbidden decision source")
 return record,features,final_audit

def recover_original_from_preserved_formal(mod,package,lock,owner,root):
 unit=f"DIAG-original-{owner}"; base=Path(root)/"units"/mod.D.unit_key(unit)
 attempts=sorted(base.glob("attempt-*"))
 if not attempts:return None
 attempt=attempts[-1]; failed=readj(attempt/"result.json")
 detail=str(failed.get("failure_detail",""))
 if (failed.get("status")!="FAILED" or failed.get("failure_boundary")!="FileExistsError" or
     "natural-owner-observation.json" not in detail):return None
 job=readj(attempt/"worker-job.json")
 if (job.get("package_commit")!=package or job.get("source_lock_sha256")!=lock or
     job.get("unit_id")!=unit or job.get("row",{}).get("owner_seed")!=owner or job.get("row",{}).get("formal") is not False):
  raise RuntimeError("failed old diagnostic attempt has unexpected identity")
 observation=readj(attempt/"natural-owner-observation.json")
 formal_path=LEGACY/"units"/mod.D.unit_key(f"TRAIN:{owner}")/"attempt-0001/result.json"
 formal=readj(formal_path); formal_obs=readj(formal_path.parent/"natural-owner-observation.json")
 if (formal.get("status")!="SUCCEEDED" or formal.get("package_commit")!=ORIGINAL or
     formal.get("source_lock_sha256")!=OLD_LOCK or formal.get("formal") is not True):
  raise RuntimeError("preserved formal owner receipt cannot back the diagnostic recovery")
 normalized=dict(observation); normalized["arm"]="TRAIN"
 if observation.get("arm")!="P1" or normalized!=formal_obs:
  raise RuntimeError("failed diagnostic observation does not match the saved formal owner observation")
 features=feature_payload(LEGACY,formal); decisions=decision_rows(features)
 progress=readj(attempt/"native-binding-audit-progress.json"); events=progress.get("events")
 if progress.get("status")!="ACTIVE" or len(events)!=len(decisions):
  raise RuntimeError("failed diagnostic attempt lacks a complete ordered native binding audit")
 for i,(event,decision) in enumerate(zip(events,decisions)):
  identities=decision.get("legal_identity_sha256")
  if (event.get("ordinal")!=i or event.get("candidate_count")!=len(identities) or
      event.get("ordered_legal_action_identity_sha256")!=identities or not event.get("binding_bytes_unchanged")):
   raise RuntimeError(f"old live native action audit differs from preserved formal decisions at {i}")
 audit={"schema_id":"issue83-native-binding-audit-v1","status":"COMPLETE_RECOVERED_FROM_POSTRUN_LOG_CONFLICT",
  "source_commit":package,"source_lock_sha256":lock,"owner_seed":owner,"unit_id":unit,"validation_count":len(events),"events":events,
  "recovery_provenance":{"failed_attempt":str(attempt),"failure_boundary":"post-run immutable observation collision",
   "saved_formal_result":str(formal_path),"all_live_ordered_action_identities_match_saved_formal":True,
   "all_native_binding_events_preserved":True,"new_or_formal_game_runs_during_recovery":0}}
 audit_path=attempt/"native-binding-audit.json"; audit_sha=write_once(audit_path,audit)
 record={"source_commit":package,"source_lock_sha256":lock,"owner_seed":owner,"unit_id":unit,
  "result_path":str(formal_path),"result_sha256":sha_file(formal_path),
  "feature_path":str(LEGACY/formal["training_features"]["path"]),
  "feature_payload_sha256":formal["training_features"]["uncompressed_sha256"],
  "binding_audit_path":str(audit_path),"binding_audit_sha256":audit_sha,"native_binding_events":len(events),
  "decisions":decisions,"terminal_floor":formal.get("terminal_floor"),"terminal_outcome":formal.get("terminal_outcome"),
  "terminal_act":formal.get("terminal_act"),"completed_transitions":formal.get("completed_transitions"),
  "formal":False,"expert_decisions":formal.get("expert_decisions"),"search":formal.get("search"),"source_repair_events":0,
  "original_live_action_order_matches_saved_formal":True,"original_formal_feature_and_terminal_reference":str(formal_path),
  "recovered_from_postrun_immutable_logging_conflict":True}
 return record,features,audit

def main():
 ap=argparse.ArgumentParser()
 ap.add_argument("--original-root",required=True);ap.add_argument("--repair-root",required=True)
 ap.add_argument("--diagnostic-output-id")
 args=ap.parse_args()
 old,oldsha,oldlock=load_driver(args.original_root,"original")
 new,newsha,newlock=load_driver(args.repair_root,"repair")
 if oldsha!=ORIGINAL or oldlock!=OLD_LOCK:raise RuntimeError("source epoch identity mismatch")
 output_id=args.diagnostic_output_id or newsha
 if not (12<=len(output_id)<=40 and all(c in "0123456789abcdef" for c in output_id)):
  raise RuntimeError("diagnostic output id must be a 12-40 character lowercase hex id")
 top=DATA/"diagnostics/native-repair-equivalence"/output_id
 final_path=top/"native-repair-equivalence.json"
 if final_path.exists():
  old_result=readj(final_path)
  print(json.dumps(old_result,sort_keys=True));return
 all_audits={};all_records={}
 clock=new.D.ActiveClock("formal",ALLOWANCE)
 try:
  if clock.expired():raise RuntimeError("global active-wall allowance is already exhausted")
  for epoch,mod,package,lock in (("original",old,oldsha,oldlock),("repair",new,newsha,newlock)):
   root=top/"raw"/epoch;root.mkdir(parents=True,exist_ok=True)
   for owner in OWNERS:
    owner_path=top/f"{epoch}-owner-{owner}.json"
    if owner_path.exists():
     record=readj(owner_path)
     if record.get("source_commit")!=package or record.get("source_lock_sha256")!=lock or record.get("owner_seed")!=owner:
      raise RuntimeError(f"existing owner record identity mismatch: {owner_path}")
     if sha_file(record["result_path"])!=record["result_sha256"] or sha_file(record["binding_audit_path"])!=record["binding_audit_sha256"]:
      raise RuntimeError(f"existing owner record evidence hash mismatch: {owner_path}")
     audit=readj(record["binding_audit_path"])
    else:
     recovered=recover_original_from_preserved_formal(mod,package,lock,owner,root) if epoch=="original" else None
     if recovered is None:record,_payload,audit=run_owner(mod,package,lock,epoch,owner,root,clock)
     else:record,_payload,audit=recovered
    formal_path=LEGACY/"units"/mod.D.unit_key(f"TRAIN:{owner}")/"attempt-0001/result.json"
    formal_result=readj(formal_path)
    if (formal_result.get("status")!="SUCCEEDED" or formal_result.get("package_commit")!=ORIGINAL or
        formal_result.get("source_lock_sha256")!=OLD_LOCK or formal_result.get("formal") is not True):
     raise RuntimeError(f"historical training receipt failed source/formal identity: {formal_path}")
    formal_payload=feature_payload(LEGACY,formal_result)
    record["matches_original_formal_terminal"]={k:record[k]==formal_result.get(k) for k in
     ("terminal_floor","terminal_outcome","terminal_act","completed_transitions")}
    record["matches_original_formal_feature_matrices_and_action_order"]=record["decisions"]==decision_rows(formal_payload)
    if epoch=="original" and not (all(record["matches_original_formal_terminal"].values()) and
       record["matches_original_formal_feature_matrices_and_action_order"]):
     raise RuntimeError(f"old-source diagnostic replay differs from its preserved formal result for owner {owner}")
    if not owner_path.exists():write_once(owner_path,record)
    all_records[(epoch,owner)]=record;all_audits[(epoch,owner)]=audit
  equivalence=[]
  for owner in OWNERS:
   o=all_records[("original",owner)];r=all_records[("repair",owner)]
   audit_equal=all_audits[("original",owner)]["events"]==all_audits[("repair",owner)]["events"]
   matrix_equal=o["decisions"]==r["decisions"]
   outcome_equal=all(o[k]==r[k] for k in ("terminal_floor","terminal_outcome","terminal_act","completed_transitions"))
   repair_events=r["source_repair_events"]
   for d in r["decisions"]:
    if len(d["shape"])!=2 or d["shape"][1]!=2048 or d["matrix_bytes"]!=d["shape"][0]*d["shape"][1]*4:raise RuntimeError("unexpected feature dimension or matrix byte count")
   equivalence.append({"owner_seed":owner,"ordered_feature_matrices_and_legal_identities_byte_equal":matrix_equal,
    "candidate_binding_hash_sequences_equal":audit_equal,"terminal_and_transition_summary_equal":outcome_equal,
    "decision_count":len(o["decisions"]),"old_binding_validation_count":len(all_audits[("original",owner)]["events"]),
    "repair_binding_validation_count":len(all_audits[("repair",owner)]["events"]),
    "repair_source_events_in_successful_owner":repair_events})
   if not (matrix_equal and audit_equal and outcome_equal):raise RuntimeError(f"source epochs differ on representative owner {owner}")
  clock.set_processes(set()); elapsed=clock.elapsed()
  result={"schema_id":"issue83-native-repair-equivalence-v1","status":"PASS",
   "original_execution_source_commit":oldsha,"original_source_lock_sha256":oldlock,
   "repair_execution_source_commit":newsha,"repair_source_lock_sha256":newlock,
   "native_source_commit":new.EXPECTED["sts_commit"],"native_module_sha256":sha_file(new.NATIVE),
   "representative_training_owners":list(OWNERS),"formal_seed_consumption":0,
   "replays_are_nonformal_same_owner_game_seed_and_policy_rng":True,
   "original_success_receipts_and_attempts_overwritten":False,
   "per_owner":equivalence,"active_wall_allowance_seconds":ALLOWANCE,
   "formal_clock_elapsed_seconds_after_diagnostic":elapsed,
   "post_clock_closeout_status":"UNMEASURED",
   "limitations":["representative source-equivalence diagnostic only; not a new efficacy sample",
    "successful historical receipts preserve legal action identity hashes; diagnostic also hashes every live native candidate binding object before and after adapter validation"]}
  result["evidence_sha256"]=write_once(final_path,result);print(json.dumps(result,sort_keys=True))
 finally:
  clock.set_processes(set());clock.close()
if __name__=="__main__":main()
