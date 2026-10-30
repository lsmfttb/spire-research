
STUDY=REPO/"studies/issue-83-pgonly-scale"; DATA=Path("/mnt/d/DeadlycatCoding/stsrl-data/issue83")
ARTIFACTS=DATA/"artifacts"; STAGE1=STUDY/"stage1"; SEED_TABLE=STUDY/"seed-table.json"
CONTRACT=STUDY/"contract.json"; SOURCE_LOCK=STUDY/"source-lock-repair-serialization-v1.json"; PREVIOUS_REPAIR_SOURCE_LOCK=STUDY/"source-lock-repair.json"; ORIGINAL_SOURCE_LOCK=STUDY/"source-lock.json"
BASE_COMMIT="acdbf9b397bce95f3e5d26077c34e80dbc59821e"
ORIGINAL_EXECUTION_COMMIT="3951ee0749d8e9c3dfcba33590ceb1f880047acc"
ORIGINAL_SOURCE_LOCK_SHA256="224c7a6e2cbd46d986294e68b31a07ec6518ea6f82eda4636ae0c98ef4fa63b2"
PREVIOUS_REPAIR_EXECUTION_COMMIT="e944d2d0260f9ab4742c3abc693c22f07419419a"
PREVIOUS_REPAIR_SOURCE_LOCK_SHA256="d7237514b36a64e74d122dbf4ed1e18e51eaf1d639b3d693882f419f0284b794"
LEGACY_FORMAL_ROOT=ARTIFACTS/"formal"/ORIGINAL_EXECUTION_COMMIT
PARENT_ROOT=Path("/mnt/d/DeadlycatCoding/stsrl-data/issue82/artifacts/formal/8f88f1554821707db6228d2de384f2334ec2615b")
PARENT_MODEL=PARENT_ROOT/"models/pi12.pt"; PARENT_OPTIMIZER=PARENT_ROOT/"models/block-11-step.pt"
PARENT_UPDATE=PARENT_ROOT/"models/issue82-update-11.json"
EXPECTED={"parent_model_sha256":"6f72fdd60d684e4667f2357ba578f84f6e087a18d4d3944c65bf1e0aabd0cf19",
 "parent_model_digest":"7c530520b5e107702575f9c8e612bff70e345b5c73715c33aa16eb75e00653ee",
 "parent_optimizer_sha256":"96a5599f389be0a90adfab2a245b88359a166b5b859101688200a492be893993",
 "parent_update_sha256":"60fc4ebbefb5ee88cd4dbe25407567e8fd38b7e38bf8e10d1fa3287a0bab9441",
 "native_sha256":"bb22332a4a7108e8f9691482df2624b3b132d75ca07e1aa7b394b932193e5cad",
 "sts_commit":"8ded44c7ee341f48bb1bd49eeaae6cf6bc0384b2","stsr_commit":"7d172f7c24c9d29429b6946ba44f6b59760b0d96"}
PILOT_TRAINING=tuple(range(8320001,8320033)); PILOT_EVALUATION=tuple(range(8330001,8330009))
FORMAL_TRAINING=tuple(range(8300001,8303073)); FORMAL_VALIDATION=tuple(range(8340001,8340065))
FORMAL_TEST=tuple(range(8310001,8310321)); FORMAL_EVALUATION=FORMAL_TEST
CHECKPOINT_LABELS=("pi16","pi20","pi24","pi28","pi32","pi36")
VALIDATION_ARMS=("PARENT","PI16","PI20","PI24","PI28","PI32","PI36")
PILOT_ARMS=("PARENT","CLONE"); TEST_ARMS=("PARENT","SELECTED","FINAL"); ARMS=VALIDATION_ARMS
CONCURRENCY=8; ALLOWANCE_FACTOR=1.75; TRAINING_LATER_POLICY_FACTOR=1.35
EVALUATION_LATER_POLICY_FACTOR=1.25; ANALYSIS_MINIMUM_SECONDS=300.0; RETRY_OVERHEAD_FACTOR=1.10
SELECTED_POLICY_LABEL=None
CURRENT_BLOCK=None
CURRENT_PILOT=False

def derive_seed(*parts):
 return int.from_bytes(hashlib.sha256(("issue83-pgonly-scale-v1|"+"|".join(map(str,parts))).encode()).digest()[:4],"big")&0x7fffffff

def registered_arm_policy(phase,arm,row,fallback):
 if row.get("phase")=="training": return f"pi{int(row['block_index'])}",False
 if arm=="PARENT": return "pi12",False
 if arm=="CLONE": return "pi13",False
 mapping={"PI16":"pi16","PI20":"pi20","PI24":"pi24","PI28":"pi28","PI32":"pi32","PI36":"pi36","FINAL":"pi36"}
 if arm in mapping: return mapping[arm],False
 if arm=="SELECTED":
  if SELECTED_POLICY_LABEL not in CHECKPOINT_LABELS: raise RuntimeError("SELECTED route lacks frozen validation receipt")
  return SELECTED_POLICY_LABEL,False
 return fallback(phase,arm,row)

def source_identity():
 for root,expected in ((STS_SOURCE,EXPECTED["sts_commit"]),(STSRL_SOURCE,EXPECTED["stsr_commit"])):
  actual=subprocess.run(["git","-C",str(root),"rev-parse","HEAD"],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True).stdout.strip()
  dirty=subprocess.run(["git","-C",str(root),"status","--porcelain"],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True).stdout.strip()
  if actual!=expected or dirty: raise RuntimeError(f"pinned source mismatch: {root} {actual} dirty={bool(dirty)}")
 for path,key in ((NATIVE,"native_sha256"),(PARENT_MODEL,"parent_model_sha256"),(PARENT_OPTIMIZER,"parent_optimizer_sha256"),(PARENT_UPDATE,"parent_update_sha256")):
  if file_sha256(path)!=EXPECTED[key]: raise RuntimeError(f"locked hash mismatch: {path}")
 audit=read_json(PARENT_UPDATE)
 if (audit.get("status")!="SUCCEEDED" or audit.get("block_index")!=11 or audit.get("owner_count")!=128 or
  audit.get("completed_steps_in_this_block")!=64 or audit.get("optimizer_step_before_block")!=0 or
  audit.get("optimizer_step_after_block")!=64 or audit.get("result_model_digest")!=EXPECTED["parent_model_digest"] or
  audit.get("model",{}).get("sha256")!=EXPECTED["parent_model_sha256"] or
  audit.get("optimizer_checkpoint",{}).get("sha256")!=EXPECTED["parent_optimizer_sha256"]):
  raise RuntimeError("#82 pi12 update receipt mismatch")
 return {"base_commit":BASE_COMMIT,"sts_commit":EXPECTED["sts_commit"],"stsr_commit":EXPECTED["stsr_commit"],
  "native_path":str(NATIVE),"native_sha256":file_sha256(NATIVE),"python_path":str(PYTHON),"python_sha256":file_sha256(PYTHON),
  "parent_model_sha256":file_sha256(PARENT_MODEL),"parent_model_digest":EXPECTED["parent_model_digest"],
  "parent_optimizer_sha256":file_sha256(PARENT_OPTIMIZER),"parent_update_sha256":file_sha256(PARENT_UPDATE)}

def _row(owner,phase,arm,formal,block,cohort):
 row=D._row(owner,phase=phase,arm=arm,formal=formal,block=block)
 row["search_invocation_seeds"]=[]; row["cohort"]=cohort; return row

