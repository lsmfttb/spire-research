"""One-time exact hashed derivation; Issue46 store is read-only."""
import hashlib
import json
from pathlib import Path
import sys
from common import CONFIG,owner_plan,sha,write

def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    store=Path(sys.argv[1]); old=Path('/home/lsmft/.cache/issue46-source-capacity')
    inputs=store/'inputs'; inputs.mkdir(parents=True,exist_ok=False)
    manifest=old/'artifact-manifest.json'
    assert digest(manifest)=='9807c5a02554f57947db1d507d0ed49b10fa07fab88042976b44e60a879e0cd1'
    entries={e['path']:e for e in json.loads(manifest.read_text())['files']}
    records=[]
    for p in owner_plan():
        key=str(p['owner'])+'/table.json'; original=old/key; entry=entries[key]
        assert original.stat().st_size==entry['bytes'] and digest(original)==entry['sha256']
        table=json.loads(original.read_text()); root=table['root']
        assert all(root[k]==p[k] for k in ('owner','split','assisted'))
        assert len(table['q'])==root['legal_count'] and table['branch_count']==root['legal_count']*2
        public=json.loads(root['serialized']); assert sha(public)==root['public_sha256']
        derived={'root':{k:root[k] for k in ('owner','split','assisted','root_id','serialized','public_sha256','legal_count','family','floor','act')},'q':table['q']}
        path=inputs/(str(p['owner'])+'.json'); write(path,derived)
        records.append({**p,'source_table_sha256':entry['sha256'],'derived_sha256':digest(path),'source_path':key,'input_path':path.name})
    native=Path('/home/lsmft/.cache/issue45-review-build-18bcd565/slaythespire.cpython-314-x86_64-linux-gnu.so')
    assert digest(native)==CONFIG['native_extension_sha256']
    write(store/'source.json',{'purpose':'STUDY_ONLY','issue46_commit':'f7037e2dadc57713bc60f2ad1b5c00c61048c7d1','old_store_read_only':str(old),'old_manifest_sha256':digest(manifest),'native_commit':CONFIG['native_commit'],'native_extension_sha256':digest(native),'stsrl_commit':CONFIG['stsrl_commit'],'config_sha256':sha(CONFIG),'derived_files':records,'retention_consumer':'Issue47 independent Reviewer and Planner','review_delete_date':'2026-11-13','no_old_fresh_scores_read':True})

if __name__=='__main__': main()
