import json
from pathlib import Path
import sys
import shutil
from common import CONFIG,sha,write,owner_plan
import hashlib

def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
    store=Path(sys.argv[1]); old=Path('/home/lsmft/.cache/issue47-action-preference'); original=Path('/home/lsmft/.cache/issue46-source-capacity')
    seal=old/'artifact-manifest.json'; assert digest(seal)=='119a514692836954ab205284c35d1070a9a089cebcc9344fd27ded2c850ff1de'
    seal46=original/'artifact-manifest.json'; assert digest(seal46)=='9807c5a02554f57947db1d507d0ed49b10fa07fab88042976b44e60a879e0cd1'
    entries={e['path']:e for e in json.loads(seal.read_text())['files']}
    sourcepath=old/'source.json'; assert digest(sourcepath)==entries['source.json']['sha256']; oldsource=json.loads(sourcepath.read_text())
    inputs=store/'inputs'; inputs.mkdir(exist_ok=False); records=[]
    for plan,record in zip(owner_plan(),oldsource['derived_files']):
        assert all(plan[k]==record[k] for k in ('owner','split','assisted'))
        src=old/'inputs'/record['input_path']; assert digest(src)==record['derived_sha256']==entries['inputs/'+record['input_path']]['sha256']
        assert digest(original/record['source_path'])==record['source_table_sha256']
        shutil.copyfile(src,inputs/record['input_path']); records.append(record)
    a=old/'training/A.json'; assert digest(a)==entries['training/A.json']['sha256']
    cp=json.loads(a.read_text()); h=cp.pop('sha256'); assert sha(cp)==h==CONFIG['frozen_A_sha256']
    shutil.copyfile(a,store/'frozen-A.json')
    native=Path('/home/lsmft/.cache/issue45-review-build-18bcd565/slaythespire.cpython-314-x86_64-linux-gnu.so'); assert digest(native)==CONFIG['native_extension_sha256']
    write(store/'source.json',{'purpose':'STUDY_ONLY','issue46_commit':'f7037e2dadc57713bc60f2ad1b5c00c61048c7d1','issue47_commit':'41484c97d7a7b294bcfe13a9b9864c1cabf10603','old_manifest_sha256':digest(seal46),'issue47_manifest_sha256':digest(seal),'issue47_source_sha256':digest(sourcepath),'frozen_A_file_sha256':digest(a),'frozen_A_checkpoint_sha256':h,'native_commit':CONFIG['native_commit'],'native_extension_sha256':digest(native),'stsrl_commit':CONFIG['stsrl_commit'],'config_sha256':sha(CONFIG),'derived_files':records,'retention_consumer':'Issue48 independent Reviewer and Planner','review_delete_date':'2026-11-14','old_stores_read_only':True,'no_old_fresh_scores_read':True})
if __name__=='__main__': main()
