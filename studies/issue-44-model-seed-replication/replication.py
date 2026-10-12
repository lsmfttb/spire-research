"""Disposable #44 wrapper; simulator, features and SGD belong to pinned #43."""
import hashlib
import json
from pathlib import Path
import random
import sys
from datetime import datetime, timezone

HERE = Path(__file__).resolve().parent
CONTRACT = json.loads((HERE/'contract.json').read_text())

def sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def file_sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block:=stream.read(1024*1024): h.update(block)
    return h.hexdigest()

def read(path): return json.loads(Path(path).read_text())

def dependencies(source_code):
    """Only immutable declared producer modules may supply training/execution."""
    base=Path(CONTRACT['source_directory'])
    if datetime.now(timezone.utc).date().isoformat()>CONTRACT['source_review_delete']:
        raise ValueError('SOURCE_RETENTION_EXPIRED: Planner decision required')
    manifest=base/'audit/manifest.json'
    if file_sha(manifest)!=CONTRACT['source_manifest_sha256']:
        raise ValueError('source manifest identity changed')
    entries={r['path']:r for r in read(manifest)}
    names=['identity.json','stage1/complete.json','stage1/cohort-train.json',
           'stage1/cohort-validation.json','stage1/cohort-heldout.json',
           'stage1/train-targets.json','stage1/validation-targets.json']
    pinned=[]
    for name in names:
        p=base/name; expected=entries[name]
        if p.stat().st_size!=expected['bytes'] or file_sha(p)!=expected['sha256']:
            raise ValueError('source dependency checksum changed: '+name)
        pinned.append(expected)
    original=read(base/'identity.json')
    if original['producer_commit']!=CONTRACT['source_producer'] or original['synthetic']:
        raise ValueError('source producer identity changed')
    code=Path(source_code).resolve()
    expected=original['producer_source_binding']['git_blobs']
    actual={p.name for p in code.glob('*.py')}|{'config.json','planner_budget_amendment.json'}
    if actual!=set(expected): raise ValueError('source executable set changed')
    for name,oid in expected.items():
        raw=(code/name).read_bytes().replace(b'\r\n',b'\n')
        if hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()!=oid:
            raise ValueError('source code blob changed: '+name)
    sys.path.insert(0,str(code))
    import launcher, learning, formal
    # Loaded runtime includes native extension/backend/adapter hashes.
    runtime=launcher.runtime()
    for k,v in runtime.items():
        if original[k]!=v: raise ValueError('loaded runtime differs: '+k)
    if original['original_config_git_blob']!=CONTRACT['config_git_blob']:
        raise ValueError('config pin differs')
    tables={s:read(base/('stage1/'+s+'-targets.json')) for s in ('train','validation')}
    roots={s:read(base/('stage1/cohort-'+s+'.json'))['roots'] for s in ('train','validation','heldout')}
    formal.validate_splits(roots)
    for split in tables:
        expected_rows={formal.digest(formal.keys(r)):r for r in roots[split]}
        if len(tables[split])!=len(expected_rows): raise ValueError('source table incomplete')
        for table in tables[split]:
            r=expected_rows.pop(table['root_id'])
            if table['split']!=split or table['owner']!=r['owner'] or table['serialized']!=r['serialized_public_input'] or len(table['q'])!=r['legal_count']:
                raise ValueError('source table/root mismatch')
    target=sha([[r['owner'],r['root_id'],r['q']] for r in tables['train']])
    if target!=CONTRACT['train_target_digest']: raise ValueError('training target digest mismatch')
    return {'original':original,'files':pinned,'code_blobs':expected},tables,roots

