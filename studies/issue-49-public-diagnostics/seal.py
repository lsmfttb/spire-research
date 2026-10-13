"""Seal additive Phase A receipts, preserving historical inputs read-only."""
import hashlib,json,sys,time,shutil
from pathlib import Path
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def write(p,x):p.write_text(json.dumps(x,sort_keys=True,indent=2)+'\n')
def main():
    start=time.process_time();store=Path(sys.argv[1]);study=Path(__file__).parent
    inputs=json.loads((store/'inputs.json').read_text())
    for r in inputs:assert sha(Path(r['path']))==r['sha256']
    native=json.loads((store/'visibility-work-receipt.json').read_text())
    grad=json.loads((store/'gradient-verification.json').read_text());proto=json.loads((store/'prototype-verification.json').read_text());visibility=json.loads((store/'visibility-checks.json').read_text())
    assert grad['passed'] and proto['passed'] and visibility['passed']
    execution=json.loads((store/'execution.json').read_text())
    diagnostics_cpu=execution.get('measured_cpu_seconds',execution.get('measured_final_diagnostics_cpu'))
    ledger={'budget_upper_cpu_seconds':3000,'purpose':'Phase A only; independent Reviewer reserve remains',
            'measured_final_diagnostics_cpu':diagnostics_cpu,
            'measured_prior_diagnostics_attempt_cpu':50.057715294,
            'native_probe_cpu_receipt':native,
            'prototype_cpu':proto['cpu_seconds'],'gradient_check_cpu':grad['cpu_seconds'],
            'visibility_check_cpu':visibility['cpu_seconds'],
            'administrative_and_imports_upper_charge':150,
            'additional_parent_aborted_optional_diagnostic_conservative_charge':108.273038541,
            'reviewer_upper_reserve':2200,'fresh_seeded_games':0,'source_owner_generation':0,'Q_continuations':0,'fitted_models':0,
            'stage':'BUILDER_SEALED_PENDING_INDEPENDENT_REVIEW','retention_consumer':'Issue49 independent Reviewer, then Planner; delete after 2026-11-14 unless explicitly extended','historical_store_retention_unchanged':True}
    # visibility receipt includes its own complete attempt total, not wall-time.
    native_cpu=native.get('total_measured_cpu_seconds',native.get('measured_cpu_seconds',native.get('cpu_seconds',0)))
    assert native_cpu>0, 'Native receipt must expose all-attempt measured total'
    ledger['native_all_attempt_cpu_seconds']=native_cpu
    ledger['builder_upper_cpu_seconds']=sum([diagnostics_cpu,50.057715294,native_cpu,proto['cpu_seconds'],grad['cpu_seconds'],visibility['cpu_seconds'],150,108.273038541,10])
    ledger['seal_and_publication_upper_charge']=10
    assert ledger['builder_upper_cpu_seconds']+2200<=3000
    write(store/'execution.json',ledger)
    records=[{'path':p.name,'sha256':sha(p),'bytes':p.stat().st_size} for p in sorted(store.glob('*.json')) if p.name!='artifact-manifest.json']
    source=[{'path':p.name,'sha256':sha(p),'bytes':p.stat().st_size} for p in sorted(study.iterdir()) if p.suffix in ('.py','.md')]
    manifest={'schema':'issue49-phaseA-manifest-v1','purpose':'STUDY_ONLY_NO_MERGE','files':records,'source_files':source,'input_hashes_file':'inputs.json','producer_commit':'filled by exact remote handoff SHA; manifest does not self-hash','cpu_builder_upper':ledger['builder_upper_cpu_seconds'],'retention':ledger['retention_consumer'],'no_48_results_read':True}
    write(store/'artifact-manifest.json',manifest)
    evidence=study/'evidence';evidence.mkdir(exist_ok=True)
    for p in store.glob('*.json'):shutil.copyfile(p,evidence/p.name)
    print(json.dumps({'passed':True,'builder_upper_cpu_seconds':ledger['builder_upper_cpu_seconds'],'reviewer_reserve':2200,'manifest_sha256':sha(store/'artifact-manifest.json'),'seal_measured_cpu':time.process_time()-start}))
if __name__=='__main__':main()
