"""Rehash original receipts and seal derived study outputs only."""
import os
import time
import shutil
from pathlib import Path
from audit import OUT, S46, S47, SEALS, load, write, digest_bytes


def sha(path):
    return digest_bytes(Path(path).read_bytes())


def main():
    start = time.process_time()
    producer = os.environ['ISSUE51_PRODUCER']
    study = Path(__file__).parent
    ledger = load(OUT/'execution.json')
    assert ledger['stage'] == 'REPORT_COMPLETE'
    inputs = load(OUT/'input_receipts.json')
    for name, base in [('46', S46), ('47', S47)]:
        assert sha(base/'artifact-manifest.json') == SEALS[name]
    for receipt in inputs['inputs']:
        base = S46 if receipt['source'] == '46' else S47
        assert sha(base/receipt['path']) == receipt['sha256']
    traces = load(OUT/'trace_summary.json')
    assert traces['complete'] and not traces['violations']
    assert not traces['missing_files'] and not traces['orphan_files']
    assert len(traces['receipts']) == 2886
    assert all(traces['pinned_expert_source']['checks'].values())
    for receipt in traces['receipts']:
        assert receipt['valid']
        assert sha(S46/receipt['label_path']) == receipt['label_sha256']
        assert sha(S46/receipt['trace_path']) == receipt['trace_sha256']
    rows = load(OUT/'owner_metrics.json')
    assert len(rows) == len({r['owner'] for r in rows}) == len({r['root_id'] for r in rows}) == 512
    assert load(OUT/'verification.json')['passed']
    ledger['charges']['seal_cpu'] = time.process_time()-start
    ledger['charges']['root_recovery_administration_publication_cleanup_upper'] = 60
    ledger['builder_upper_cpu_seconds'] = sum(ledger['charges'].values())
    assert ledger['builder_upper_cpu_seconds'] + ledger['reviewer_reserved'] <= ledger['cpu_hard_upper']
    ledger.update(stage='SEALED_PENDING_INDEPENDENT_REVIEW',
                  seal_producer=producer, original_rank_producer='f1767d5',
                  original_trace_producer='d94256e', original_report_producer='2051f6d',
                  root_recovery='Execution agent stopped at model capacity after report; root repeated the cheap ranking audit and report, retained earlier CPU charges, and completed sealing/publication. No trace rerun, simulation or learning.',
                  new_games=0, new_Q=0, training=0, original_retention_unchanged=True)
    write(OUT/'execution.json', ledger)
    files = [{'path':p.name,'sha256':sha(p),'bytes':p.stat().st_size}
             for p in sorted(OUT.iterdir()) if p.suffix in ('.json','.md') and p.name != 'artifact-manifest.json']
    sources = [{'path':p.name,'sha256':sha(p),'bytes':p.stat().st_size}
               for p in sorted(study.iterdir()) if p.suffix in ('.py','.md')]
    seal = {'purpose':'STUDY_ONLY', 'producer_commit':producer,
            'source_files':sources,'files':files,'original_seals':SEALS,
            'original_retention':{'46':'2026-11-12','47':'2026-11-13'},
            'consumer':'Issue51 independent Reviewer then Planner','review_delete_date':'2026-11-14',
            'builder_upper_cpu_seconds':ledger['builder_upper_cpu_seconds'],
            'reviewer_reserved':ledger['reviewer_reserved'],'cpu_hard_upper':3000,
            'raw_source_cache_exported':False,'new_games':0,'new_Q':0,'training':0}
    write(OUT/'artifact-manifest.json', seal)
    evidence = study/'evidence'; evidence.mkdir(exist_ok=False)
    for name in [x['path'] for x in files] + ['artifact-manifest.json']:
        shutil.copyfile(OUT/name, evidence/name)
    shutil.copyfile(OUT/'REPORT.md', study/'REPORT.md')
    print({'passed':True,'seal_sha256':sha(OUT/'artifact-manifest.json'),
           'builder_upper_cpu_seconds':ledger['builder_upper_cpu_seconds']})


if __name__ == '__main__':
    main()
