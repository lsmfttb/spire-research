"""Seal completed study files and copy review evidence to the remote study branch."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
from common import write

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''): h.update(block)
    return h.hexdigest()

def main():
    start=time.process_time(); store=Path(sys.argv[1]); out=Path(sys.argv[2])
    ledger=json.loads((store/'execution.json').read_text())
    audit=json.loads((store/'audit.json').read_text())
    assert ledger['status']=='COMPLETE_PENDING_INDEPENDENT_REVIEW' and not ledger['active']
    assert audit['passed'] and audit['counts']['fresh_runs']==640
    assert len(ledger['completed'])==512 and len(ledger['fresh_completed'])==640
    assert not ledger['failed'] and not ledger['fresh_failed']
    out.mkdir(parents=True,exist_ok=True)
    entries=[]
    for path in sorted(store.rglob('*')):
        if not path.is_file() or path.name in ('artifact-manifest.json','execution.json') or path.suffix=='.tmp': continue
        entries.append({'path':path.relative_to(store).as_posix(),'bytes':path.stat().st_size,'sha256':digest(path)})
    # One measured hashing pass; preserve the preceding aggregate budget carry.
    ledger['seal_measured_cpu']=time.process_time()-start
    ledger['seal_producer']=os.environ['ISSUE46_SEAL_PRODUCER']
    ledger['cumulative_cpu']+=ledger['seal_measured_cpu']+10
    ledger['administrative_cpu_charge']+=10 # copy/write/CLI overhead upper
    assert ledger['cumulative_cpu']<ledger['cap']
    write(store/'execution.json',ledger)
    path=store/'execution.json'
    entries.append({'path':'execution.json','bytes':path.stat().st_size,'sha256':digest(path)})
    manifest={'purpose':'STUDY_ONLY','producer':os.environ['ISSUE46_SEAL_PRODUCER'],'store':str(store),'retention_consumer':'Issue46 independent Reviewer and Planner','review_delete_date':'2026-11-12','file_count':len(entries),'total_bytes':sum(e['bytes'] for e in entries),'files':entries}
    write(store/'artifact-manifest.json',manifest)
    for name in ('artifact-manifest.json','execution.json','audit.json','report.json','model-verification.json','first-fixed-key-costs.json','source.json','owner-plan.json','resume-source.json','finish-failure-first-attempt.json'):
        shutil.copyfile(store/name,out/name)
    for path in (store/'training').glob('*.json'):
        (out/'training').mkdir(exist_ok=True); shutil.copyfile(path,out/'training'/path.name)
    receipt=store/'interrupted-attempts/20261012T161734Z/interruption.json'
    shutil.copyfile(receipt,out/'interruption.json')
    print(json.dumps({'manifest_sha256':digest(store/'artifact-manifest.json'),'files':len(entries),'bytes':manifest['total_bytes'],'cumulative_cpu_upper':ledger['cumulative_cpu']}))

if __name__=='__main__': main()
