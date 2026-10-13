"""Read-only integrity verification and additive documentation repair seal."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

BASE_SHA = '87ab17c84d4951c58cdd62b38f9489e4ba40ca18'
BASE_SEAL = 'a717e647b86ec048dc4648d65b2b65b55656891d0833a203325773f9b1b86e83'
STUDY = 'studies/issue-50-optimizer-dynamics/'
STORE = Path('/home/lsmft/.cache/issue50-optimizer-dynamics')
GIT = '/mnt/d/Git/cmd/git.exe'


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def blob(repo, ref, path):
    return subprocess.check_output([GIT, '-C', repo, 'show', ref + ':' + path])


def main():
    start = time.process_time()
    repo, producer = sys.argv[1:3]
    destination = STORE / ('documentation-repair-' + producer[:8])
    destination.mkdir(exist_ok=False)
    original = (STORE / 'artifact-manifest.json').read_bytes()
    assert digest(original) == BASE_SEAL
    manifest = json.loads(original)
    receipts = []
    for record in manifest['source_files']:
        raw = blob(repo, BASE_SHA, STUDY + record['path'])
        assert digest(raw) == record['sha256']
        if record['path'] != 'README.md':
            assert blob(repo, producer, STUDY + record['path']) == raw
        receipts.append({'kind': 'original_source', **record})
    for record in manifest['files']:
        raw = (STORE / record['path']).read_bytes()
        assert digest(raw) == record['sha256']
        assert blob(repo, BASE_SHA, STUDY + 'evidence/' + record['path']) == raw
        assert blob(repo, producer, STUDY + 'evidence/' + record['path']) == raw
        receipts.append({'kind': 'original_output', **record})
    assert blob(repo, producer, STUDY + 'evidence/artifact-manifest.json') == original
    old_doc = blob(repo, BASE_SHA, STUDY + 'README.md')
    new_doc = blob(repo, producer, STUDY + 'README.md')
    run_source = blob(repo, producer, STUDY + 'run.py').decode()
    assert b'No diagnostic outcomes\nare read until all four epoch100 checkpoints exist' in old_doc
    assert b'The metadata split is\nfrozen before Q tables are opened' in new_doc
    assert b'Later arms may not yet\nhave completed' in new_doc
    assert run_source.index('initial_result=') < run_source.index('for epoch in range(1,101):')
    assert run_source.index('outputs[key]=') > run_source.index('for epoch in range(1,101):')
    changes = subprocess.check_output([GIT, '-C', repo, 'diff', '--name-only', BASE_SHA, producer]).decode().splitlines()
    assert sorted(changes) == sorted([STUDY + 'README.md', STUDY + 'verify_documentation_repair.py'])
    previous = json.loads((STORE / 'execution.json').read_text())['builder_upper_cpu_seconds']
    measured = time.process_time() - start
    upper = previous + measured + 40.0
    assert upper + 1000 <= 3000
    receipt = {
        'purpose': 'STUDY_ONLY_DOCUMENTATION_REPAIR',
        'original_handoff_sha': BASE_SHA,
        'producer_commit': producer,
        'original_seal_sha256': BASE_SEAL,
        'original_seal_and_all_source_output_hashes_unchanged': True,
        'original_receipts': receipts,
        'README_before_sha256': digest(old_doc),
        'README_after_sha256': digest(new_doc),
        'verification_source_sha256': digest(blob(repo, producer, STUDY + 'verify_documentation_repair.py')),
        'actual_timing': 'Metadata split precedes Q. Each arm computes epoch0 metrics before its fixed fit and epoch100 metrics afterward; later arms may be unfinished. No diagnostic-driven selection or tuning.',
        'refits': 0, 'new_games': 0, 'new_Q': 0,
        'prior_builder_upper_cpu_seconds': previous,
        'verification_measured_python_cpu_seconds': measured,
        'additional_administration_git_hashing_publication_cleanup_upper_cpu_seconds': 40.0,
        'builder_upper_cpu_seconds': upper,
        'reviewer_reserve_cpu_seconds': 1000,
        'all_in_budget_ceiling_cpu_seconds': 3000,
        'consumer': 'Issue50 independent Reviewer then Planner',
        'review_delete_date': '2026-11-14',
        'original_retention_unchanged': True,
    }
    encoded = (json.dumps(receipt, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    (destination / 'receipt.json').write_bytes(encoded)
    seal = {'producer_commit': producer, 'original_seal_sha256': BASE_SEAL,
            'files': [{'path': 'receipt.json', 'sha256': digest(encoded), 'bytes': len(encoded)}],
            'purpose': 'ADDITIVE_DOCUMENTATION_ONLY', 'consumer': receipt['consumer'],
            'review_delete_date': '2026-11-14'}
    sealed = (json.dumps(seal, sort_keys=True, indent=2) + '\n').encode()
    (destination / 'artifact-manifest.json').write_bytes(sealed)
    assert digest((STORE / 'artifact-manifest.json').read_bytes()) == BASE_SEAL
    print(json.dumps({'store': str(destination), 'seal_sha256': digest(sealed),
                      'builder_upper_cpu_seconds': upper, 'passed': True}))


if __name__ == '__main__':
    main()
