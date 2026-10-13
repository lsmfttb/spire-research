"""Seal this derivative study; original Issue46 evidence is read-only."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
from common import write
from model import load

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''): h.update(block)
    return h.hexdigest()

def main():
    start=time.process_time(); store=Path(sys.argv[1]); out=Path(sys.argv[2])
    ledger=json.loads((store/'execution.json').read_text()); audit=json.loads((store/'audit.json').read_text())
    assert ledger['status']=='COMPLETE_PENDING_INDEPENDENT_REVIEW' and not ledger['active'] and not ledger['fresh_failed']
    assert set(map(tuple,ledger['fresh_completed']))=={(s,a) for s in range(481001,481129) for a in ('B','A','Expert')}
    assert audit['passed'] and audit['counts']['fresh_runs']==384
    checkpoints=json.loads((store/'training/checkpoints.json').read_text()); records=json.loads((store/'training/optimization.json').read_text())
    for arm,cp in checkpoints.items():
        load(cp)
        if arm=='B':
            assert len(records[arm])==200
            best=min((r['validation_mean_regret'],r['epoch'],r['seed']) for r in records[arm])
            assert best==(cp['validation_mean_regret'],cp['epoch'],cp['seed'])
    prior=Path('/home/lsmft/.cache/issue47-action-preference/artifact-manifest.json')
    assert digest(prior)=='119a514692836954ab205284c35d1070a9a089cebcc9344fd27ded2c850ff1de'
    old=Path('/home/lsmft/.cache/issue46-source-capacity/artifact-manifest.json')
    assert digest(old)=='9807c5a02554f57947db1d507d0ed49b10fa07fab88042976b44e60a879e0cd1'
    entries=[]
    for path in sorted(store.rglob('*')):
        if not path.is_file() or path.name in ('artifact-manifest.json','execution.json') or path.suffix=='.tmp': continue
        entries.append({'path':path.relative_to(store).as_posix(),'bytes':path.stat().st_size,'sha256':digest(path)})
    ledger['seal_measured_cpu']=time.process_time()-start; ledger['seal_producer']=os.environ['ISSUE48_SEAL_PRODUCER']
    ledger['cumulative_cpu']+=ledger['seal_measured_cpu']+10; ledger['administrative_cpu_charge']+=10
    assert ledger['cumulative_cpu']<40000
    write(store/'execution.json',ledger); path=store/'execution.json'
    entries.append({'path':'execution.json','bytes':path.stat().st_size,'sha256':digest(path)})
    manifest={'purpose':'STUDY_ONLY','execution_and_derivation_producer':ledger['producer'],'seal_producer':ledger['seal_producer'],'store':str(store),'file_count':len(entries),'total_bytes':sum(e['bytes'] for e in entries),'files':entries,'retention_consumer':'Issue48 independent Reviewer and Planner','review_delete_date':'2026-11-14','original_issue46_manifest_unchanged':True}
    write(store/'artifact-manifest.json',manifest); out.mkdir(parents=True,exist_ok=True)
    for name in ('artifact-manifest.json','execution.json','audit.json','report.json','verification.json','diagnostics.json','source.json','superseded-execution.json'):
        shutil.copyfile(store/name,out/name)
    (out/'training').mkdir(exist_ok=True)
    for path in (store/'training').glob('*.json'): shutil.copyfile(path,out/'training'/path.name)
    print(json.dumps({'manifest_sha256':digest(store/'artifact-manifest.json'),'files':len(entries),'bytes':manifest['total_bytes'],'cumulative_cpu':ledger['cumulative_cpu'],'validation_selection_recomputed':True}))

if __name__=='__main__': main()
