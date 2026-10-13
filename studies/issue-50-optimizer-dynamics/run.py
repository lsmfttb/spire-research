"""Exactly four frozen 100-epoch fits; source metadata split is already sealed."""
import json,sys,time,math,copy
from pathlib import Path
import torch
import train47
from freeze import OLD46,OLD47,sha,write,value_sha

def flat(model):return torch.cat([p.detach().flatten() for p in model.parameters()])
def norms(model,grad=False):return {n:float((p.grad if grad else p.detach()).norm()) for n,p in model.named_parameters()}
def optimizer(model,arm):
    if arm=='S':return torch.optim.SGD(model.parameters(),lr=.01,momentum=0,weight_decay=0)
    return torch.optim.Adam(model.parameters(),lr=.001,betas=(.9,.999),eps=1e-8,weight_decay=0)
def verify(batch):
    errors={}
    for arm in ('S','D'):
        actual=train47.initialize(446101);manual=copy.deepcopy(actual);opt=optimizer(actual,arm)
        m=[torch.zeros_like(p) for p in manual.parameters()];v=[torch.zeros_like(p) for p in manual.parameters()]
        errs=[]
        for step in (1,2):
            opt.zero_grad();loss=train47.objective(actual(batch['x']).flatten(),batch,'P');loss.backward();assert all(torch.isfinite(p.grad).all() for p in actual.parameters());opt.step()
            manual.zero_grad();train47.objective(manual(batch['x']).flatten(),batch,'P').backward()
            with torch.no_grad():
                for j,p in enumerate(manual.parameters()):
                    if arm=='S':p.add_(p.grad,alpha=-.01)
                    else:
                        m[j]=.9*m[j]+.1*p.grad;v[j]=.999*v[j]+.001*p.grad.square()
                        p.add_((m[j]/(1-.9**step))/(torch.sqrt(v[j]/(1-.999**step))+1e-8),alpha=-.001)
            error=float((flat(actual)-flat(manual)).abs().max());assert error<1e-13;errs.append(error)
            if arm=='D':
                for j,p in enumerate(actual.parameters()):
                    assert float((opt.state[p]['exp_avg']-m[j]).abs().max())<1e-14
                    assert float((opt.state[p]['exp_avg_sq']-v[j]).abs().max())<1e-14
        errors[arm]=errs
    assert max(range(4),key=lambda i:[1,1,0,1][i])==0
    return {'passed':True,'actual_training_batch_roots':len(batch['groups']),'independent_manual_step_max_errors':errors,'Adam_moments_verified_steps':[1,2],'first_max_ordinal_tie_rule':True,'finite_gradients':True,'model_parameters':sum(p.numel() for p in actual.parameters()),'exact_original_objective_and_initializer':True}
def metrics(model,initial,data,rows_by_owner):
    with torch.no_grad():scores=model(data['x']).flatten();baseline=initial(data['x']).flatten();loss=float(train47.objective(scores,data,'P'))
    roots=[]
    for off,n,t in data['groups']:
        s=scores[off:off+n].tolist();s0=baseline[off:off+n].tolist();q=t['q'];owner=t['root']['owner'];meta=rows_by_owner[owner]
        chosen=max(range(n),key=lambda i:s[i]);initial_choice=max(range(n),key=lambda i:s0[i]);pairs=[(i,j) for i in range(n) for j in range(i+1,n) if q[i]!=q[j]]
        def margin(v):return max(v)-sorted(v)[-2] if n>1 else 0.
        oriented=[(i,j) if q[i]>q[j] else (j,i) for i,j in pairs]
        pair_loss=sum(math.log1p(math.exp(-(s[i]-s[j]))) for i,j in oriented)/len(oriented) if oriented else None
        original_options=json.loads(t['root']['serialized'])['legal_options']
        roots.append({'owner':owner,'root_id':meta['root_id'],'assisted':meta['assisted'],'old45':meta['old45'],'family':meta['family'],'split':meta['diagnostic_split'],'legal_count':n,'scores':s,'initial_scores':s0,'q':q,'chosen':chosen,'selected_native_action':original_options[chosen]['action'],'initial_choice':initial_choice,'choice_changed':chosen!=initial_choice,'regret':max(q)-q[chosen],'initial_regret':max(q)-q[initial_choice],'margin':margin(s),'initial_margin':margin(s0),'top_score_ties':sum(v==max(s) for v in s),'optimal_q_ties':sum(v==max(q) for v in q),'positive_pairs':len(pairs),'pairwise_logistic_loss':pair_loss,'common_score_shift':sum(s[i]-s0[i] for i in range(n))/n,'centered_score_motion_rms':math.sqrt(sum(((s[i]-sum(s)/n)-(s0[i]-sum(s0)/n))**2 for i in range(n))/n)})
    return {'pairwise_logistic_loss':loss,'informative_roots':data['informative'],'uninformative_roots':len(roots)-data['informative'],'mean_regret':sum(r['regret'] for r in roots)/len(roots),'mean_initial_regret':sum(r['initial_regret'] for r in roots)/len(roots),'choice_changes':sum(r['choice_changed'] for r in roots),'roots':roots}
