"""Two frozen tiny objectives, common validation regret selection, heldout after freeze."""
import json
from pathlib import Path
import sys
import time
import torch
from common import CONFIG,owner_plan,sha,write
from student import Scorer,features,treatment

torch.set_num_threads(1); torch.set_num_interop_threads(1)
torch.use_deterministic_algorithms(True)

def initialize(seed):
    model=torch.nn.Sequential(torch.nn.Linear(128,16,dtype=torch.float64),torch.nn.Tanh(),torch.nn.Linear(16,1,dtype=torch.float64))
    original=Scorer(seed)
    with torch.no_grad():
        for layer,w,b in zip((model[0],model[2]),(original.w,[original.v]),(original.b,[original.c])):
            layer.weight.copy_(torch.tensor(w,dtype=torch.float64)); layer.bias.copy_(torch.tensor(b,dtype=torch.float64))
    return model

def state(model): return {'w':model[0].weight.tolist(),'b':model[0].bias.tolist(),'v':model[2].weight.tolist()[0],'c':model[2].bias.tolist()[0]}

def encode(tables):
    rows=[]; groups=[]; high=[]; low=[]; counts=[]
    for table in tables:
        value=treatment(table['root']['serialized'],'visible'); q=table['q']
        assert len(q)==len(value['legal_options']) and len(q)>0
        offset=len(rows); rows.extend(features(value,i) for i in range(len(q)))
        groups.append((offset,len(q),table)); pairs=[]
        for i in range(len(q)):
            for j in range(i+1,len(q)):
                if q[i]!=q[j]: pairs.append((offset+i,offset+j) if q[i]>q[j] else (offset+j,offset+i))
        counts.append(len(pairs)); high.extend(a for a,b in pairs); low.extend(b for a,b in pairs)
    informative=sum(c>0 for c in counts)
    weights=[1/(informative*c) for c in counts if c for _ in range(c)] if informative else []
    target=torch.tensor([v/60 for t in tables for v in t['q']],dtype=torch.float64)
    option_weight=torch.tensor([1/(len(tables)*len(t['q'])) for t in tables for _ in t['q']],dtype=torch.float64)
    return {'x':torch.tensor(rows,dtype=torch.float64),'target':target,'weight':option_weight,'groups':groups,'high':torch.tensor(high,dtype=torch.long),'low':torch.tensor(low,dtype=torch.long),'pair_weight':torch.tensor(weights,dtype=torch.float64),'pair_counts':counts,'informative':informative}

def objective(score,data,arm):
    if arm=='A': return ((score-data['target']).square()*data['weight']).sum()
    assert data['informative']>0, 'No informative training roots'
    return (torch.nn.functional.softplus(-(score[data['high']]-score[data['low']]))*data['pair_weight']).sum()

def ranking(score,data):
    records=[]
    for offset,n,table in data['groups']:
        values=score[offset:offset+n].tolist(); chosen=max(range(n),key=lambda i:values[i]); q=table['q']; root=table['root']
        records.append({'owner':root['owner'],'root_id':root['root_id'],'family':root['family'],'floor':root['floor'],'act':root['act'],'assisted':root['assisted'],'chosen':chosen,'regret':max(q)-q[chosen],'return_rank':1+sum(v>q[chosen] for v in q),'optimal_return_ties':sum(v==max(q) for v in q),'top_score_ties':sum(v==max(values) for v in values),'legal_count':n,'informative_pairs':sum(q[i]!=q[j] for i in range(n) for j in range(i+1,n))})
    return {'mean_regret':sum(r['regret'] for r in records)/len(records),'roots':records}

def main():
    start=time.process_time(); store=Path(sys.argv[1]); out=store/'training'; out.mkdir(exist_ok=False)
    source=json.loads((store/'source.json').read_text())
    def tables(split):
        return [json.loads((store/'inputs'/(str(p['owner'])+'.json')).read_text()) for p in owner_plan() if p['split']==split]
    training=tables('train'); validation=tables('validation'); tr=encode(training); va=encode(validation)
    assert len(training)==384 and len(validation)==64 and tr['informative']>0
    checkpoints={}; all_records={}
    for arm in ('A','P'):
        best=None; records=[]
        for seed in CONFIG['model_seeds']:
            model=initialize(seed); optimizer=torch.optim.SGD(model.parameters(),lr=.01,weight_decay=0)
            for epoch in range(1,101):
                optimizer.zero_grad(); loss=objective(model(tr['x']).flatten(),tr,arm); loss.backward(); optimizer.step()
                with torch.no_grad():
                    train_loss=float(objective(model(tr['x']).flatten(),tr,arm)); ranks=ranking(model(va['x']).flatten(),va)
                regret=ranks['mean_regret']; records.append({'seed':seed,'epoch':epoch,'train_loss':train_loss,'validation_mean_regret':regret,'validation_choices':[r['chosen'] for r in ranks['roots']]})
                key=(regret,epoch,seed)
                if best is None or key<best[0]: best=(key,state(model))
        cp={'cell':arm,'capacity':'T','parameters':2081,'seed':best[0][2],'epoch':best[0][1],'validation_mean_regret':best[0][0],'state':best[1],'config_sha256':sha(CONFIG),'train_inputs_sha256':sha(training),'validation_inputs_sha256':sha(validation),'purpose':'STUDY_ONLY'}
        cp['sha256']=sha(cp); write(out/(arm+'.json'),cp); checkpoints[arm]=cp; all_records[arm]=records
    write(out/'checkpoints.json',checkpoints); write(out/'optimization.json',all_records)
    # No heldout input loaded before both selected checkpoints are frozen.
    heldout=tables('heldout'); he=encode(heldout); reports={}
    for arm,cp in checkpoints.items():
        model=initialize(cp['seed']); st=cp['state']
        with torch.no_grad():
            for layer,w,b in zip((model[0],model[2]),(st['w'],[st['v']]),(st['b'],[st['c']])):
                layer.weight.copy_(torch.tensor(w,dtype=torch.float64)); layer.bias.copy_(torch.tensor(b,dtype=torch.float64))
            reports[arm]={'checkpoint_sha256':cp['sha256'],**ranking(model(he['x']).flatten(),he)}
    write(out/'heldout.json',reports)
    write(out/'complete.json',{'cpu':time.process_time()-start,'torch_version':torch.__version__,'dtype':'float64','threads':1,'train_informative_roots':tr['informative'],'train_uninformative_roots':384-tr['informative'],'train_pair_counts':tr['pair_counts'],'heldout_opened_after_both_checkpoints_frozen':True,'checkpoints':{a:c['sha256'] for a,c in checkpoints.items()}})

if __name__=='__main__': main()
