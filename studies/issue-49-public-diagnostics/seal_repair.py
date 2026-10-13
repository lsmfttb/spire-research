"""Additive repair seal; never changes original Phase A store files."""
import json,hashlib,time,sys,shutil
from pathlib import Path
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def write(p,value):p.write_text(json.dumps(value,sort_keys=True,indent=2)+'\n')
def main():
    start=time.process_time();original=Path(sys.argv[1]);out=Path(sys.argv[2]);study=Path(__file__).parent
    assert sha(original/'artifact-manifest.json')=='0bbd889989139c8510b37c84228545abc723fd559d8f51b083287df24791d3bd'
    old_manifest=json.loads((original/'artifact-manifest.json').read_text())
    for row in old_manifest['files']:assert sha(original/row['path'])==row['sha256']
    checks=[json.loads((out/name).read_text()) for name in ('snapshot-verification.json','prototype-verification.json','visibility-checks.json')]
    assert all(c['passed'] for c in checks)
    measured=sum(c['cpu_seconds'] for c in checks)+0.008520664999999997
    ledger={'stage':'REPAIR_VERIFIED_AND_SEALED','budget_upper_cpu_seconds':3000,'prior_builder_upper_cpu_seconds':416.330496165,'measured_completed_test_attempts_cpu_seconds':measured,'administrative_imports_failed_check_and_publication_upper_charge':100,'failed_attempts':[{'stage':'visibility_checks','reason':'repair output directory lacked native input; corrected invocation reads original store','cpu_covered_by_administrative_upper_charge':True}],'repair_builder_upper_cpu_seconds':measured+100,'cumulative_builder_upper_cpu_seconds':416.330496165+measured+100,'reviewer_reserve_upper_cpu_seconds':2200,'fresh_games':0,'native_executions':0,'training_steps':0,'new_Q_labels':0,'retention_consumer':'Issue49 Reviewer then Planner; delete with named original store after 2026-11-14 unless extended'}
    assert ledger['cumulative_builder_upper_cpu_seconds']+2200<=3000
    write(out/'repair-execution.json',ledger)
    sources=['prototype.py','test_prototype.py','visibility_checks.py','verify_snapshot_guard.py','seal_repair.py','REPAIR.md']
    manifest={'purpose':'ADDITIVE_OPERATIONAL_REPAIR_STUDY_ONLY','original_commit':'9e3b4c6f531b86f7d2e94d1098fc5ad22a79bb27','original_seal_sha256':sha(original/'artifact-manifest.json'),'original_artifacts_verified_unchanged':True,'sources':[{'path':name,'sha256':sha(study/name)} for name in sources],'outputs':[{'path':p.name,'sha256':sha(p),'bytes':p.stat().st_size} for p in sorted(out.glob('*.json')) if p.name!='repair-manifest.json'],'cumulative_builder_upper_cpu_seconds':ledger['cumulative_builder_upper_cpu_seconds']}
    write(out/'repair-manifest.json',manifest)
    evidence=study/'repair-snapshot-guard';evidence.mkdir(exist_ok=True)
    for p in out.glob('*.json'):shutil.copyfile(p,evidence/p.name)
    print(json.dumps({'passed':True,'repair_manifest_sha256':sha(out/'repair-manifest.json'),'cumulative_builder_upper_cpu_seconds':ledger['cumulative_builder_upper_cpu_seconds'],'seal_measured_cpu':time.process_time()-start}))
if __name__=='__main__':main()
