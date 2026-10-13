"""Single predeclared S package; validation selection then heldout opening."""
import json
from pathlib import Path
import sys
import time
import torch
from common import CONFIG,owner_plan,sha,write
from structured import WIDTH,vocabulary,encode

torch.set_num_threads(1); torch.set_num_interop_threads(1)
torch.use_deterministic_algorithms(True)

class Network(torch.nn.Module):
    def __init__(self,n,seed):
        super().__init__(); torch.manual_seed(seed)
        self.embedding=torch.nn.Embedding(n,8,dtype=torch.float64)
        self.entity=torch.nn.Linear(8,8,dtype=torch.float64)
        self.scorer=torch.nn.Linear(42,32,dtype=torch.float64)
        self.out=torch.nn.Linear(32,1,dtype=torch.float64)
        with torch.no_grad():
            self.embedding.weight.uniform_(-.05,.05)
    def forward(self,d):
        tokens=self.embedding(d['tokens'])*d['values'][:,None]
        entities=torch.zeros((d['entities'],8),dtype=torch.float64).index_add(0,d['token_entity'],tokens)
        entities=torch.tanh(self.entity(entities/d['entity_count'][:,None]))
        pools=torch.zeros((d['rows']*5,8),dtype=torch.float64).index_add(0,d['entity_pool'],entities)
        pools=pools/d['pool_count'].clamp(min=1)[:,None]
        x=torch.cat((pools.reshape(d['rows'],40),d['extra']),dim=1)
        return self.out(torch.tanh(self.scorer(x))).flatten()

def state(m):
    return {'embedding':m.embedding.weight.tolist(),'entity_w':m.entity.weight.tolist(),'entity_b':m.entity.bias.tolist(),'score_w':m.scorer.weight.tolist(),'score_b':m.scorer.bias.tolist(),'out_w':m.out.weight.tolist()[0],'out_b':m.out.bias.tolist()[0]}

def restore(m,s):
    with torch.no_grad():
        for target,key in [(m.embedding.weight,'embedding'),(m.entity.weight,'entity_w'),(m.entity.bias,'entity_b'),(m.scorer.weight,'score_w'),(m.scorer.bias,'score_b'),(m.out.weight,'out_w'),(m.out.bias,'out_b')]:
            value=s[key]; value=[value] if key=='out_w' else [value] if key=='out_b' else value
            target.copy_(torch.tensor(value,dtype=torch.float64))

def batch(tables,vocab):
    tokens=[]; values=[]; token_entity=[]; entity_count=[]; entity_pool=[]; pool_count=[]; extra=[]; groups=[]; target=[]; weights=[]
    for table in tables:
        offset=len(extra); q=table['q']; groups.append((offset,len(q),table))
        for i,v in enumerate(q):
            channels,ex=encode(table['root']['serialized'],i,vocab); row=len(extra); extra.append(ex)
            target.append(v/60); weights.append(1/(len(tables)*len(q)))
            for j,channel in enumerate(channels):
                pool_count.append(len(channel))
                for entity in channel:
                    e=len(entity_count); entity_count.append(len(entity)); entity_pool.append(row*5+j)
                    for t,x in entity: tokens.append(t); values.append(x); token_entity.append(e)
    result={'rows':len(extra),'entities':len(entity_count),'groups':groups}
    for key,value in [('tokens',tokens),('values',values),('token_entity',token_entity),('entity_count',entity_count),('entity_pool',entity_pool),('pool_count',pool_count),('extra',extra),('target',target),('weight',weights)]:
        result[key]=torch.tensor(value,dtype=torch.long if key in ('tokens','token_entity','entity_pool') else torch.float64)
    return result

def loss(score,d): return ((score-d['target']).square()*d['weight']).sum()

def ranking(score,data):
    records=[]
    for offset,n,table in data['groups']:
        values=score[offset:offset+n].tolist(); chosen=max(range(n),key=lambda i:values[i]); q=table['q']; root=table['root']
        records.append({'owner':root['owner'],'family':root['family'],'floor':root['floor'],'act':root['act'],'assisted':root['assisted'],'chosen':chosen,'regret':max(q)-q[chosen],'return_rank':1+sum(v>q[chosen] for v in q),'optimal_return_ties':sum(v==max(q) for v in q),'top_score_ties':sum(v==max(values) for v in values),'score_spread':max(values)-min(values),'legal_count':n})
    return {'mean_regret':sum(r['regret'] for r in records)/len(records),'roots':records}

