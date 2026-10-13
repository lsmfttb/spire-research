"""Seal exact optimizer-only outputs and source lineage for independent review."""
import json,sys,time,shutil
from pathlib import Path
from freeze import sha,write,value_sha,OLD46,OLD47
def main():
    start=time.process_time();store=Path(sys.argv[1]);study=Path(__file__).parent
    ledger=json.loads((store/'execution.json').read_text());assert ledger['stage']=='REPORT_COMPLETE'
    source=json.loads((store/'source.json').read_text());assert sha(store/'split.json')==source['split_sha256']==ledger['split_sha256']
    for name,base in [('46',OLD46),('47',OLD47)]:assert sha(base/'artifact-manifest.json')==source['original_seals'][name]
    assert sha(OLD47/'source.json')==source['original_source_sha256']
    for row in source['metadata_receipts']+json.loads((store/'input-receipts.json').read_text()):assert sha(Path(row['path']))==row['sha256']
    for name,r in source['pinned_source_blobs'].items():assert sha(study/name)==r['sha256']
    records=json.loads((store/'optimization.json').read_text());assert len(records)==400
    for arm in ('S','D'):
        for seed in (446101,446102):
            rs=[r for r in records if r['arm']==arm and r['seed']==seed];assert [r['epoch'] for r in rs]==list(range(1,101))
            cp=json.loads((store/f'{arm}-{seed}-epoch100.json').read_text());cp_hash=cp.pop('sha256');assert value_sha(cp)==cp_hash==ledger['checkpoints'][f'{arm}-{seed}'];assert value_sha(cp['state'])==rs[-1]['state_sha256']
    assert json.loads((store/'verification.json').read_text())['passed']
    ledger['stage']='SEALED_PENDING_INDEPENDENT_REVIEW';ledger['seal_and_publication_upper_charge']=10
    ledger['builder_upper_cpu_seconds']=sum(ledger['measured_stage_cpu_seconds'].values())+250+50+10
    ledger['ordinary_setup_or_failed_read_receipts']=[{'event':'mistyped producer env corrected before Q','covered_by_administrative_upper_charge':True},{'event':'post-fit read-only status command syntax error; no execution rerun','covered_by_administrative_upper_charge':True},{'event':'derived report regeneration initially rejected completed stage; made reporting idempotent without rerunning fitting; prehandoff provisional seal preserved','covered_by_administrative_upper_charge':True}]
    ledger['retention_consumer']='Issue50 independent Reviewer then Planner';ledger['review_delete_date']='2026-11-14';ledger['historical_store_retention_unchanged']=True
    assert ledger['builder_upper_cpu_seconds']+1000<=3000
    write(store/'execution.json',ledger)
    artifacts=[{'path':p.name,'sha256':sha(p),'bytes':p.stat().st_size} for p in sorted(store.glob('*.json')) if p.name!='artifact-manifest.json']
    sources=[{'path':p.name,'sha256':sha(p),'bytes':p.stat().st_size} for p in sorted(study.iterdir()) if p.suffix in ('.py','.md','.json')]
    manifest={'schema':'issue50-optimizer-only-v1','purpose':'STUDY_ONLY_NO_MERGE','producer_commit':ledger['execution_producer_commit'],'split_producer_commit':ledger['producer_commit'],'source_files':sources,'files':artifacts,'original_seals_unchanged':True,'source_owner_generation':0,'fresh_native_games':0,'new_Q_continuations':0,'four_fits_exact100epochs':True,'builder_upper_cpu_seconds':ledger['builder_upper_cpu_seconds'],'reviewer_reserve':1000,'review_delete_date':'2026-11-14','consumer':'Issue50 independent Reviewer then Planner'}
    write(store/'artifact-manifest.json',manifest);evidence=study/'evidence';evidence.mkdir(exist_ok=True)
    for p in store.glob('*.json'):shutil.copyfile(p,evidence/p.name)
    print(json.dumps({'passed':True,'manifest_sha256':sha(store/'artifact-manifest.json'),'builder_upper_cpu_seconds':ledger['builder_upper_cpu_seconds'],'seal_measured_cpu_seconds':time.process_time()-start}))
if __name__=='__main__':main()