def train_block(tables,block):
    import learning
    if block not in range(6): raise ValueError('unknown block')
    original=learning.CONFIG
    # Exactly the original fit function; only prospective initialization seeds
    # are overridden in memory. The immutable source/config files are untouched.
    try:
        learning.CONFIG={**original,'model_seeds':CONTRACT['model_seeds'][block]}
        result=learning.fit(tables['train'],tables['validation'])
    finally: learning.CONFIG=original
    for value in result.values():
        c=value['checkpoint']
        # Seed/selection provenance lives in the block manifest; load uses the
        # unchanged semantic/normalization configuration of the source model.
        c['config_sha256']=sha(original); c.pop('checkpoint_sha256'); c['checkpoint_sha256']=sha(c)
        learning.load(c)
    return result

def block_plan(checkpoints,block):
    import formal
    original=formal.CONFIG
    try:
        formal.CONFIG={**original,'fresh_seeds':CONTRACT['fresh_seeds']}
        rows=[r for r in formal.evaluation_plan(checkpoints) if r['arm']!='expert_source']
    finally: formal.CONFIG=original
    return [{**r,'block':block} for r in rows]

def manifest(blocks):
    if set(blocks)!=set(range(6)): raise ValueError('all six blocks must freeze first')
    rows=[p for b in range(6) for p in block_plan(blocks[b],b)]
    if len(rows)!=1536 or len({(p['block'],p['seed'],p['arm']) for p in rows})!=1536:
        raise ValueError('run manifest incomplete')
    return rows

def evaluate(adapter,plan,checkpoints,store,progress):
    import formal
    block=plan['block']
    if plan not in block_plan(checkpoints,block): raise ValueError('unfrozen evaluation key')
    original=formal.CONFIG
    try:
        formal.CONFIG={**original,'fresh_seeds':CONTRACT['fresh_seeds']}
        return formal.evaluate_one(adapter,{k:v for k,v in plan.items() if k!='block'},checkpoints,store,progress)
    finally: formal.CONFIG=original

def percentile(values):
    samples=sorted(values); return {'mean':sum(samples)/len(samples),
        'lower':samples[int(.025*len(samples))],'upper':samples[int(.975*len(samples))]}

def crossed(grid,seed=440901,replicates=10000):
    if len(grid)!=6 or any(len(row)!=128 for row in grid): raise ValueError('crossed grid must be6x128')
    rng=random.Random(seed); samples=[]
    for _ in range(replicates):
        blocks=[rng.randrange(6) for _ in range(6)]
        seeds=[rng.randrange(128) for _ in range(128)]
        # Multiplicity weights keep the crossed pairs intact and avoid IID flattening.
        bc=[blocks.count(b) for b in range(6)]; sc=[seeds.count(s) for s in range(128)]
        samples.append(sum(bc[b]*sum(sc[s]*grid[b][s] for s in range(128)) for b in range(6))/768)
    result=percentile(samples); result['mean']=sum(map(sum,grid))/768
    return {**result,'bootstrap_seed':seed,'replicates':replicates,'resampling':'independent blocks and seeds; crossed paired grid'}

def classify(valid,coverage,interval,means):
    if not valid: return 'INCOMPLETE'
    if not coverage: return 'FRESH_LEARNED_CONTROL_INSUFFICIENT'
    if interval['lower']>0 and sum(m>0 for m in means)>=5: return 'SEMANTIC_GAIN_MODEL_SEED_ROBUST_WITHIN_FIXED_DATA'
    if interval['upper']<0 and sum(m<0 for m in means)>=5: return 'SEMANTIC_HARM_MODEL_SEED_REPLICATED'
    return 'MODEL_SEED_ROBUSTNESS_NOT_ESTABLISHED'