def main():
    start=time.process_time(); store=Path(sys.argv[1]); out=store/'training'; out.mkdir(exist_ok=False)
    def tables(split): return [json.loads((store/'inputs'/(str(p['owner'])+'.json')).read_text()) for p in owner_plan() if p['split']==split]
    training=tables('train'); vocab=vocabulary(training); m=Network(len(vocab),CONFIG['model_seeds'][0]); parameters=sum(p.numel() for p in m.parameters()); assert parameters<=30000
    # Vocabulary and architecture frozen before validation opens, and before Q-regret inspection.
    write(out/'package.json',{'config_sha256':sha(CONFIG),'vocabulary':vocab,'parameters':parameters,'train_inputs_sha256':sha(training),'architecture':'8d token -> mean entity/tanh8 -> five separate mean pools -> tanh32 joint scorer','omitted_visible_fields':CONFIG['encoder']['omitted_visible_fields']})
    tr=batch(training,vocab); validation=tables('validation'); va=batch(validation,vocab)
    best=None; records=[]
    for seed in CONFIG['model_seeds']:
        model=Network(len(vocab),seed); optimizer=torch.optim.Adam(model.parameters(),lr=.001,weight_decay=0)
        for epoch in range(1,201):
            optimizer.zero_grad(); objective=loss(model(tr),tr); objective.backward()
            grad=float(torch.sqrt(sum(p.grad.square().sum() for p in model.parameters())))
            embedding_grad=float(model.embedding.weight.grad.abs().max()); optimizer.step()
            with torch.no_grad():
                train_score=model(tr); train_loss=float(loss(train_score,tr)); ranks=ranking(model(va),va)
            record={'seed':seed,'epoch':epoch,'train_loss':train_loss,'gradient_l2':grad,'embedding_max_abs_gradient':embedding_grad,'train_score_std':float(train_score.std()),'validation_mean_regret':ranks['mean_regret'],'validation_choices':[r['chosen'] for r in ranks['roots']]}
            records.append(record); key=(ranks['mean_regret'],epoch,seed)
            if best is None or key<best[0]: best=(key,state(model),ranks)
            write(out/'optimization.json',{'S':records})
            write(out/'progress.json',{'seed':seed,'epoch':epoch,'cpu':time.process_time()-start})
    cp={'cell':'S','parameters':parameters,'seed':best[0][2],'epoch':best[0][1],'validation_mean_regret':best[0][0],'state':best[1],'vocabulary':vocab,'config_sha256':sha(CONFIG),'train_inputs_sha256':sha(training),'validation_inputs_sha256':sha(validation),'purpose':'STUDY_ONLY'}
    cp['sha256']=sha(cp); write(out/'S.json',cp)
    a=json.loads((store/'frozen-A.json').read_text()); write(out/'checkpoints.json',{'S':cp,'A':a}); write(out/'validation.json',best[2])
    # Selected S checkpoint durably frozen before any heldout table opens.
    heldout=tables('heldout'); he=batch(heldout,vocab); model=Network(len(vocab),cp['seed']); restore(model,cp['state'])
    from model import load
    comparator=load(a); ar=[]
    for t in heldout:
        scores=comparator.scores(t['root']['serialized'],'visible'); ar.extend(scores)
    with torch.no_grad(): report={'S':{'checkpoint_sha256':cp['sha256'],**ranking(model(he),he)},'A':{'checkpoint_sha256':a['sha256'],**ranking(torch.tensor(ar,dtype=torch.float64),he)}}
    write(out/'heldout.json',report)
    write(out/'complete.json',{'cpu':time.process_time()-start,'parameters':parameters,'torch_version':torch.__version__,'dtype':'float64','threads':1,'heldout_opened_after_S_frozen':True,'checkpoints':{'S':cp['sha256'],'A':a['sha256']}})
if __name__=='__main__': main()
