"""All four cells, validation selection only; heldout read after checkpoints freeze."""
import copy
import json
from pathlib import Path
import random
import sys
import time
from collections import defaultdict,Counter
import torch
from common import CONFIG,owner_plan,sha,write
from student import Scorer,features,treatment

torch.set_num_threads(1); torch.set_num_interop_threads(1)
torch.use_deterministic_algorithms(True)
STORE=Path(sys.argv[1]); OUT=STORE/'training'; OUT.mkdir(exist_ok=True)
START=time.process_time()

def small_roots(roots):
    result=[]
    for assisted in (False,True):
        groups=defaultdict(list)
        for root in roots:
            if root['assisted']==assisted: groups[(root['act'],root['family'],root['floor'])].append(root)
        for rows in groups.values(): rows.sort(key=lambda r:sha([46003,r['owner']]))
        strata=sorted(groups)
        chosen=[]
        while len(chosen)<48:
            changed=False
            for stratum in strata:
                if groups[stratum]: chosen.append(groups[stratum].pop(0)); changed=True
                if len(chosen)==48: break
            if not changed: raise ValueError('SMALL_COHORT_UNAVAILABLE')
        result.extend(chosen)
    return sorted(result,key=lambda r:r['owner'])

def arrays(tables):
    xs=[]; ys=[]; ws=[]
    for table in tables:
        root=table['root']; value=treatment(root['serialized'],'visible')
        if len(table['q'])!=len(value['legal_options']): raise ValueError('all-action label mismatch')
        for i,q in enumerate(table['q']):
            xs.append(features(value,i)); ys.append([q/60]); ws.append([1/(len(table['q'])*len(tables))])
    return tuple(torch.tensor(v,dtype=torch.float64) for v in (xs,ys,ws))

def initialize(capacity,seed):
    sizes=[128,16,1] if capacity=='T' else [128,256,256,1]
    modules=[]
    for i,(a,b) in enumerate(zip(sizes,sizes[1:])):
        modules.append(torch.nn.Linear(a,b,dtype=torch.float64))
        if i<len(sizes)-2: modules.append(torch.nn.Tanh())
    model=torch.nn.Sequential(*modules); layers=[m for m in model if isinstance(m,torch.nn.Linear)]
    if capacity=='T':
        original=Scorer(seed); weights=[original.w,[original.v]]; biases=[original.b,[original.c]]
    else:
        rng=random.Random(seed); weights=[]; biases=[]
        for a,b in zip(sizes,sizes[1:]):
            weights.append([[rng.uniform(-.05,.05) for _ in range(a)] for _ in range(b)]); biases.append([0.]*b)
    with torch.no_grad():
        for layer,w,b in zip(layers,weights,biases):
            layer.weight.copy_(torch.tensor(w,dtype=torch.float64)); layer.bias.copy_(torch.tensor(b,dtype=torch.float64))
    return model

def state(model,capacity):
    layers=[m for m in model if isinstance(m,torch.nn.Linear)]
    if capacity=='T': return {'w':layers[0].weight.tolist(),'b':layers[0].bias.tolist(),'v':layers[1].weight.tolist()[0],'c':layers[1].bias.tolist()[0]}
    return {'weights':[m.weight.tolist() for m in layers],'biases':[m.bias.tolist() for m in layers]}

def loss(model,rows):
    x,y,w=rows
    return ((model(x)-y).square()*w).sum()

def main():
    plan=owner_plan()
    if any(not (STORE/str(p['owner'])/'complete.json').exists() for p in plan): raise ValueError('INCOMPLETE_SOURCE_GRID')
    # The subset is frozen from root/provenance files before this code reads ANY q table.
    roots=[json.loads((STORE/str(p['owner'])/'root.json').read_text()) for p in plan if p['split']=='train']
    small=small_roots(roots)
    write(OUT/'small-cohort.json',{'owners':[r['owner'] for r in small],'root_ids':[r['root_id'] for r in small],'rule':CONFIG['small_selection']})
    train=[json.loads((STORE/str(r['owner'])/'table.json').read_text()) for r in roots]
    validation=[json.loads((STORE/str(p['owner'])/'table.json').read_text()) for p in plan if p['split']=='validation']
    xs=arrays([t for t in train if t['root']['owner'] in {r['owner'] for r in small}]); xl=arrays(train); xv=arrays(validation)
    checkpoints={}; optimization={}
    for scale in ('S','L'):
        for capacity in ('T','B'):
            cell=scale+'x'+capacity; rows=xs if scale=='S' else xl; best=None; records=[]
            selected_tables=[t for t in train if scale=='L' or t['root']['owner'] in {r['owner'] for r in small}]
            for seed in CONFIG['model_seeds']:
                model=initialize(capacity,seed)
                optimizer=torch.optim.SGD(model.parameters(),lr=CONFIG['training']['learning_rate'],weight_decay=0)
                for epoch in range(1,CONFIG['training']['epochs']+1):
                    optimizer.zero_grad(); value=loss(model,rows); value.backward(); optimizer.step()
                    with torch.no_grad(): vl=float(loss(model,xv)); tl=float(loss(model,rows))
                    records.append({'seed':seed,'epoch':epoch,'train_mse':tl,'validation_mse':vl})
                    key=(vl,epoch,seed)
                    if best is None or key<best[0]: best=(key,state(model,capacity))
            checkpoint={'cell':cell,'scale':scale,'capacity':capacity,'seed':best[0][2],'epoch':best[0][1],'validation_mse':best[0][0],'state':best[1],'config_sha256':sha(CONFIG),'train_targets_sha256':sha(selected_tables),'validation_targets_sha256':sha(validation),'parameters':CONFIG['training']['tiny_parameters' if capacity=='T' else 'bigger_parameters'],'purpose':'STUDY_ONLY'}
            checkpoint['sha256']=sha(checkpoint); checkpoints[cell]=checkpoint; optimization[cell]=records
            write(OUT/(cell+'.json'),checkpoint)
    write(OUT/'checkpoints.json',checkpoints); write(OUT/'optimization.json',optimization)
    # Heldout is entered only after all four selected checkpoint files exist.
    heldout=[json.loads((STORE/str(p['owner'])/'table.json').read_text()) for p in plan if p['split']=='heldout']
    xh=arrays(heldout); reports={}
    for cell,checkpoint in checkpoints.items():
        model=initialize(checkpoint['capacity'],checkpoint['seed']); layers=[m for m in model if isinstance(m,torch.nn.Linear)]
        st=checkpoint['state']; weights=[st['w'],[st['v']]] if checkpoint['capacity']=='T' else st['weights']; biases=[st['b'],[st['c']]] if checkpoint['capacity']=='T' else st['biases']
        with torch.no_grad():
            for m,w,b in zip(layers,weights,biases): m.weight.copy_(torch.tensor(w,dtype=torch.float64)); m.bias.copy_(torch.tensor(b,dtype=torch.float64))
            reports[cell]={'heldout_mse':float(loss(model,xh)),'checkpoint_sha256':checkpoint['sha256']}
    write(OUT/'heldout.json',reports)
    counts={split:dict(Counter(f"act{r['act']}/floor{r['floor']}/{r['family']}" for r in [json.loads((STORE/str(p['owner'])/'root.json').read_text()) for p in plan if p['split']==split])) for split in ('train','validation','heldout')}
    write(OUT/'complete.json',{'checkpoints':{k:v['sha256'] for k,v in checkpoints.items()},'roots':counts,'cpu':time.process_time()-START,'torch_version':torch.__version__,'dtype':'float64','threads':1})

if __name__=='__main__': main()