def report(runs,blocks,train_roots):
    from collections import Counter
    import formal,reporting
    plan=manifest(blocks); by={(r['block'],r['seed'],r['arm']):r for r in runs}
    if len(runs)!=1536 or set(by)!={(p['block'],p['seed'],p['arm']) for p in plan}:
        raise ValueError('INCOMPLETE: original1536 keys required')
    coverage=True; block_reports=[]; grid=[]; compact=[]
    pooled={a:Counter() for a in ('visible','masked')}
    for b in range(6):
        rows=[by[b,s,a] for s in range(435001,435129) for a in ('visible','masked')]
        expected={(p['seed'],p['arm']):p for p in block_plan(blocks[b],b)}
        counts={a:Counter() for a in pooled}; reach={a:Counter() for a in pooled}; floors={a:Counter() for a in pooled}
        shared=Counter(); disagree=Counter()
        for r in rows:
            p=expected[r['seed'],r['arm']]
            if not r['terminal'] or r['problems'] or r['key']!=[p['key'],p['seed'],p['arm']] or r['outcome'] not in ('PLAYER_LOSS','PLAYER_VICTORY'):
                raise ValueError('INCOMPLETE: terminal/key mismatch')
            if r['steps']>20000 or r['final_raw_privileged']['outcome']!=r['outcome'] or r['floor']!=r['final_raw_privileged']['floor_num']:
                raise ValueError('INCOMPLETE: native terminal mismatch')
            if r['outcome']=='PLAYER_LOSS' and r['final_raw_privileged']['cur_hp']!=0: raise ValueError('INCOMPLETE: false death')
            a=r['arm']; floors[a][r['floor']]+=1; pooled[a][r['floor']]+=1
            for act in (2,3,4): reach[a]['act'+str(act)]+=int(r['max_act']>=act)
            for k in ('heart_encountered','heart_win'): reach[a][k]+=int(r[k])
            for c in r['choices']:
                counts[a][c['family']+'_'+c['route']]+=1
                if c['family'] in ('MAP_SCREEN','REWARDS') and c['route']!='learned': raise ValueError('PUBLIC_INPUT_INVALID: supported fallback')
            compact.append({k:v for k,v in r.items() if k not in ('choices','identity','final_raw_privileged')})
        for s in range(435001,435129):
            indexed=[]
            for a in ('visible','masked'):
                seen=Counter(); out={}
                for c in by[b,s,a]['choices']:
                    if 'public_sha256' not in c: continue
                    h=c['public_sha256']; out[h,seen[h]]=c; seen[h]+=1
                indexed.append(out)
            for key in indexed[0].keys()&indexed[1].keys():
                v,m=indexed[0][key],indexed[1][key]
                if v['serialized_public_input']!=m['serialized_public_input']: raise ValueError('public hash collision')
                shared[v['family']]+=1; disagree[v['family']]+=int(v['ordinal']!=m['ordinal'])
        differences=[by[b,s,'visible']['floor']-by[b,s,'masked']['floor'] for s in range(435001,435129)]; grid.append(differences)
        coverage &= all(counts[a][f+'_learned']>=64 for a in counts for f in ('MAP_SCREEN','REWARDS'))
        ci=formal.clustered_interval({s:[d] for s,d in zip(range(435001,435129),differences)},CONTRACT['block_bootstrap_seeds'][b],10000)
        block_reports.append({'block':b,'paired_interval':ci,'signs':{'positive':sum(d>0 for d in differences),'negative':sum(d<0 for d in differences),'tie':differences.count(0)},'choice_counts':{a:dict(c) for a,c in counts.items()},'progress':{a:dict(c) for a,c in reach.items()},'terminal_floors':{a:dict(c) for a,c in floors.items()},'shared_states':dict(shared),'disagreements':dict(disagree),'shift':reporting.shift(train_roots,rows)})
    ci=crossed(grid); means=[sum(r)/128 for r in grid]
    return {'classification':classify(True,coverage,ci,means),'purpose':'STUDY_ONLY',
        'information_regime':'fixed-data model-seed replication; privileged teacher-assisted Battle; not noSL',
        'crossed_interval':ci,'blocks':block_reports,'differences':grid,'runs':compact,
        'pooled_terminal_floors':{a:dict(c) for a,c in pooled.items()},
        'search_work':{k:sum(r[k] for r in runs) for k in ('battle_search_calls','battle_search_cpu','battle_search_wall')},
        'historical_discovery':'Issue43 primary+0.984375 [0.1328125,1.84375]; not pooled'}