def main():
    start=time.process_time();store=Path(sys.argv[1]);ledger=json.loads((store/'execution.json').read_text());assert ledger['stage']=='SPLIT_FROZEN_BEFORE_Q'
    source=json.loads((store/'source.json').read_text());split=json.loads((store/'split.json').read_text());assert sha(store/'split.json')==source['split_sha256']==ledger['split_sha256']
    byowner={r['owner']:r for r in split['rows']};tables=[];receipts=[]
    ledger.update(stage='READING_FROZEN_TRAIN_Q',Q_values_opened=True);write(store/'execution.json',ledger)
    for r in source['training_source_records']:
        path=OLD47/'inputs'/r['input_path'];assert sha(path)==r['derived_sha256'];t=json.loads(path.read_text());orig=OLD46/r['source_path'];assert sha(orig)==r['source_table_sha256'];original=json.loads(orig.read_text());assert t['q']==original['q'] and t['root']['serialized']==original['root']['serialized']
        assert t['root']['root_id']==byowner[r['owner']]['root_id'];tables.append(t);receipts.extend([{'path':str(path),'sha256':sha(path)},{'path':str(orig),'sha256':sha(orig)}])
        options=json.loads(t['root']['serialized'])['legal_options'];assert t['root']['legal_count']==len(options)==len(t['q'])
        assert [o['candidate_index'] for o in options]==list(range(len(options)))
    assert len(tables)==384;train=[t for t in tables if byowner[t['root']['owner']]['diagnostic_split']=='train'];diag=[t for t in tables if byowner[t['root']['owner']]['diagnostic_split']=='diagnostic'];assert len(train)==320 and len(diag)==64
    tr=train47.encode(train);va=train47.encode(diag)
    # Provenance and Q never enter features. Copy both fields and verify exact x.
    altered=copy.deepcopy(train);altered[0]['root']['assisted']=not altered[0]['root']['assisted'];altered[0]['q']=[v+1 for v in altered[0]['q']]
    assert torch.equal(tr['x'],train47.encode(altered)['x'])
    check_start=time.process_time();checks=verify(tr);checks['features_independent_of_Q_and_assistance_metadata']=True;checks['all_384_original_native_option_ordinals_consistent']=True;checks['cpu_seconds']=time.process_time()-check_start;write(store/'verification.json',checks)
    write(store/'input-receipts.json',receipts);records=[];checkpoints={};outputs={}
    for seed in (446101,446102):
        initial=train47.initialize(seed)
        write(store/f'epoch0-{seed}.json',{'seed':seed,'epoch':0,'state':train47.state(initial)})
        for arm in ('S','D'):
            model=train47.initialize(seed);assert torch.equal(flat(model),flat(initial));opt=optimizer(model,arm);key=f'{arm}-{seed}'
            initial_result={s:metrics(model,initial,b,byowner) for s,b in [('train',tr),('diagnostic',va)]}
            for epoch in range(1,101):
                before=flat(model).clone();opt.zero_grad();loss=train47.objective(model(tr['x']).flatten(),tr,'P');loss.backward()
                assert all(torch.isfinite(p.grad).all() for p in model.parameters());g=norms(model,True);opt.step()
                after=flat(model)
                with torch.no_grad():post=float(train47.objective(model(tr['x']).flatten(),tr,'P'))
                record={'arm':arm,'seed':seed,'epoch':epoch,'loss_before':float(loss.detach()),'loss_after':post,'layer_gradient_l2':g,'step_l2':float((after-before).norm()),'state_sha256':value_sha(train47.state(model))};records.append(record)
                ledger.update(stage=f'{key}/epoch{epoch}',measured_stage_cpu_seconds={**ledger['measured_stage_cpu_seconds'],'run':time.process_time()-start});write(store/'execution.json',ledger);write(store/'optimization.json',records)
                assert sum(ledger['measured_stage_cpu_seconds'].values())+300+1000<3000,'All-in budget reserve exceeded'
            cp={'seed':seed,'arm':arm,'epoch':100,'state':train47.state(model),'parameters':2081,'objective':'exact47P','split_sha256':sha(store/'split.json')};cp['sha256']=value_sha(cp);write(store/f'{key}-epoch100.json',cp);checkpoints[key]=cp['sha256']
            outputs[key]={'epoch0':initial_result,'epoch100':{s:metrics(model,initial,b,byowner) for s,b in [('train',tr),('diagnostic',va)]}}
            write(store/'functional.json',outputs)
    assert len(records)==400 and all(sum(r['seed']==seed and r['arm']==arm for r in records)==100 for seed in (446101,446102) for arm in ('S','D'))
    ledger.update(stage='FOUR_FITS_COMPLETE',measured_stage_cpu_seconds={**ledger['measured_stage_cpu_seconds'],'run':time.process_time()-start},checkpoints=checkpoints);write(store/'execution.json',ledger)
    print(json.dumps({'stage':ledger['stage'],'cpu':ledger['measured_stage_cpu_seconds']['run'],'checkpoints':checkpoints}))
if __name__=='__main__':main()
