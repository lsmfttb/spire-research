"""Add a corrected Issue46 historical receipt without modifying sealed evidence."""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys
from common import write

def filehash(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    store=Path(sys.argv[1]); out=Path(sys.argv[2])
    original=store/'interrupted-attempts/20261012T161734Z/interruption.json'
    raw=original.read_bytes(); receipt=json.loads(raw); before=deepcopy(receipt['previous_ledger'])
    # At write time only settled_worker_cpu had changed. Later admin/active/
    # receipt-list updates occur AFTER the original receipt write in the code.
    debit=sum(Decimal(str(r['hard_limit_cpu_upper'])) for r in receipt['interrupted'])
    recorded=Decimal(str(before['settled_worker_cpu']))
    reconstructed=recorded-debit
    before['settled_worker_cpu']=float(reconstructed)
    parts={k:str(Decimal(str(before[k]))) for k in ('settled_worker_cpu','administrative_cpu_charge','parent_cpu')}
    parts['active_live_cpu']=str(sum(Decimal(str(v['cpu'])) for v in before['active'].values()))
    total=sum(Decimal(v) for v in parts.values()); prior=Decimal(str(before['cumulative_cpu']))
    assert abs(total-prior)<Decimal('0.000000001')
    ledger_hash=filehash(store/'execution.json'); manifest_hash=filehash(store/'artifact-manifest.json')
    correction={'purpose':'STUDY_ONLY','producer':os.environ['ISSUE46_RECEIPT_PRODUCER'],
        'original_receipt_path':str(original),'original_receipt_sha256':hashlib.sha256(raw).hexdigest(),
        'reason':'previous_ledger aliased mutable ledger; interrupted-worker debit changed the historical settled_worker_cpu before serialization',
        'recorded_previous_settled_worker_cpu':str(recorded),'interrupted_worker_debit':str(debit),
        'reconstructed_previous_settled_worker_cpu':str(reconstructed),'corrected_previous_ledger':before,
        'reconciliation':{'components':parts,'sum':str(total),'recorded_previous_cumulative_cpu':str(prior),'rounding_difference':str(total-prior)},
        'original_sealed_manifest_sha256':manifest_hash,'unchanged_execution_sha256':ledger_hash,
        'sealed_final_cpu_upper':json.loads((store/'execution.json').read_text())['cumulative_cpu'],
        'post_handoff_receipt_repair_cpu_upper':20,
        'notice':'Additive historical correction only. Original receipt, sealed manifest, ledger, source, training and evaluation bytes remain unchanged.'}
    target=original.with_name('interruption-corrected.json'); assert not target.exists()
    write(target,correction); out.mkdir(parents=True,exist_ok=True)
    (out/'interruption-corrected.json').write_bytes(target.read_bytes())
    assert original.read_bytes()==raw and filehash(store/'execution.json')==ledger_hash and filehash(store/'artifact-manifest.json')==manifest_hash
    supplement={'original_manifest_sha256':manifest_hash,'additional_files':[{'path':target.relative_to(store).as_posix(),'sha256':filehash(target),'bytes':target.stat().st_size}],
        'retention_consumer':'Issue46 independent Reviewer and Planner','review_delete_date':'2026-11-12'}
    write(store/'receipt-correction-manifest.json',supplement); write(out/'receipt-correction-manifest.json',supplement)
    print(json.dumps({'original_receipt_sha256':correction['original_receipt_sha256'],'corrected_receipt_sha256':filehash(target),'reconciliation':correction['reconciliation']}))

if __name__=='__main__': main()