def make_seed_table():
 rows=[]
 for s in PILOT_TRAINING: rows.append(_row(s,"training","TRAIN",False,12,"pilot-training"))
 for s in PILOT_EVALUATION:
  for a in PILOT_ARMS: rows.append(_row(s,"heldout",a,False,None,"pilot-evaluation"))
 for i,s in enumerate(FORMAL_TRAINING): rows.append(_row(s,"training","TRAIN",True,12+i//128,"training"))
 for s in FORMAL_VALIDATION:
  for a in VALIDATION_ARMS: rows.append(_row(s,"heldout",a,True,None,"validation"))
 for s in FORMAL_TEST:
  for a in TEST_ARMS: rows.append(_row(s,"heldout",a,True,None,"test"))
 return {"schema_id":"issue83-seed-table-v1","study_id":"issue-83-pgonly-scale",
  "ranges":{"pilot_training":[8320001,8320032,32],"pilot_eval":[8330001,8330008,8],
   "training":[8300001,8303072,3072],"validation":[8340001,8340064,64],"test":[8310001,8310320,320]},
  "training_blocks":[{"block_index":12+i,"owners":128,"first":FORMAL_TRAINING[128*i],
   "last":FORMAL_TRAINING[128*(i+1)-1],"model_before":f"pi{12+i}","model_after":f"pi{13+i}"} for i in range(24)],
  "pilot_arms":list(PILOT_ARMS),"validation_arms":list(VALIDATION_ARMS),"test_arms":list(TEST_ARMS),
  "search_invocation_seeds":[],"rows":rows}

def contract_document():
 return {"schema_id":"issue83-contract-v2","study_id":"issue-83-pgonly-scale","amendment_comment_id":6520801229,
  "scope":"STUDY_ONLY","parent":{"model_sha256":EXPECTED["parent_model_sha256"],"model_digest":EXPECTED["parent_model_digest"],
   "optimizer_sha256":EXPECTED["parent_optimizer_sha256"],"audit_sha256":EXPECTED["parent_update_sha256"],"step":64},
  "training":{"owners":[8300001,8303072],"count":3072,"blocks":24,"owners_per_block":128,"steps_per_block":64,
   "first_optimizer_transition":[64,128],"later_optimizer":"fresh AdamW 0->64 while model weights continue",
   "operator":"PGONLY; no J/R, Expert, elite supervision, PPO, critic, synthetic games or rule/reward/model changes",
   "battle":"Battle Search-v2@400","preserve_each_block_checkpoint":True},
  "pilot":{"training":[8320001,8320032],"eval":[8330001,8330008],"arms":list(PILOT_ARMS),"clone_update_steps":64},
  "validation":{"owners":[8340001,8340064],"games":448,"arms":list(VALIDATION_ARMS),"candidates":list(CHECKPOINT_LABELS),
   "rule":"highest mean terminal floor; ties choose earliest checkpoint","freeze_before_test":True},
  "test":{"owners":[8310001,8310320],"games":"640 when selected pi36, otherwise 960",
   "primary":"SELECTED-PARENT paired terminal floor; 20k owner-bootstrap percentile 95% CI","secondary":"pi36-PARENT"},
  "time_policy":{"pilot_forecast_includes_3072_training+24_updates+448_validation+max_960_test":True,
   "one_global_allowance_factor":1.75,"overlaps_count_once":True,"idle_gaps_excluded":True},
  "data_root":str(DATA),"limitations":["single trained lineage","changing on-policy distribution","privileged native surrogate not public/live-client parity"]}

def canonical_text_sha256(path):
 raw=Path(path).read_bytes()
 raw.decode("utf-8-sig")
 raw=raw.replace(bytes((13,10)),bytes((10,))).replace(bytes((13,)),bytes((10,)))
 if raw.startswith(bytes((239,187,191))): raw=raw[3:]
 return sha256_bytes(raw)

def repair_source_lock_sha256():
 return canonical_text_sha256(SOURCE_LOCK)

def source_files():
 paths=[STUDY81/"driver.py",STUDY81/"source-lock.json",STUDY79/"driver.py",STUDY79/"source-lock.json",
  STUDY75/"algorithm.py",STUDY75/"learning.py",STUDY75/"model.py",STUDY75/"runtime.py",
  STUDY75/"config.json",STUDY75/"stage1/config.json",STUDY75/"stage1/executor.py"]
 return {p.relative_to(REPO).as_posix():canonical_text_sha256(p) for p in paths}

def study_file_hashes():
 return {"driver.py":canonical_text_sha256(STUDY/"driver.py"),"issue83_contract.py":canonical_text_sha256(STUDY/"issue83_contract.py"),
  "run_issue83.sh":canonical_text_sha256(STUDY/"run_issue83.sh"),"config.json":canonical_text_sha256(STUDY/"config.json"),
  "stage1/config.json":canonical_text_sha256(STAGE1/"config.json"),"contract.json":canonical_text_sha256(CONTRACT),
  "seed-table.json":canonical_text_sha256(SEED_TABLE)}

def source_lock_document():
 return {"schema_id":"issue83-source-lock-repair-v3","base_commit":BASE_COMMIT,
  "text_hash_policy":"UTF-8; ignore optional BOM; normalize CRLF and bare CR to LF for locked text files; binary runtime/model/native files remain byte-exact",
  "source_epoch":{"kind":"semantics-preserving-artifact-serialization-repair",
   "supersedes_execution_commit":PREVIOUS_REPAIR_EXECUTION_COMMIT,
   "supersedes_source_lock_sha256":PREVIOUS_REPAIR_SOURCE_LOCK_SHA256,
   "previous_repair_source_lock_sha256":PREVIOUS_REPAIR_SOURCE_LOCK_SHA256,
   "original_execution_commit":ORIGINAL_EXECUTION_COMMIT,
   "original_source_lock_sha256":ORIGINAL_SOURCE_LOCK_SHA256,
   "repair_id":"json-single-serialization-writer"},
  "study_files_sha256":study_file_hashes(),"source_files_sha256":source_files(),
  "runtime_identity":source_identity(),"seed_table_sha256":canonical_text_sha256(SEED_TABLE),
  "contract_sha256":canonical_text_sha256(CONTRACT),"arms":{"pilot":list(PILOT_ARMS),"validation":list(VALIDATION_ARMS),"test":list(TEST_ARMS)},
  "concurrency":8,"one_global_allowance_set_after_pilot":True,"raw_evidence_root":str(DATA)}

def prepare():
 mb=subprocess.run(["git","merge-base","HEAD",BASE_COMMIT],cwd=REPO,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True).stdout.strip()
 if mb!=BASE_COMMIT: raise RuntimeError("wrong base source")
 original=subprocess.run(["git","merge-base","HEAD",ORIGINAL_EXECUTION_COMMIT],cwd=REPO,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True).stdout.strip()
 if original!=ORIGINAL_EXECUTION_COMMIT: raise RuntimeError("repair epoch is not descended from the exact original execution source")
 if file_sha256(ORIGINAL_SOURCE_LOCK)!=ORIGINAL_SOURCE_LOCK_SHA256:
  raise RuntimeError("original execution source lock changed; refusing to append repair epoch")
 source_identity()
 for p in (DATA,ARTIFACTS,ARTIFACTS/"pilot",ARTIFACTS/"formal",ARTIFACTS/"verify",STAGE1): p.mkdir(parents=True,exist_ok=True)
 for src,dst in ((STUDY81/"config.json",STUDY/"config.json"),(STUDY81/"stage1/config.json",STAGE1/"config.json")):
  raw=src.read_bytes()
  if dst.exists() and dst.read_bytes()!=raw: raise FileExistsError(f"refusing to replace config: {dst}")
  if not dst.exists(): dst.write_bytes(raw)
 write_json_once(SEED_TABLE,make_seed_table()); write_json_once(CONTRACT,contract_document())
 lock=source_lock_document()
 if SOURCE_LOCK.exists() and read_json(SOURCE_LOCK)!=lock: raise FileExistsError("source lock differs")
 write_once(SOURCE_LOCK,canonical(lock)+b"\n"); check_lock()
 return {"status":"PREPARED","source_lock_sha256":repair_source_lock_sha256(),"seed_sha256":canonical_text_sha256(SEED_TABLE),
  "seed_rows":len(read_json(SEED_TABLE)["rows"]),"pilot_games":48,"training_games":3072,
  "validation_games":448,"maximum_test_games":960,"parent_model_sha256":file_sha256(PARENT_MODEL),
  "parent_optimizer_sha256":file_sha256(PARENT_OPTIMIZER),"parent_update_sha256":file_sha256(PARENT_UPDATE)}

def check_lock():
 lock=read_json(SOURCE_LOCK)
 if lock.get("schema_id")!="issue83-source-lock-repair-v3" or study_file_hashes()!=lock.get("study_files_sha256"):
  raise RuntimeError("Issue 83 repair source lock mismatch")
 epoch=lock.get("source_epoch") or {}
 if (epoch.get("kind")!="semantics-preserving-artifact-serialization-repair" or
     epoch.get("supersedes_execution_commit")!=PREVIOUS_REPAIR_EXECUTION_COMMIT or
     epoch.get("supersedes_source_lock_sha256")!=PREVIOUS_REPAIR_SOURCE_LOCK_SHA256 or
     epoch.get("previous_repair_source_lock_sha256")!=PREVIOUS_REPAIR_SOURCE_LOCK_SHA256 or
     epoch.get("original_execution_commit")!=ORIGINAL_EXECUTION_COMMIT or
     epoch.get("original_source_lock_sha256")!=ORIGINAL_SOURCE_LOCK_SHA256 or
     epoch.get("repair_id")!="json-single-serialization-writer" or
     file_sha256(PREVIOUS_REPAIR_SOURCE_LOCK)!=PREVIOUS_REPAIR_SOURCE_LOCK_SHA256 or
     file_sha256(ORIGINAL_SOURCE_LOCK)!=ORIGINAL_SOURCE_LOCK_SHA256):
  raise RuntimeError("serialization repair source lock does not preserve both prior execution epochs")
 if source_files()!=lock.get("source_files_sha256") or source_identity()!=lock.get("runtime_identity"):
  raise RuntimeError("pinned sources or model identities changed")
 if canonical_text_sha256(SEED_TABLE)!=lock.get("seed_table_sha256") or canonical_text_sha256(CONTRACT)!=lock.get("contract_sha256"):
  raise RuntimeError("contract or seed table changed")
 return lock

def repair_dead_adventurer_phase3_projection(projection,actions):
 """Recover only the source-defined Dead Adventurer phase-3 search candidate.

 The unchanged native projection rejects this valid candidate because it
 insists on a reward entry even though the native state machine guarantees an
 encounter at phase 3. The native semantic mapping and exact ordered action
 binding already exist; this restores the fields discarded by that early
 rejection. Every other unsupported candidate remains fail-closed.
 """
 if not isinstance(projection,dict) or not isinstance(actions,(list,tuple)): return None
 candidates=projection.get("candidates")
 if (projection.get("schema_id")!="native-privileged-noncombat-choice-projection-v1" or
     projection.get("audience")!="training_oracle_only" or projection.get("public_api_safe") is not False or
     projection.get("native_source_commit")!=EXPECTED["sts_commit"] or
     projection.get("candidate_order_source")!="StepSimulator::legalActions; ordered occurrences are retained without deduplication" or
     projection.get("screen_identity")!={"availability":"available","source":"GameContext::screenState","value":"EVENT_SCREEN","visibility":"public_now"} or
     not isinstance(candidates,list) or len(candidates)!=2 or len(actions)!=2 or
     projection.get("candidate_count")!=2 or projection.get("supported_candidate_count")!=1 or
     projection.get("coverage_status")!="NATIVE_CHOICE_SEMANTIC_NOT_READY"):
  return None
 if projection.get("unsupported_candidates")!=[{"candidate_index":0,"kind":"event",
      "reason":"Dead Adventurer phase has no native reward entry"}]: return None
 c0,c1=candidates
 if not isinstance(c0,dict) or not isinstance(c1,dict): return None
 if c0.get("coverage_status")!="unsupported" or c0.get("reason")!="Dead Adventurer phase has no native reward entry": return None
 if c1.get("coverage_status")!="supported": return None
 f0=c0.get("features"); f1=c1.get("features")
 if not isinstance(f0,dict) or not isinstance(f1,dict) or set(f0)!={"event","choice"} or set(f1)!={"event","choice"}: return None
 event={"availability":"available","source":"GameContext::curEvent and constants/Events.h",
        "value":"Dead Adventurer","visibility":"public_now"}
 if f0.get("event")!=event or f1.get("event")!=event: return None
 if f0.get("choice")!={"availability":"unavailable","reason":"Dead Adventurer phase has no native reward entry",
                         "required_for_choice_semantics":True}: return None
 leave={"availability":"available","source":"GameContext::chooseEventOption DEAD_ADVENTURER reward codes",
        "value":{"choice_type":"leave_dead_adventurer","effect_type":"return_to_map"},"visibility":"oracle_only"}
 if f1.get("choice")!=leave: return None
 for index,(candidate,action,idx1) in enumerate(((c0,actions[0],0),(c1,actions[1],1))):
  binding=candidate.get("candidate_binding"); value=binding.get("value") if isinstance(binding,dict) else None
  native=value.get("candidate_action") if isinstance(value,dict) else None; raw=getattr(action,"raw",None)
  if (not isinstance(binding,dict) or binding.get("availability")!="available" or binding.get("visibility")!="oracle_only" or
      not isinstance(value,dict) or value.get("candidate_index")!=index or
      not isinstance(native,dict) or native.get("kind")!="event" or native.get("idx1")!=idx1 or
      getattr(action,"kind",None)!="event" or not isinstance(raw,dict) or raw.get("idx1")!=idx1): return None
 # Native setup sets phase=0 and rewards=[0,1,2]. The only transition that
 # leaves EVENT_SCREEN while preserving this event increments phase by one;
 # at phase 3, chooseEventOption's 100% (or 110% at A15+) encounter chance
 # always enters the native encounter, so rewards[3] is never read. Therefore
 # this exact out-of-range projection reason identifies reachable phase 3.
 c0["features"]["choice"]={"availability":"available",
  "source":"GameContext::chooseEventOption DEAD_ADVENTURER reward codes",
  "value":{"choice_type":"search_dead_adventurer_site",
   "effect_type":"native_phase_reward_or_encounter_transition","current_phase":3},"visibility":"oracle_only"}
 c0["coverage_status"]="supported"; c0.pop("reason",None)
 projection["coverage_status"]="supported"; projection["supported_candidate_count"]=2
 projection["unsupported_candidates"]=[]
 return {"repair_id":"dead-adventurer-phase3-native-projection-coverage","candidate_index":0,
  "derived_phase":3,"projection_reason":"Dead Adventurer phase has no native reward entry",
  "choice_type":"search_dead_adventurer_site","effect_type":"native_phase_reward_or_encounter_transition",
  "phase_reward_kind":"not-applicable: phase 3 always enters encounter before reward lookup"}


def verify_parent_and_runtime(output_dir):
 check_lock(); a,r,m,l,e=D.load_modules(output_dir,pilot=True); pol,digest,_=e._load_model("pi12")
 import torch
 model=torch.load(PARENT_MODEL,map_location="cpu",weights_only=True); opt=torch.load(PARENT_OPTIMIZER,map_location="cpu",weights_only=True)
 embedded=opt.get("model_state_dict")
 match=isinstance(embedded,dict) and embedded.keys()==model.keys() and all(torch.equal(embedded[k],model[k]) for k in model)
 steps=optimizer_steps(opt.get("optimizer_state_dict",{})); audit=read_json(PARENT_UPDATE)
 if digest!=EXPECTED["parent_model_digest"] or not match or opt.get("completed_steps")!=64 or len(steps)!=6 or set(steps)!={64}:
  raise RuntimeError("pi12 and paired AdamW are not identical/step64")
 if audit.get("result_model_digest")!=digest or audit.get("model",{}).get("sha256")!=file_sha256(PARENT_MODEL) or audit.get("optimizer_checkpoint",{}).get("sha256")!=file_sha256(PARENT_OPTIMIZER):
  raise RuntimeError("#82 final update audit mismatch")
 cfg,run,native,enc=r.initialize_native_runtime(output_dir/"native-import",sampling=False,reviewer_proof=None,stage1_binding=None)
 loaded=Path(native.__file__).resolve()
 if loaded!=NATIVE.resolve() or file_sha256(loaded)!=EXPECTED["native_sha256"]: raise RuntimeError("native module mismatch")
 rec={"schema_id":"issue83-parent-preflight-v1","status":"PASS_IMPORT_ONLY","formal_seeds_consumed":0,
  "simulator_instances_created":0,"parent_model_sha256":file_sha256(PARENT_MODEL),"parent_digest":digest,
  "parent_optimizer_sha256":file_sha256(PARENT_OPTIMIZER),"optimizer_step":64,"weights_match":match,
  "parent_audit_sha256":file_sha256(PARENT_UPDATE),"native_path":str(loaded),"native_sha256":file_sha256(loaded),
  "sts_commit":EXPECTED["sts_commit"],"stsr_commit":EXPECTED["stsr_commit"]}
 write_json_once(ARTIFACTS/f"parent-preflight-{current_source_commit()}.json",rec); return rec

def worker_main(job_path):
 global CURRENT_BLOCK,CURRENT_PILOT,SELECTED_POLICY_LABEL
 job=read_json(job_path); row=job["row"]; CURRENT_PILOT=bool(job.get("pilot"))
 CURRENT_BLOCK=int(row["block_index"]) if row.get("phase")=="training" else None
 if row.get("arm")=="SELECTED":
  sel=read_json(Path(job["artifact_root"])/"checkpoint-selection.json")
  if sel.get("status")!="FROZEN_BEFORE_TEST": raise RuntimeError("missing frozen selection")
  SELECTED_POLICY_LABEL=sel["selected_checkpoint"]
 return int(D.worker_main(job_path))

def optimizer_steps(state):
 return sorted(int(v["step"].item()) for v in state.get("state",{}).values() if "step" in v)


def train_update(block,examples,*,root,pilot,clock=None,source_epoch_summary=None):
 global CURRENT_BLOCK
 CURRENT_BLOCK=block; alg,rt,m,l,e=D.load_modules(root,pilot=pilot); count=32 if pilot else 128
 frozen,src,_=e._load_model(f"pi{block}"); ownerhash=hashlib.sha256(",".join(str(int(x.owner_seed)) for x in examples).encode("ascii")).hexdigest()
 orderseed=derive_seed("block",pilot,block); ck=root/"models"/f"block-{block}-step.pt"; target=e._model_path(f"pi{block+1}")
 if len(examples)!=count: raise RuntimeError("wrong block owner count")
 rp=root/"models"/f"issue83-update-{block}.json"
 if rp.exists():
  rec=read_json(rp); _,dst,_=e._load_model(f"pi{block+1}")
  if rec.get("source_model_digest")!=src or rec.get("result_model_digest")!=dst: raise RuntimeError("existing update chain mismatch")
  return rec
 resume=__import__("torch").load(ck,map_location="cpu",weights_only=True) if ck.exists() else None
 def save_step(step,model,opt,rng,order,diagnostics):
  if clock and clock.expired(): raise GlobalWallExpired("global active wall allowance expired")
  e._atomic_torch_save(ck,{"schema_id":e.RESUME_SCHEMA,"block_index":block,"completed_steps":step,
   "block_order_seed":orderseed,"source_model_digest":src,"owner_seed_sha256":ownerhash,
   "model_state_dict":model.state_dict(),"optimizer_state_dict":opt.state_dict(),"python_rng_state":rng,
   "owner_order":[int(v) for v in order],"diagnostics":[vars(v) for v in diagnostics]})
 start=time.monotonic(); result=l.train_policy_iteration_block(frozen,examples,block_order_seed=orderseed,step_callback=save_step,resume_state=resume)
 ds=[vars(x) for x in result.diagnostics]
 if len(ds)!=64 or any(x["loss_elite"]!=0.0 or x["grad_norm_elite"]!=0.0 or x["elite_owner_count"]!=0 or x["elite_root_count"]!=0 for x in ds):
  raise RuntimeError("elite contribution in PGONLY block")
 model=e._save_model_once(result.model,target,m.model_digest); saved=__import__("torch").load(ck,map_location="cpu",weights_only=True)
 steps=optimizer_steps(saved["optimizer_state_dict"]); after=128 if block==12 else 64
 if len(steps)!=6 or set(steps)!={after}: raise RuntimeError("wrong optimizer step count")
 rec={"schema_id":"issue83-update-v1","status":"SUCCEEDED","block_index":block,"owner_count":count,"completed_steps_in_this_block":64,
  "optimizer_step_before_block":64 if block==12 else 0,"optimizer_step_after_block":after,
  "optimizer_lineage":"inherited #82 final AdamW" if block==12 else "fresh AdamW; weights continue",
  "optimizer_inherited_state_sha256":file_sha256(PARENT_OPTIMIZER) if block==12 else None,
  "owner_seed_sha256":ownerhash,"source_model_digest":result.source_model_digest,"result_model_digest":result.result_model_digest,
  "model":model,"optimizer_checkpoint":{"path":ck.relative_to(root).as_posix(),"sha256":file_sha256(ck),"steps":steps},
  "diagnostics":ds,"elapsed_seconds":time.monotonic()-start,"expert_decisions":0,"search_invocations":0,"pilot_nonformal":pilot,
   "execution_source_commit":current_source_commit(),"source_lock_sha256":repair_source_lock_sha256(),
   "training_input_epoch_summary":source_epoch_summary}
 write_json_once(rp,rec); return rec

def rows_for(table,*,phase,formal,block=None,cohort=None,arms=None):
 return [r for r in table["rows"] if r["phase"]==phase and r["formal"] is bool(formal) and
  (block is None or r.get("block_index")==block) and (cohort is None or r.get("cohort")==cohort) and
  (arms is None or r.get("arm") in arms)]

def verify_update(block,rows,root):
 path=root/"models"/f"issue83-update-{block}.json"
 if not path.is_file(): return None
 rec=read_json(path); ownerhash=hashlib.sha256(",".join(map(str,sorted(int(x["owner_seed"]) for x in rows))).encode("ascii")).hexdigest()
 before=64 if block==12 else 0; after=128 if block==12 else 64
 if (rec.get("status")!="SUCCEEDED" or rec.get("block_index")!=block or rec.get("owner_count")!=128 or
     rec.get("completed_steps_in_this_block")!=64 or rec.get("optimizer_step_before_block")!=before or
     rec.get("optimizer_step_after_block")!=after or rec.get("owner_seed_sha256")!=ownerhash):
  raise RuntimeError(f"block {block} update receipt mismatch")
 inherited=EXPECTED["parent_optimizer_sha256"] if block==12 else None
 if rec.get("optimizer_inherited_state_sha256")!=inherited: raise RuntimeError(f"block {block} optimizer lineage mismatch")
 if block>=26:
  epoch_path=root/"training-block-source-epochs"/f"block-{block}.json"
  summary=rec.get("training_input_epoch_summary") or {}
  if (rec.get("execution_source_commit")!=current_source_commit() or rec.get("source_lock_sha256")!=repair_source_lock_sha256() or
      summary.get("path")!=epoch_path.relative_to(root).as_posix() or summary.get("sha256")!=file_sha256(epoch_path)):
   raise RuntimeError(f"block {block} update does not bind its mixed/source epoch owner receipts")
 ds=rec.get("diagnostics",[])
 if len(ds)!=64 or any(d.get("loss_elite")!=0.0 or d.get("grad_norm_elite")!=0.0 or d.get("elite_owner_count")!=0 or d.get("elite_root_count")!=0 for d in ds):
  raise RuntimeError(f"block {block} diagnostics contain elite contributions")
 global CURRENT_BLOCK
 CURRENT_BLOCK=block; a,r,m,l,e=D.load_modules(root,pilot=False)
 _,src,_=e._load_model(f"pi{block}"); _,dst,_=e._load_model(f"pi{block+1}")
 ck=root/"models"/f"block-{block}-step.pt"; state=__import__("torch").load(ck,map_location="cpu",weights_only=True)
 steps=optimizer_steps(state.get("optimizer_state_dict",{})); torch=__import__("torch")
 saved_model=torch.load(e._model_path(f"pi{block+1}"),map_location="cpu",weights_only=True)
 embedded=state.get("model_state_dict")
 weights_match=isinstance(embedded,dict) and embedded.keys()==saved_model.keys() and all(torch.equal(embedded[k],saved_model[k]) for k in saved_model)
 if (rec.get("source_model_digest")!=src or rec.get("result_model_digest")!=dst or
     rec.get("model",{}).get("sha256")!=file_sha256(e._model_path(f"pi{block+1}")) or
     rec.get("model",{}).get("model_digest")!=dst or file_sha256(ck)!=rec.get("optimizer_checkpoint",{}).get("sha256") or
     len(steps)!=6 or set(steps)!={after} or state.get("block_index")!=block or
     state.get("completed_steps")!=64 or state.get("source_model_digest")!=src or
     state.get("owner_seed_sha256")!=ownerhash or not weights_match):
  raise RuntimeError(f"block {block} checkpoint/model chain mismatch")
 return rec

def save_training_progress(root,package,clock,updates,current_block=None):
 atomic_json(root/"training-progress.json",{"schema_id":"issue83-training-progress-v1","source_branch_sha":package,
  "status":"ACTIVE","completed_training_owners":sum(x["owner_count"] for x in updates),"completed_blocks":updates,
  "current_block":current_block,"active_wall_seconds":clock.elapsed(),"allowance_seconds":clock.allowance})

def _phase_reports(root,rows,package):
 out=[]
 for row in rows:
  item=D.existing_success_record(root,row,package)
  if item is None: raise RuntimeError(f"missing terminal {row['unit_id']}")
  report,path=item
  if report.get("expert_decisions")!=0 or report.get("search")!=[] or report.get("terminal_outcome") is None or report.get("terminal_floor") is None:
   raise RuntimeError(f"invalid terminal {row['unit_id']}")
  obs_path=path.parent/"natural-owner-observation.json"; obs=read_json(obs_path)
  if obs.get("seed")!=row["owner_seed"] or obs.get("arm")!=row["arm"] or obs.get("search_root_callback") is not False:
   raise RuntimeError("native observation identity mismatch")
  out.append({"owner_seed":int(row["owner_seed"]),"arm":row["arm"],"terminal_floor":int(report["terminal_floor"]),
   "terminal_outcome":report["terminal_outcome"],"act2_plus":bool(report["act2_plus"]),"act3_plus":bool(report["act3_plus"]),
   "act4_reached":bool(report["act4_reached"]),"heart_victory":bool(report["heart_victory"]),
   "completed_transitions":int(report["completed_transitions"]),"action_counts":obs.get("action_kind_counts") or {},
   "result_path":path.relative_to(root).as_posix(),"result_sha256":file_sha256(path),
   "observation_path":obs_path.relative_to(root).as_posix(),"observation_sha256":file_sha256(obs_path)})
 return out

def _per_arm(rows):
 out={}
 for arm in sorted(set(x["arm"] for x in rows)):
  part=[x for x in rows if x["arm"]==arm]; kinds={}
  for x in part:
   for k,v in x["action_counts"].items(): kinds[k]=kinds.get(k,0)+int(v)
  out[arm]={"owners":len(part),"mean_terminal_floor":statistics.fmean(x["terminal_floor"] for x in part),
   "act2_plus":sum(x["act2_plus"] for x in part),"act3_plus":sum(x["act3_plus"] for x in part),
   "act4_reached":sum(x["act4_reached"] for x in part),"heart_victory":sum(x["heart_victory"] for x in part),
   "completed_transitions":sum(x["completed_transitions"] for x in part),"action_counts":kinds}
 return out

def _paired_bootstrap(rows,left,right,label):
 import numpy as np
 group={}
 for x in rows: group.setdefault(x["owner_seed"],{})[x["arm"]]=x
 pairs=[(float(v[left]["terminal_floor"]),float(v[right]["terminal_floor"])) for v in group.values() if left in v and right in v]
 if len(pairs)!=320: raise RuntimeError(f"wrong pair count: {label}")
 diff=np.asarray([a-b for a,b in pairs]); seed=derive_seed("bootstrap",label,"terminal_floor")
 means=np.random.default_rng(seed).choice(diff,size=(20000,320),replace=True).mean(axis=1)
 return {"paired_owner_count":320,"mean_difference":float(diff.mean()),
  "ci95":[float(np.quantile(means,.025)),float(np.quantile(means,.975))],
  "bootstrap_replicates":20000,"bootstrap_seed":seed,"interval":"percentile 95%"}

def pilot():
 check_lock(); package=current_source_commit(); root=ARTIFACTS/"pilot"/package
 report_path=root/"pilot-report.json"; policy_path=root/"time-policy.json"
 if report_path.exists(): return read_json(report_path)
 clock=D.ActiveClock(f"pilot-{package[:12]}",None)
 try:
  pre=verify_parent_and_runtime(root/"verify"); table=read_json(SEED_TABLE)
  tr=rows_for(table,phase="training",formal=False,cohort="pilot-training")
  tp=run_phase("training",clock,lambda:D.run_units(tr,root=root,pilot=True,clock=clock,package_commit=package),package,root)
  def update_op():
   global CURRENT_BLOCK
   CURRENT_BLOCK=12; check_lock(); a,r,m,l,e=D.load_modules(root,pilot=True)
   examples=D._training_examples(tr,root=root,executor=e,package_commit=package)
   rec=train_update(12,examples,root=root,pilot=True,clock=clock); del examples
   return {"status":"SUCCEEDED","block_index":12,"owner_count":rec["owner_count"],"completed_steps":64,"result_model_digest":rec["result_model_digest"]}
  up=run_phase("update",clock,update_op,package,root)
  ev=rows_for(table,phase="heldout",formal=False,cohort="pilot-evaluation")
  ep=run_phase("evaluation",clock,lambda:D.run_units(ev,root=root,pilot=True,clock=clock,package_commit=package),package,root)
  reports=[D.existing_success(root,x,package) for x in tr+ev]
  if len(reports)!=48 or any(x is None or x.get("expert_decisions")!=0 or x.get("search")!=[] or x.get("terminal_outcome") is None for x in reports):
   raise RuntimeError("pilot requires 48 complete natural PGONLY terminals")
  measured=clock.elapsed(); tw=float(tp["elapsed_active_seconds"]); uw=float(up["elapsed_active_seconds"]); ew=float(ep["elapsed_active_seconds"])
  trainproj=tw*96*1.35; updateproj=uw*24; evalproj=ew*88*1.25
  subtotal=trainproj+updateproj+evalproj; retry=subtotal*.10; analysis=max(300.0,subtotal*.05)
  projection=measured+subtotal+retry+analysis; allowance=math.ceil(projection*1.75)
  tc=D._max_concurrent_workers(root,tr); ec=D._max_concurrent_workers(root,ev)
  if max(tc,ec)>8 or min(tc,ec)!=8: raise RuntimeError("pilot did not attain registered 8-worker concurrency")
  upd=read_json(root/"models/issue83-update-12.json")
  policy={"schema_id":"issue83-wall-policy-v1","source_branch_sha":package,"source_lock_sha256":repair_source_lock_sha256(),
   "seed_table_sha256":canonical_text_sha256(SEED_TABLE),"actual_pilot_active_seconds":measured,
   "max_training_workers":tc,"max_evaluation_workers":ec,
   "projection_components_seconds":{"pilot_actual":measured,"3072_training_factor_1_35":trainproj,
    "24_updates":updateproj,"448_validation_plus_960_test_factor_1_25":evalproj,"retry_margin_10_percent":retry,"analysis":analysis},
   "complete_task_projection_seconds":projection,"complete_task_projection_hours":projection/3600,
   "one_global_active_wall_allowance_seconds":allowance,"allowance_factor":1.75,
   "clock_rule":"union of active task-program wall intervals; overlaps count once; idle gaps excluded",
   "training_wall_distribution":distribution(list(tp["result_summary"].get("row_wall_seconds",{}).values())),
   "evaluation_wall_distribution":distribution(list(ep["result_summary"].get("row_wall_seconds",{}).values())),
   "training_tries":tp["result_summary"].get("tries",{}),"evaluation_tries":ep["result_summary"].get("tries",{}),
   "disposable_update_receipt":upd,"limitations":["32 pilot training and 16 debug evaluation games; future timings are projections",
    "later-policy/retry factors are projected","forecast includes max 960 test games","clone is not efficacy evidence"]}
  write_json_once(policy_path,policy)
  result={"schema_id":"issue83-pilot-v1","status":"PILOT_COMPLETE_NONFORMAL","source_branch_sha":package,
   "source_lock_sha256":repair_source_lock_sha256(),"seed_table_sha256":canonical_text_sha256(SEED_TABLE),"parent_preflight":pre,
   "training":{"games":32,"complete_terminals":32,"active_seconds":tw,"max_workers":tc,
    "row_wall_seconds":tp["result_summary"].get("row_wall_seconds",{}),"tries":tp["result_summary"].get("tries",{})},
   "update":{"owners":upd["owner_count"],"steps":64,"optimizer_before":64,"optimizer_after":128,
    "inherited_optimizer_sha256":upd["optimizer_inherited_state_sha256"],"model_digest":upd["result_model_digest"],"formal":False},
   "parent_clone_eval":{"games":16,"complete_terminals":16,"active_seconds":ew,"max_workers":ec,
    "row_wall_seconds":ep["result_summary"].get("row_wall_seconds",{}),"efficacy_inference":False},
   "pilot_active_seconds":measured,"projection_seconds":projection,"projection_hours":projection/3600,
   "one_global_active_wall_allowance_seconds":allowance,"time_policy_path":policy_path.relative_to(DATA).as_posix(),
   "formal_results":False,"efficacy_inference":False}
  write_json_once(report_path,result); return result
 finally:
  elapsed=clock.close(); final=finalize_pilot_clock_outputs(report_path,policy_path,f"pilot-{package[:12]}",elapsed)
  if final is not None and "result" in locals(): result.clear(); result.update(final)

def _assemble_pilot_report(package):
 check_lock(); root=ARTIFACTS/"pilot"/package; report_path=root/"pilot-report.json"
 if report_path.is_file(): return read_json(report_path)
 policy_path=root/"time-policy.json"; policy=read_json(policy_path); table=read_json(SEED_TABLE)
 lock_sha=repair_source_lock_sha256(); seed_sha=canonical_text_sha256(SEED_TABLE)
 if (policy.get("source_branch_sha")!=package or policy.get("source_lock_sha256")!=lock_sha or
     policy.get("seed_table_sha256")!=seed_sha): raise RuntimeError("pilot timing receipt is not bound to this source lock and seed table")
 lock_bytes=subprocess.run(["git","show",f"{package}:{STUDY.relative_to(REPO).as_posix()}/source-lock.json"],
  cwd=REPO,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE).stdout
 if sha256_bytes(lock_bytes)!=lock_sha: raise RuntimeError("pilot commit does not contain the measured source lock")
 write_once(root/"source-lock-execution.json",lock_bytes)
 phase={name:read_json(root/f"pilot-phase-{name}.json") for name in ("training","update","evaluation")}
 if any(x.get("status")!="SUCCEEDED" or x.get("package_commit")!=package for x in phase.values()):
  raise RuntimeError("pilot closeout requires all three source-bound phases")
 tr=rows_for(table,phase="training",formal=False,cohort="pilot-training")
 ev=rows_for(table,phase="heldout",formal=False,cohort="pilot-evaluation")
 if len(tr)!=32 or len(ev)!=16: raise RuntimeError("pilot roster must be 32 train plus 16 two-arm heldout games")
 results=_phase_reports(root,tr+ev,package)
 if len(results)!=48 or any(x["terminal_outcome"] is None or x["terminal_floor"] is None for x in results):
  raise RuntimeError("pilot requires 48 complete natural-game terminals")
 _,_,_,_,executor=D.load_modules(root,pilot=True)
 examples=D._training_examples(tr,root=root,executor=executor,package_commit=package)
 if len(examples)!=32 or len({int(x.owner_seed) for x in examples})!=32:
  raise RuntimeError("pilot training features are incomplete or duplicate owners")
 del examples
 update=read_json(root/"models/issue83-update-12.json")
 ck=root/"models/block-12-step.pt"; saved=__import__("torch").load(ck,map_location="cpu",weights_only=True)
 _,clone_digest,_=executor._load_model("pi13"); steps=optimizer_steps(saved.get("optimizer_state_dict",{}))
 diag=update.get("diagnostics",[])
 if (update.get("status")!="SUCCEEDED" or update.get("owner_count")!=32 or update.get("completed_steps_in_this_block")!=64 or
     update.get("optimizer_step_before_block")!=64 or update.get("optimizer_step_after_block")!=128 or
     update.get("optimizer_inherited_state_sha256")!=EXPECTED["parent_optimizer_sha256"] or
     update.get("result_model_digest")!=clone_digest or update.get("optimizer_checkpoint",{}).get("sha256")!=file_sha256(ck) or
     saved.get("completed_steps")!=64 or saved.get("block_index")!=12 or len(steps)!=6 or set(steps)!={128} or
     len(diag)!=64 or any(d.get("loss_elite")!=0.0 or d.get("grad_norm_elite")!=0.0 or d.get("elite_owner_count")!=0 or d.get("elite_root_count")!=0 for d in diag)):
  raise RuntimeError("pilot disposable PGONLY update or inherited AdamW read-back failed")
 if any(p.is_dir() for p in (ARTIFACTS/"formal").glob("*/units/*/attempt-*")):
  raise RuntimeError("formal seed attempts exist before pilot closeout")
 tc=D._max_concurrent_workers(root,tr); ec=D._max_concurrent_workers(root,ev)
 if tc!=CONCURRENCY or ec!=CONCURRENCY: raise RuntimeError("pilot did not attain the registered effective concurrency")
 preflight=read_json(ARTIFACTS/f"parent-preflight-{package}.json")
 if (preflight.get("status")!="PASS_IMPORT_ONLY" or preflight.get("formal_seeds_consumed")!=0 or
     preflight.get("parent_model_sha256")!=EXPECTED["parent_model_sha256"] or
     preflight.get("parent_optimizer_sha256")!=EXPECTED["parent_optimizer_sha256"] or
     preflight.get("native_sha256")!=EXPECTED["native_sha256"]):
  raise RuntimeError("pilot parent/native import-only preflight mismatch")
 result={"schema_id":"issue83-pilot-v1","status":"PILOT_COMPLETE_NONFORMAL","source_branch_sha":package,
  "source_lock_sha256":lock_sha,"seed_table_sha256":seed_sha,"parent_preflight":preflight,
  "training":{"games":32,"complete_terminals":32,"elapsed_active_seconds":float(phase["training"]["elapsed_active_seconds"]),
   "max_workers":tc,"row_wall_seconds":phase["training"]["result_summary"].get("row_wall_seconds",{}),
   "tries":phase["training"]["result_summary"].get("tries",{}),
   "owners":[x for x in results if x["arm"]=="TRAIN"]},
  "update":{"owners":32,"steps":64,"optimizer_before":64,"optimizer_after":128,
   "inherited_optimizer_sha256":update["optimizer_inherited_state_sha256"],"model_digest":clone_digest,
   "checkpoint_sha256":file_sha256(ck),"elite_diagnostics_zero":True,"formal":False},
  "parent_clone_eval":{"games":16,"complete_terminals":16,
   "elapsed_active_seconds":float(phase["evaluation"]["elapsed_active_seconds"]),"max_workers":ec,
   "row_wall_seconds":phase["evaluation"]["result_summary"].get("row_wall_seconds",{}),
   "tries":phase["evaluation"]["result_summary"].get("tries",{}),
   "owners":[x for x in results if x["arm"] in PILOT_ARMS],"efficacy_inference":False},
  "pilot_active_seconds":float(policy["actual_pilot_active_seconds"]),
  "complete_task_projection_seconds":float(policy["complete_task_projection_seconds"]),
  "complete_task_projection_hours":float(policy["complete_task_projection_hours"]),
  "one_global_active_wall_allowance_seconds":int(policy["one_global_active_wall_allowance_seconds"]),
  "allowance_factor":float(policy["allowance_factor"]),
  "time_policy_path":policy_path.relative_to(DATA).as_posix(),
  "formal_results":False,"efficacy_inference":False,
  "limitations":["32 pilot training and 16 debug evaluation games; later timings are projections",
   "later-policy and retry factors are projections","forecast covers 448 validation and up to 960 test games","disposable clone is not efficacy evidence"]}
 write_once(report_path,canonical(result)+b"\n"); return result


def finalize_pilot(package):
 check_lock(); root=ARTIFACTS/"pilot"/package; report_path=root/"pilot-report.json"
 if report_path.is_file():
  report=read_json(report_path)
  if report.get("source_branch_sha")!=package or report.get("source_lock_sha256")!=repair_source_lock_sha256():
   raise RuntimeError("existing pilot report source identity mismatch")
  return report
 recovery_mode=f"pilot-report-recovery-{package[:12]}"; recovery_clock=D.ActiveClock(recovery_mode,None)
 result=None
 try:
  result=_assemble_pilot_report(package); return result
 finally:
  recovery_clock.close(); recovery_record=closed_active_wall_clock(recovery_mode)
  recovery_session=read_json(recovery_clock.session_path)
  if report_path.is_file():
   recovered=read_json(report_path); finalization=recovered.setdefault("finalization",{})
   finalization.update({"method":"receipt-only; no games, simulator workers, or optimizer update rerun",
    "report_recovery_active_wall_status":"MEASURED","report_recovery_active_wall_session_seconds":float(recovery_session["session_seconds"]),
    "report_recovery_active_wall_cumulative_seconds":recovery_record["elapsed_seconds"],
    "report_recovery_active_wall_clock_path":str(recovery_record["path"]),
    "report_recovery_active_wall_clock_sha256":recovery_record["sha256"],
    "report_recovery_wall_session_path":str(recovery_clock.session_path),
    "report_recovery_wall_session_sha256":file_sha256(recovery_clock.session_path),
    "post_clock_closeout_active_wall_status":"UNMEASURED","post_clock_closeout_active_wall_seconds":None})
   atomic_json(report_path,recovered)
   if result is not None: result.clear(); result.update(recovered)

def analyze_validation(root,table,package,*,data_root):
 rows=rows_for(table,phase="heldout",formal=True,cohort="validation"); out=_phase_reports(data_root,rows,package)
 if len(out)!=448: raise RuntimeError("validation requires 448 complete games")
 counts={a:sum(x["arm"]==a for x in out) for a in VALIDATION_ARMS}
 if counts!={a:64 for a in VALIDATION_ARMS}: raise RuntimeError(f"wrong validation arm counts: {counts}")
 means={a:statistics.fmean(x["terminal_floor"] for x in out if x["arm"]==a) for a in VALIDATION_ARMS}
 candidates=[{"arm":a,"checkpoint":a.lower(),"mean_terminal_floor":means[a]} for a in VALIDATION_ARMS if a!="PARENT"]
 chosen=max(enumerate(candidates),key=lambda x:(x[1]["mean_terminal_floor"],-x[0]))[1]
 _,_,_,_,e=D.load_modules(root,pilot=False); digests={}
 for label in ("pi12",)+CHECKPOINT_LABELS: _,digests[label],_=e._load_model(label)
 return {"schema_id":"issue83-validation-selection-v1","status":"FROZEN_BEFORE_TEST","source_branch_sha":package,
  "source_lock_sha256":repair_source_lock_sha256(),"seed_table_sha256":canonical_text_sha256(SEED_TABLE),"validation_games":448,
  "candidate_order":list(CHECKPOINT_LABELS),"tie_break":"earliest checkpoint",
  "selection_rule":"highest mean terminal floor among six new checkpoints","selected_checkpoint":chosen["checkpoint"],
  "selected_arm":chosen["arm"],"selected_mean_terminal_floor":chosen["mean_terminal_floor"],
  "candidate_means":candidates,"parent_mean_terminal_floor":means["PARENT"],"per_arm":_per_arm(out),
  "model_digests":digests,"owner_arm_outcomes":out,"test_outcomes_opened":False,
  "training_source_epoch_manifest_sha256":file_sha256(root/"source-epoch-training-complete.json")}

def freeze_selection(root,table,package,*,data_root):
 p=root/"checkpoint-selection.json"
 if p.exists():
  old=read_json(p)
  if (old.get("status")!="FROZEN_BEFORE_TEST" or old.get("source_branch_sha")!=package or
      old.get("source_lock_sha256")!=repair_source_lock_sha256() or old.get("seed_table_sha256")!=canonical_text_sha256(SEED_TABLE) or
      old.get("selected_checkpoint") not in CHECKPOINT_LABELS or len(old.get("owner_arm_outcomes",[]))!=448 or
      old.get("test_outcomes_opened") is not False or
      old.get("training_source_epoch_manifest_sha256")!=file_sha256(root/"source-epoch-training-complete.json")):
   raise RuntimeError("selection receipt mismatch")
  return old
 result=analyze_validation(root,table,package,data_root=data_root); write_once(p,canonical(result)+b"\n"); return result

def _has_attempt(root,row):
 folder=root/"units"/D.unit_key(row["unit_id"])
 return folder.exists() and any(folder.glob("attempt-*"))

def analyze_test(root,table,package,selection,*,data_root):
 arms=["PARENT","SELECTED"]+(["FINAL"] if selection["selected_checkpoint"]!="pi36" else [])
 if selection["selected_checkpoint"]=="pi36":
  redundant=rows_for(table,phase="heldout",formal=True,cohort="test",arms=["FINAL"])
  if any(_has_attempt(data_root,row) for row in redundant): raise RuntimeError("fixed pi36 was redundantly tested when it equals SELECTED")
 rows=rows_for(table,phase="heldout",formal=True,cohort="test",arms=arms); out=_phase_reports(data_root,rows,package)
 if len(out)!=320*len(arms): raise RuntimeError("wrong test terminal count")
 if {a:sum(x["arm"]==a for x in out) for a in arms}!={a:320 for a in arms}: raise RuntimeError("wrong test arm size")
 primary=_paired_bootstrap(out,"SELECTED","PARENT","SELECTED-PARENT")
 secondary=None if "FINAL" not in arms else _paired_bootstrap(out,"FINAL","PARENT","FINAL-PARENT")
 return {"schema_id":"issue83-test-analysis-v1","status":"COMPLETE","source_branch_sha":package,
  "source_lock_sha256":repair_source_lock_sha256(),"seed_table_sha256":canonical_text_sha256(SEED_TABLE),
  "selection_receipt_sha256":file_sha256(root/"checkpoint-selection.json"),"test_owners":320,
  "complete_test_games":len(out),"arms":arms,"primary_registered_contrast":"SELECTED-PARENT terminal_floor",
  "primary":primary,"secondary_fixed_pi36_minus_parent":secondary,"per_arm":_per_arm(out),"owner_arm_outcomes":out,
  "selected_checkpoint":selection["selected_checkpoint"],"fixed_final_checkpoint":"pi36",
  "training_source_epoch_manifest_sha256":file_sha256(root/"source-epoch-training-complete.json"),
  "limitations":["paired owners do not ensure shared future hidden RNG","single training lineage","privileged native surrogate is not public/live-client parity"]}


def source_bound_training_receipt(data_root,row,package):
 old=(ORIGINAL_EXECUTION_COMMIT,ORIGINAL_SOURCE_LOCK_SHA256); new=(package,repair_source_lock_sha256())
 block=int(row["block_index"]); folder=Path(data_root)/"units"/D.unit_key(row["unit_id"])
 if not folder.is_dir(): return None
 matches=[]
 for result in sorted(folder.glob("attempt-*/result.json")):
  value=read_json(result)
  if value.get("status")!="SUCCEEDED": continue
  if value.get("unit_id")!=row["unit_id"] or value.get("owner_seed")!=row["owner_seed"]:
   raise RuntimeError(f"training receipt bound to wrong owner: {result}")
  identity=(value.get("package_commit"),value.get("source_lock_sha256"))
  if identity==old and block>26: raise RuntimeError(f"old receipt outside source epoch: {result}")
  if identity not in (old,new): raise RuntimeError(f"unregistered training receipt source: {result}")
  if (value.get("phase")!=row["phase"] or value.get("arm")!=row["arm"] or value.get("formal") is not True or
      value.get("expert_decisions")!=0 or value.get("search")!=[] or value.get("terminal_outcome") is None or value.get("terminal_floor") is None):
   raise RuntimeError(f"training receipt violates contract: {result}")
  job=read_json(result.parent/"worker-job.json"); jr=job.get("row") or {}
  for k in ("unit_id","owner_seed","game_seed","policy_rng_seed","block_index","phase","arm","formal","cohort"):
   if jr.get(k)!=row.get(k): raise RuntimeError(f"worker job differs from registered field {k}: {result}")
  if (job.get("package_commit")!=identity[0] or job.get("source_lock_sha256")!=identity[1]):
   raise RuntimeError(f"worker job/result source mismatch: {result}")
  matches.append((value,result,identity))
 if len(matches)>1: raise RuntimeError(f"duplicate successful receipt for {row['unit_id']}")
 return matches[0] if matches else None


def training_examples_across_epochs(rows,data_root,executor,package):
 import zlib
 examples=[]
 for row in sorted(rows,key=lambda item:int(item["owner_seed"])):
  found=source_bound_training_receipt(data_root,row,package)
  if found is None: raise RuntimeError(f"training block lacks owner {row['unit_id']}")
  report,path,_=found; pointer=report.get("training_features")
  if not isinstance(pointer,dict) or pointer.get("compression")!="zlib": raise RuntimeError(f"missing features: {row['unit_id']}")
  compressed=(Path(data_root)/pointer["path"]).read_bytes()
  if sha256_bytes(compressed)!=pointer["sha256"]: raise RuntimeError(f"compressed feature hash mismatch: {row['unit_id']}")
  raw=zlib.decompress(compressed)
  if sha256_bytes(raw)!=pointer["uncompressed_sha256"]: raise RuntimeError(f"feature payload hash mismatch: {row['unit_id']}")
  example=executor._training_example_from_payload(json.loads(raw))
  if example.owner_seed!=row["owner_seed"] or not example.base.complete: raise RuntimeError(f"wrong/incomplete owner features: {row['unit_id']}")
  examples.append(example)
 return examples


def training_block_source_epoch(root,data_root,rows,block,package):
 receipts=[]; counts={}; old=(ORIGINAL_EXECUTION_COMMIT,ORIGINAL_SOURCE_LOCK_SHA256)
 for row in sorted(rows,key=lambda item:int(item["owner_seed"])):
  found=source_bound_training_receipt(data_root,row,package)
  if found is None: raise RuntimeError(f"block {block} lacks owner {row['unit_id']}")
  report,path,identity=found; epoch="original-3951ee0" if identity==old else "repair-"+package[:12]
  counts[epoch]=counts.get(epoch,0)+1; pointer=report.get("training_features") or {}
  receipts.append({"unit_id":row["unit_id"],"owner_seed":int(row["owner_seed"]),"execution_source_commit":identity[0],
   "source_lock_sha256":identity[1],"result_path":path.relative_to(Path(data_root)).as_posix(),
   "result_sha256":file_sha256(path),"training_features_path":pointer.get("path"),"training_features_sha256":pointer.get("sha256")})
 expected=({"original-3951ee0":128} if block<=25 else
  {"original-3951ee0":127,"repair-"+package[:12]:1} if block==26 else {"repair-"+package[:12]:128})
 if counts!=expected: raise RuntimeError(f"block {block} source counts differ: {counts}, expected {expected}")
 record={"schema_id":"issue83-training-block-source-epochs-v1","status":"VERIFIED_BEFORE_UPDATE",
  "block_index":block,"owner_count":len(rows),"source_epoch_counts":counts,"source_epoch_receipts":receipts,
  "registered_expected_counts":expected,"raw_evidence_root":str(data_root)}
 path=Path(root)/"training-block-source-epochs"/f"block-{block}.json"
 write_json_once(path,record)
 return {"path":path.relative_to(root).as_posix(),"sha256":file_sha256(path),"counts":counts}


def write_source_epoch_plan(root,package,allowance):
 plan={"schema_id":"issue83-source-epoch-plan-v1","status":"LOCKED_BEFORE_RESUMED_FORMAL",
  "repair_execution_source_commit":package,"repair_source_lock_sha256":repair_source_lock_sha256(),
  "original_execution_source_commit":ORIGINAL_EXECUTION_COMMIT,"original_source_lock_sha256":ORIGINAL_SOURCE_LOCK_SHA256,
  "native_source_commit":EXPECTED["sts_commit"],"native_module_sha256":EXPECTED["native_sha256"],
  "raw_training_receipts_remain_at":str(LEGACY_FORMAL_ROOT),
  "allocation":{"blocks_12_to_25":{"source":ORIGINAL_EXECUTION_COMMIT,"owners_per_block":128},
   "block_26":{"original_source_receipts":127,"repair_source_receipts":1,"repair_owner_seed":8301831},
   "blocks_27_to_35":{"source":package,"owners_per_block":128},
   "expected_total_original_source_receipts":1919,"expected_total_repair_source_receipts":1153},
  "preserve_original_failures_and_interrupted_attempt":{"owner_seed":8301831,"failed_attempts":13,"interrupted_attempt":14},
  "active_wall_allowance_seconds":allowance,"training_data_not_copied":True,"original_epoch_evidence_not_overwritten":True}
 write_json_once(Path(root)/"source-epoch-plan.json",plan)


def training_update_summary(rec,path,source,lock):
 return {"block_index":int(rec["block_index"]),"status":rec["status"],"owner_count":int(rec["owner_count"]),
  "execution_source_commit":source,"source_lock_sha256":lock,"update_receipt_path":str(path),
  "update_receipt_sha256":file_sha256(path),"result_model_digest":rec["result_model_digest"],
  "optimizer_checkpoint_sha256":rec["optimizer_checkpoint"]["sha256"]}


def complete_training_epoch_manifest(root,package):
 paths=[Path(root)/"training-block-source-epochs"/f"block-{b}.json" for b in range(12,36)]
 records=[read_json(x) for x in paths]; total={}; owners=0
 for rec in records:
  owners+=int(rec["owner_count"])
  for epoch,count in rec["source_epoch_counts"].items(): total[epoch]=total.get(epoch,0)+int(count)
 expected={"original-3951ee0":1919,"repair-"+package[:12]:1153}
 if owners!=3072 or total!=expected: raise RuntimeError(f"source epoch totals differ: {owners}, {total}")
 manifest={"schema_id":"issue83-source-epoch-training-complete-v1","status":"ALL_3072_TRAINING_RECEIPTS_AND_24_UPDATES_SOURCE_BOUND",
  "repair_execution_source_commit":package,"repair_source_lock_sha256":repair_source_lock_sha256(),
  "original_execution_source_commit":ORIGINAL_EXECUTION_COMMIT,"original_source_lock_sha256":ORIGINAL_SOURCE_LOCK_SHA256,
  "training_owner_count":owners,"source_epoch_counts":total,
  "block_epoch_receipt_manifest_sha256":[file_sha256(x) for x in paths],
  "source_epoch_manifest_paths":[x.relative_to(Path(root)).as_posix() for x in paths],
  "limitations":["old and repaired executables are distinct source epochs","treatment equivalence requires native witness and feature-equivalence evidence"]}
 path=Path(root)/"source-epoch-training-complete.json"
 write_json_once(path,manifest)


def require_no_test_before_selection(data_root,root,table):
 if (Path(root)/"checkpoint-selection.json").exists(): return
 rows=rows_for(table,phase="heldout",formal=True,cohort="test")
 started=[x["unit_id"] for x in rows if _has_attempt(data_root,x)]
 if started: raise RuntimeError(f"test began before selection: {started[:8]}")

def formal():
 check_lock(); package=current_source_commit(); table=read_json(SEED_TABLE)
 old_lock=file_sha256(ORIGINAL_SOURCE_LOCK)
 if old_lock!=ORIGINAL_SOURCE_LOCK_SHA256: raise RuntimeError("original source lock changed")
 legacy_seed_sha=read_json(ORIGINAL_SOURCE_LOCK).get("seed_table_sha256")
 if not isinstance(legacy_seed_sha,str): raise RuntimeError("original lock lacks its exact seed-table identity")
 pilot_sha,pilot_path,policy_path=find_pilot_package(ORIGINAL_EXECUTION_COMMIT,legacy_seed_sha,old_lock)
 report=read_json(pilot_path); policy=read_json(policy_path)
 if report.get("status")!="PILOT_COMPLETE_NONFORMAL": raise RuntimeError("matching original pilot required")
 allowance=int(policy["one_global_active_wall_allowance_seconds"])
 if allowance!=67272: raise RuntimeError("registered global active-wall allowance changed")
 pilot_clock=closed_active_wall_clock(f"pilot-{pilot_sha[:12]}")
 pilot_seconds=pilot_clock["elapsed_seconds"]
 recovery_clock=closed_active_wall_clock(f"pilot-report-recovery-{pilot_sha[:12]}",required=False)
 recovery=None if recovery_clock is None else recovery_clock["elapsed_seconds"]
 recovery_status=pilot_report_recovery_status(report,recovery_clock)
 ensure_formal_clock_initial_state(pilot_seconds,pilot_clock_sha256=pilot_clock["sha256"],recovery_seconds=recovery,
  recovery_clock_sha256=None if recovery_clock is None else recovery_clock["sha256"],recovery_status=recovery_status)
 root=ARTIFACTS/"formal"/package; data_root=LEGACY_FORMAL_ROOT
 root.mkdir(parents=True,exist_ok=True); clock=D.ActiveClock("formal",allowance); updates=[]
 try:
  check_lock(); write_source_epoch_plan(root,package,allowance); require_no_test_before_selection(data_root,root,table)
  for block in range(12,36):
   rows=rows_for(table,phase="training",formal=True,cohort="training",block=block)
   if block<=25:
    training_block_source_epoch(root,data_root,rows,block,package)
    rec=verify_update(block,rows,LEGACY_FORMAL_ROOT)
    if rec is None: raise RuntimeError(f"original completed update {block} is missing")
    receipt_path=LEGACY_FORMAL_ROOT/"models"/f"issue83-update-{block}.json"
    execution_source=ORIGINAL_EXECUTION_COMMIT; lock_sha=ORIGINAL_SOURCE_LOCK_SHA256
   else:
    missing=[row for row in rows if source_bound_training_receipt(data_root,row,package) is None]
    if missing:
     games=D.run_units(missing,root=data_root,pilot=False,clock=clock,package_commit=package)
     if games.get("status")!="SUCCEEDED":
      result={"schema_id":"issue83-progress-v1","status":games.get("status"),"phase":"training","block_index":block,
       "completed":games.get("completed"),"remaining":games.get("remaining"),"updates":updates,
       "active_wall_seconds":clock.elapsed(),"allowance":allowance}; atomic_json(root/"formal-progress.json",result); return result
    training_input_epoch=training_block_source_epoch(root,data_root,rows,block,package)
    rec=verify_update(block,rows,root)
    if rec is None:
     a,r,m,l,e=D.load_modules(root,pilot=False)
     examples=training_examples_across_epochs(rows,data_root,e,package)
     rec=train_update(block,examples,root=root,pilot=False,clock=clock,source_epoch_summary=training_input_epoch); del examples
     rec=verify_update(block,rows,root)
    if rec is None: raise RuntimeError(f"repair-epoch update {block} was not verifiable")
    receipt_path=root/"models"/f"issue83-update-{block}.json"
    execution_source=package; lock_sha=repair_source_lock_sha256()
   updates.append(training_update_summary(rec,receipt_path,execution_source,lock_sha))
   save_training_progress(root,package,clock,updates,current_block=block)
   if clock.expired():
    result={"schema_id":"issue83-progress-v1","status":"GLOBAL_ACTIVE_WALL_ALLOWANCE_EXCEEDED","phase":"training",
     "block_index":block,"updates":updates,"active_wall_seconds":clock.elapsed(),"allowance":allowance}
    atomic_json(root/"formal-progress.json",result); return result
  complete_training_epoch_manifest(root,package)
  validation_rows=rows_for(table,phase="heldout",formal=True,cohort="validation")
  vr=D.run_units(validation_rows,root=data_root,pilot=False,clock=clock,package_commit=package)
  if vr.get("status")!="SUCCEEDED":
   result={"schema_id":"issue83-progress-v1","status":vr.get("status"),"phase":"validation","completed":vr.get("completed"),
    "remaining":vr.get("remaining"),"active_wall_seconds":clock.elapsed(),"allowance":allowance}
   atomic_json(root/"formal-progress.json",result); return result
  selection=freeze_selection(root,table,package,data_root=data_root)
  arms=["PARENT","SELECTED"]+(["FINAL"] if selection["selected_checkpoint"]!="pi36" else [])
  test_rows=rows_for(table,phase="heldout",formal=True,cohort="test",arms=arms)
  all_test=rows_for(table,phase="heldout",formal=True,cohort="test")
  if not (root/"checkpoint-selection.json").is_file(): raise RuntimeError("test blocked until frozen selection exists")
  if not (root/"test-opened.json").exists() and any(_has_attempt(data_root,x) for x in all_test):
   raise RuntimeError("test attempts predate validation selection")
  write_once(root/"test-opened.json",canonical({"selection_receipt_sha256":file_sha256(root/"checkpoint-selection.json"),
   "opened_before_first_test":True,"source_epoch_training_complete_sha256":file_sha256(root/"source-epoch-training-complete.json")})+b"\n")
  ev=D.run_units(test_rows,root=data_root,pilot=False,clock=clock,package_commit=package)
  if ev.get("status")!="SUCCEEDED":
   result={"schema_id":"issue83-progress-v1","status":ev.get("status"),"phase":"test","completed":ev.get("completed"),
    "remaining":ev.get("remaining"),"active_wall_seconds":clock.elapsed(),"allowance":allowance}
   atomic_json(root/"formal-progress.json",result); return result
  final=analyze_test(root,table,package,selection,data_root=data_root)
  final.update({"pilot_report_sha256":file_sha256(pilot_path),"time_policy_sha256":file_sha256(policy_path),
   "validation_selection_before_test":True,"training_updates":updates,
   "source_epoch_training_complete_sha256":file_sha256(root/"source-epoch-training-complete.json"),
   "source_epoch_totals":read_json(root/"source-epoch-training-complete.json")["source_epoch_counts"],
   "pilot_active_wall_seconds_carried_forward":pilot_seconds,"pilot_active_wall_clock_sha256":pilot_clock["sha256"],
   "pilot_report_recovery_status":recovery_status,"active_wall_seconds_before_closeout":clock.elapsed(),
   "active_wall_allowance_seconds":allowance,
   "formal_clock_carry_forward_sha256":file_sha256(ARTIFACTS/"formal-clock-carry-forward.json")})
  final_path=root/"final-analysis.json"; write_once(final_path,canonical(final)+b"\n")
  atomic_json(root/"formal-progress.json",{"schema_id":"issue83-progress-v1","status":"COMPLETE",
   "training_owners":3072,"training_blocks":24,"validation_games":448,"test_games":len(test_rows),
   "selected_checkpoint":selection["selected_checkpoint"],"active_wall_seconds":clock.elapsed(),"allowance":allowance,
   "source_epoch_training_complete_sha256":file_sha256(root/"source-epoch-training-complete.json"),
   "final_analysis_sha256":file_sha256(final_path)}); return final
 except GlobalWallExpired as exc:
  result={"schema_id":"issue83-progress-v1","status":"GLOBAL_ACTIVE_WALL_ALLOWANCE_EXCEEDED","failure":str(exc),
   "updates":updates,"active_wall_seconds":clock.elapsed(),"allowance":allowance}; atomic_json(root/"formal-progress.json",result); return result
 finally:
  clock.close(); closed=closed_active_wall_clock("formal")
  progress_path=root/"formal-progress.json"; progress=read_json(progress_path) if progress_path.exists() else {}
  atomic_json(root/"active-wall-closeout.json",{"schema_id":"issue83-clock-closeout-v1",
   "active_wall_seconds":closed["elapsed_seconds"],"allowance":allowance,"clock_sha256":closed["sha256"],
   "pilot_active_wall_seconds":pilot_seconds,"pilot_clock_sha256":pilot_clock["sha256"],
   "exact_complete_task_active_wall_seconds":None,"post_clock_closeout_active_wall_status":"UNMEASURED",
   "post_clock_closeout_active_wall_seconds":None})
  if progress:
   progress.update({"active_wall_seconds":closed["elapsed_seconds"],"clock_sha256":closed["sha256"],
    "exact_complete_task_active_wall_seconds":None,"post_clock_closeout_active_wall_status":"UNMEASURED"})
   atomic_json(progress_path,progress)
  if "final" in locals(): final.update({"active_wall_seconds_at_clock_close":closed["elapsed_seconds"],
   "exact_complete_task_active_wall_seconds":None,"post_clock_closeout_active_wall_status":"UNMEASURED"})
  elif "result" in locals(): result.update({"active_wall_seconds_at_clock_close":closed["elapsed_seconds"],
   "exact_complete_task_active_wall_seconds":None,"post_clock_closeout_active_wall_status":"UNMEASURED"})

def load_modules(root,*,pilot):
 global CURRENT_PILOT
 CURRENT_PILOT=bool(pilot); ISSUE81.ACTIVE_BLOCK=CURRENT_BLOCK
 alg,rt,model,learn,ex=ISSUE81.patch_modules(root,pilot=pilot)
 if not getattr(rt.initialize_native_runtime,"_issue83_phase3_repair_wrapper",False):
  initialize_native_runtime=rt.initialize_native_runtime
  def initialize_with_phase3_repair(*args,**kwargs):
   cfg,runner,module,encoder=initialize_native_runtime(*args,**kwargs)
   if not getattr(runner,"_issue83_phase3_repair_installed",False):
    validate=runner.validate_live_native_candidate_bindings
    events=[]
    def validate_with_phase3_repair(projection,actions):
     event=repair_dead_adventurer_phase3_projection(projection,actions)
     if event is not None:
      validate(projection,actions)
      events.append(event)
      return None
     return validate(projection,actions)
    runner.validate_live_native_candidate_bindings=validate_with_phase3_repair
    runner._issue83_phase3_repair_events=events
    select=runner.StudyController.select_action
    def select_with_repair_audit(self,adapter,snapshot,actions,context,step_index):
     before=len(events)
     decision=select(self,adapter,snapshot,actions,context,step_index)
     recovered=events[before:]
     if recovered:
      if not self.trace or self.trace[-1].get("kind")!="categorical":
       raise RuntimeError("phase-3 repair audit has no corresponding categorical decision")
      self.trace[-1]["issue83_projection_repairs"]=list(recovered)
     return decision
    runner.StudyController.select_action=select_with_repair_audit
    runner._issue83_phase3_repair_installed=True
   return cfg,runner,module,encoder
  initialize_with_phase3_repair._issue83_phase3_repair_wrapper=True
  rt.initialize_native_runtime=initialize_with_phase3_repair
 ex.STUDY_ID="issue-83-pgonly-scale"; old_path=ex._model_path
 def model_path(label):
  if label=="pi12": return PARENT_MODEL
  if label.startswith("pi") and label[2:].isdigit() and int(label[2:])>=13:
   index=int(label[2:])
   if pilot or index<=26: return old_path(label) if pilot else LEGACY_FORMAL_ROOT/"models"/f"{label}.pt"
   return root/"models"/f"{label}.pt"
  return old_path(label)
 ex._model_path=model_path
 learn.FORMAL_BLOCK_OWNERS=32 if pilot else 128
 learn.FORMAL_MINIBATCH_OWNERS=32; learn.FORMAL_STEPS_PER_BLOCK=64
 old=getattr(learn,"_issue83_original_new_optimizer",learn._new_optimizer)
 learn._issue83_original_new_optimizer=old
 def new_optimizer(policy):
  opt=old(policy)
  if CURRENT_BLOCK==12:
   opt.load_state_dict(__import__("torch").load(PARENT_OPTIMIZER,map_location="cpu",weights_only=True)["optimizer_state_dict"])
  return opt
 learn._new_optimizer=new_optimizer
 return alg,rt,model,learn,ex
