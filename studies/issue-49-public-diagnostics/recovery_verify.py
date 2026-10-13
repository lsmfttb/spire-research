"""Additive recovery verification; original and repair seals remain immutable."""
import hashlib,json,os,subprocess,sys,time
from pathlib import Path

def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def write(p,value): p.write_text(json.dumps(value,sort_keys=True,indent=2)+'\n')

def main():
    start=time.process_time(); original=Path(sys.argv[1]); out=Path(sys.argv[2]); study=Path(__file__).parent
    out.mkdir(parents=True,exist_ok=False)
    prior=json.loads((study/'repair-snapshot-guard/repair-execution.json').read_text())
    ledger={'stage':'RECOVERY_VERIFYING','original_handoff':'9e3b4c6f531b86f7d2e94d1098fc5ad22a79bb27','repair_handoff':'93f5941ab341e99cf8e40736e04516e1057ff53a','prior_builder_upper_cpu':prior['cumulative_builder_upper_cpu_seconds'],'administrative_recovery_upper_cpu':50.,'worker_cpu':0.,'completed':[],'fresh_games':0,'native_executions':0,'training_steps':0,'new_labels':0,'budget':3000,'retention_consumer':'Issue49 Reviewer/Planner','review_delete_date':'2026-11-14'}
    def publish():
        ledger['parent_cpu']=time.process_time()-start
        ledger['builder_cumulative_upper_cpu']=ledger['prior_builder_upper_cpu']+50+ledger['worker_cpu']+ledger['parent_cpu']
        assert ledger['builder_cumulative_upper_cpu']+2200<3000
        write(out/'recovery-execution.json',ledger)
    publish()
    assert sha(original/'artifact-manifest.json')=='0bbd889989139c8510b37c84228545abc723fd559d8f51b083287df24791d3bd'
    for row in json.loads((original/'artifact-manifest.json').read_text())['files']:
        assert sha(original/row['path'])==row['sha256']
    repair=json.loads((study/'repair-snapshot-guard/repair-manifest.json').read_text())
    for row in repair['sources']: assert sha(study/row['path'])==row['sha256']
    for row in repair['outputs']: assert sha(study/'repair-snapshot-guard'/row['path'])==row['sha256']
    tests=[('snapshot','verify_snapshot_guard.py',[str(original),str(out)]),('prototype','test_prototype.py',[str(out)]),('visibility','visibility_checks.py',[str(out),str(original)])]
    for name,script,args in tests:
        with (out/(name+'.log')).open('w') as log:
            process=subprocess.Popen([sys.executable,str(study/script),*args],stdout=log,stderr=log)
            _,status,usage=os.wait4(process.pid,0)
        ledger['worker_cpu']+=usage.ru_utime+usage.ru_stime
        code=os.waitstatus_to_exitcode(status); ledger['completed'].append({'test':name,'exit':code});publish()
        if code: ledger['stage']='FAILED';publish(); raise RuntimeError(name+' failed')
    for name in ('snapshot-verification.json','prototype-verification.json','visibility-checks.json'):
        assert json.loads((out/name).read_text())['passed']
    ledger.update(stage='RECOVERY_VERIFIED',original_seal_unchanged=True,repair_seal_unchanged=True,all_repaired_source_and_output_hashes_verified=True)
    publish()
    manifest={'purpose':'ADDITIVE_RECOVERY_VERIFICATION_STUDY_ONLY','source_sha256':sha(Path(__file__)),'builder_cumulative_upper_cpu':ledger['builder_cumulative_upper_cpu'],'files':[{'path':p.name,'bytes':p.stat().st_size,'sha256':sha(p)} for p in sorted(out.iterdir()) if p.is_file()]}
    write(out/'recovery-manifest.json',manifest)
    print(json.dumps({'passed':True,'builder_cumulative_upper_cpu':ledger['builder_cumulative_upper_cpu'],'recovery_manifest_sha256':sha(out/'recovery-manifest.json')}))
if __name__=='__main__': main()
