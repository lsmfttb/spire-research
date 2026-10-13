"""Freeze metadata-only retrospective split BEFORE opening any Q table."""
import hashlib,json,os,sys,time,subprocess
from pathlib import Path
from collections import defaultdict
OLD46=Path('/home/lsmft/.cache/issue46-source-capacity')
OLD47=Path('/home/lsmft/.cache/issue47-action-preference')
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def value_sha(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def write(p,v):
    p=Path(p);temp=p.with_suffix('.tmp');temp.write_text(json.dumps(v,sort_keys=True,indent=2,allow_nan=False)+'\n');temp.replace(p)
def main():
    start=time.process_time();store=Path(sys.argv[1]);store.mkdir(parents=True,exist_ok=False)
    protocol=json.loads(Path(__file__).with_name('protocol.json').read_text());write(store/'protocol.json',protocol)
    ledger={'stage':'METADATA_ONLY','budget_upper_cpu_seconds':3000,'measured_stage_cpu_seconds':{},'administrative_upper_charge':250,'root_coordination_upper_charge':50,'reviewer_reserve':1000,'producer_commit':os.environ['ISSUE50_PRODUCER'],'Q_values_opened':False,'fresh_games':0};write(store/'execution.json',ledger)
    pinned={}
    for original,local in [('student.py','student.py'),('train.py','train47.py'),('common.py','common.py'),('adapter.py','adapter47.py'),('config.json','config47.json'),('config.json','config.json')]:
        blob=subprocess.check_output(['git.exe','-C','D:/DeadlycatCoding/spire-research','show',protocol['pinned47']+':studies/issue-47-action-preference/'+original]);assert Path(__file__).with_name(local).read_bytes()==blob
        pinned[local]={'original':original,'commit':protocol['pinned47'],'sha256':hashlib.sha256(blob).hexdigest()}
    manifests={}
    for name,base,expected in [('46',OLD46,'9807c5a02554f57947db1d507d0ed49b10fa07fab88042976b44e60a879e0cd1'),('47',OLD47,'119a514692836954ab205284c35d1070a9a089cebcc9344fd27ded2c850ff1de')]:
        p=base/'artifact-manifest.json';assert sha(p)==expected;manifests[name]=json.loads(p.read_text())
    hashes46={r['path']:r['sha256'] for r in manifests['46']['files']};hashes47={r['path']:r['sha256'] for r in manifests['47']['files']}
    assert sha(OLD47/'source.json')==hashes47['source.json'];source=json.loads((OLD47/'source.json').read_text());metadata=[];receipts=[]
    for rec in source['derived_files']:
        if rec['split']!='train':continue
        path=OLD46/str(rec['owner'])/'root.json';assert sha(path)==hashes46[str(rec['owner'])+'/root.json'];r=json.loads(path.read_text())
        assert r['owner']==rec['owner'] and r['assisted']==rec['assisted'] and r['old45']==rec['old45'] and r['split']=='train'
        metadata.append({k:r[k] for k in ('owner','root_id','assisted','old45','family')})
        receipts.append({'path':str(path),'sha256':sha(path),'role':'preQ metadata only'})
    assert len(metadata)==384 and len({r['root_id'] for r in metadata})==384
    cells=defaultdict(list)
    for r in metadata:cells[(r['assisted'],r['family'])].append(r)
    chosen=[];occupancy=[]
    for cell,rs in sorted(cells.items()):
        assert len(rs)==96
        if not cell[0]:
            origins={k:[r for r in rs if r['old45']==k] for k in (False,True)}
            if all(len(v)>=8 for v in origins.values()):allocation={False:8,True:8}
            else:
                exact={k:16*len(v)/96 for k,v in origins.items()};allocation={k:int(v) for k,v in exact.items()}
                for k in sorted(exact,key=lambda k:(-(exact[k]-allocation[k]),str(k)))[:16-sum(allocation.values())]:allocation[k]+=1
            for k,vs in origins.items():chosen.extend(sorted(vs,key=lambda r:value_sha([50017,r['owner']]))[:allocation[k]])
            occupancy.append({'assisted':cell[0],'family':cell[1],'old45':{str(k):len(v) for k,v in origins.items()},'allocation':{str(k):v for k,v in allocation.items()}})
        else:chosen.extend(sorted(rs,key=lambda r:value_sha([50017,r['owner']]))[:16])
    selected={r['owner'] for r in chosen};assert len(selected)==64
    for r in metadata:r['diagnostic_split']='diagnostic' if r['owner'] in selected else 'train'
    frozen={'rule':protocol['partition_hash_encoding'],'rows':sorted(metadata,key=lambda r:r['owner']),'natural_origin_occupancy':occupancy,'diagnostic_owners':sorted(selected),'training_owners':sorted(r['owner'] for r in metadata if r['owner'] not in selected),'Q_values_opened_before_freeze':False,'historically_exposed_research_data':True}
    write(store/'split.json',frozen)
    lineage={'pinned_source_blobs':pinned,'training_source_records':[r for r in source['derived_files'] if r['split']=='train'],'original_source_sha256':sha(OLD47/'source.json'),'metadata_receipts':receipts,'original_seals':{'46':sha(OLD46/'artifact-manifest.json'),'47':sha(OLD47/'artifact-manifest.json')},'split_sha256':sha(store/'split.json'),'producer_commit':ledger['producer_commit']}
    write(store/'source.json',lineage)
    ledger['stage']='SPLIT_FROZEN_BEFORE_Q';ledger['split_sha256']=sha(store/'split.json');ledger['measured_stage_cpu_seconds']['freeze']=time.process_time()-start;write(store/'execution.json',ledger)
    print(json.dumps({'split_sha256':ledger['split_sha256'],'CPU':ledger['measured_stage_cpu_seconds']['freeze'],'natural_occupancy':occupancy}))
if __name__=='__main__':main()
