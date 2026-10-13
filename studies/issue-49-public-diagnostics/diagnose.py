"""Read-only frozen model diagnostics. No optimizer steps or simulator import."""
import json, hashlib, math, random, sys, time, os
from pathlib import Path
from collections import defaultdict
import torch
import student46, student47

torch.set_num_threads(1)
torch.set_num_interop_threads(1)
torch.use_deterministic_algorithms(True)
DT=torch.float64
OLD46=Path('/home/lsmft/.cache/issue46-source-capacity')
OLD47=Path('/home/lsmft/.cache/issue47-action-preference')
def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def write(p,v):
    temp=p.with_suffix('.tmp'); temp.write_text(json.dumps(v,sort_keys=True,indent=2,allow_nan=False)+'\n'); temp.replace(p)
def initialize(capacity,seed):
    sizes=[128,16,1] if capacity=='T' else [128,256,256,1]
    layers=[]
    for i,(a,b) in enumerate(zip(sizes,sizes[1:])):
        layers.append(torch.nn.Linear(a,b,dtype=DT))
        if i<len(sizes)-2: layers.append(torch.nn.Tanh())
    model=torch.nn.Sequential(*layers)
    if capacity=='T':
        s=student47.Scorer(seed); weights=[s.w,[s.v]]; biases=[s.b,[s.c]]
    else:
        rng=random.Random(seed)
        weights=[[[rng.uniform(-.05,.05) for _ in range(a)] for _ in range(b)] for a,b in zip(sizes,sizes[1:])]
        biases=[[0.]*b for b in sizes[1:]]
    assign(model,weights,biases); return model
def assign(model,weights,biases):
    with torch.no_grad():
        for layer,w,b in zip([m for m in model if isinstance(m,torch.nn.Linear)],weights,biases):
            layer.weight.copy_(torch.tensor(w,dtype=DT)); layer.bias.copy_(torch.tensor(b,dtype=DT))
def load(cp):
    model=initialize(cp['capacity'],cp['seed']); st=cp['state']
    assign(model,[st['w'],[st['v']]] if cp['capacity']=='T' else st['weights'],[st['b'],[st['c']]] if cp['capacity']=='T' else st['biases']); return model
def encode(tables,module):
    rows=[]; groups=[]
    for t in tables:
        value=module.treatment(t['root']['serialized'],'visible'); offset=len(rows)
        assert len(t['q'])==len(value['legal_options'])
        rows.extend(module.features(value,i) for i in range(len(t['q'])))
        groups.append((offset,len(t['q']),t))
    return torch.tensor(rows,dtype=DT),groups
def root_metrics(scores,initial,groups):
    result=[]
    for off,n,t in groups:
        a=initial[off:off+n].tolist(); b=scores[off:off+n].tolist(); q=t['q']; r=t['root']
        ia=max(range(n),key=lambda i:a[i]); ib=max(range(n),key=lambda i:b[i])
        def margin(v): return max(v)-sorted(v)[-2] if n>1 else 0.
        pair=[(i,j) for i in range(n) for j in range(i+1,n) if q[i]!=q[j]]
        def concord(v): return sum((v[i]-v[j])*(q[i]-q[j])>0 for i,j in pair)/len(pair) if pair else None
        def centered(v): return sum(((v[i]-sum(v)/n)-(q[i]-sum(q)/n)/60)**2 for i in range(n))/n
        result.append({'owner':r['owner'],'split':r['split'],'family':r['family'],'assisted':r['assisted'],'n':n,'initial_choice':ia,'selected_choice':ib,'agreement':ia==ib,'initial_regret':max(q)-q[ia],'selected_regret':max(q)-q[ib],'initial_mse':sum((a[i]-q[i]/60)**2 for i in range(n))/n,'selected_mse':sum((b[i]-q[i]/60)**2 for i in range(n))/n,'initial_centered_mse':centered(a),'selected_centered_mse':centered(b),'initial_margin':margin(a),'selected_margin':margin(b),'initial_concordance':concord(a),'selected_concordance':concord(b),'mean_score_shift':sum(b[i]-a[i] for i in range(n))/n,'centered_score_change_rms':math.sqrt(sum(((b[i]-sum(b)/n)-(a[i]-sum(a)/n))**2 for i in range(n))/n)})
    return result
def aggregate(rows):
    groups=defaultdict(list)
    for r in rows:
        groups[(r['split'],'ALL','ALL')].append(r)
        groups[(r['split'],r['family'],'assisted' if r['assisted'] else 'natural')].append(r)
    out=[]
    for (split,family,origin),rs in sorted(groups.items()):
        fields=[k for k in rs[0] if k.startswith(('initial_','selected_','mean_','centered_')) and k not in ('initial_choice','selected_choice')]
        out.append({'split':split,'family':family,'origin':origin,'roots':len(rs),'argmax_agreement':sum(r['agreement'] for r in rs)/len(rs),**{k:sum(r[k] for r in rs if r[k] is not None)/sum(r[k] is not None for r in rs) if any(r[k] is not None for r in rs) else None for k in fields}})
    return out
def grads(model,loss):
    gs=torch.autograd.grad(loss,tuple(model.parameters()),allow_unused=True)
    return torch.cat([g.flatten() if g is not None else torch.zeros_like(p).flatten() for g,p in zip(gs,model.parameters())])
def layer_norms(model,g):
    result={}; off=0
    for name,p in model.named_parameters():
        x=g[off:off+p.numel()]; result[name]={'l2':float(x.norm()),'max_abs':float(x.abs().max()),'mean':float(x.mean())};off+=p.numel()
    return result
def objective(scores,groups,arm):
    terms=[]; informative=0
    for off,n,t in groups:
        s=scores[off:off+n]; y=torch.tensor(t['q'],dtype=DT)/60
        if arm=='A': terms.append((s-y).square().mean())
        else:
            pairs=[(i,j) if t['q'][i]>t['q'][j] else (j,i) for i in range(n) for j in range(i+1,n) if t['q'][i]!=t['q'][j]]
            if pairs:
                terms.append(torch.stack([torch.nn.functional.softplus(-(s[i]-s[j])) for i,j in pairs]).mean()); informative+=1
    return torch.stack(terms).mean(),informative
def gradient_diagnostic(model,x,groups,arm):
    s=model(x).flatten(); loss,inf=objective(s,groups,arm); g=grads(model,loss)
    root_g=[]; differences=[]; derivative=[]; diff_gradient=[]
    for off,n,t in groups:
        local=model(x[off:off+n]).flatten(); local_loss,_=objective(local,[(0,n,t)],arm) if arm=='A' or len(set(t['q']))>1 else (None,0)
        if local_loss is not None: root_g.append(grads(model,local_loss))
        # Every informative pair, rather than a sampled favorable pair.
        for i in range(n):
            for j in range(i+1,n):
                if t['q'][i]==t['q'][j]: continue
                hi,lo=(i,j) if t['q'][i]>t['q'][j] else (j,i)
                pairscore=model(x[off:off+n]).flatten(); d=pairscore[hi]-pairscore[lo]
                differences.append(float(d.detach())); derivative.append(float((-torch.sigmoid(-d)).detach()));diff_gradient.append(float(grads(model,d).norm()))
    matrix=torch.stack(root_g); norms=matrix.norm(dim=1); mean=matrix.mean(dim=0); mean_norm=float(mean.norm()); denom=float(norms.mean())
    normed=matrix/torch.clamp(norms[:,None],min=1e-300)
    # mean off-diagonal cosine, exact algebra without materializing pair matrix.
    count=len(root_g); mean_cos=float((normed.sum(dim=0).square().sum()-normed.square().sum())/(count*(count-1))) if count>1 else None
    def summary(a): return {'count':len(a),'min':min(a) if a else None,'mean':sum(a)/len(a) if a else None,'max':max(a) if a else None,'rms':math.sqrt(sum(v*v for v in a)/len(a)) if a else None}
    with torch.no_grad():
        # Directional first-order effect; no update, no training or checkpoint selection.
        params=list(model.parameters()); off=0
        backup=[p.clone() for p in params]
        for p in params: p.add_(g[off:off+p.numel()].reshape(p.shape),alpha=-.01);off+=p.numel()
        directional=model(x).flatten()-s.detach()
        for p,b in zip(params,backup):p.copy_(b)
    centered=[]
    for off,n,t in groups:
        d=directional[off:off+n]; centered.extend((d-d.mean()).tolist())
    return {'objective':arm,'loss':float(loss.detach()),'informative_roots':inf if arm=='P' else sum(len(set(t['q']))>1 for _,_,t in groups),'full_batch_gradient':layer_norms(model,g),'full_batch_l2':float(g.norm()),'root_gradient_norms':summary(norms.tolist()),'cancellation_ratio_norm_mean_over_mean_norm':mean_norm/denom if denom else 0.,'mean_pairwise_root_gradient_cosine':mean_cos,'full_gradient_matches_mean_root_max_error':float((g-mean).abs().max()),'score_difference':summary(differences),'logistic_derivative':summary(derivative),'pair_score_gradient_l2':summary(diff_gradient),'hypothetical_one_SGD001_score_change':summary(directional.tolist()),'hypothetical_one_SGD001_centered_score_change':summary(centered),'no_optimizer_step_or_checkpoint_saved':True}
def main():
    start=time.process_time(); out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True)
    ledger={'budget_upper_cpu_seconds':3000,'administrative_upper_charge':150,'stage':'READING','measured_cpu_seconds':0,'fresh_games':0,'training_steps':0};write(out/'execution.json',ledger)
    receipts=[]
    def read(p):
        receipts.append({'path':str(p),'sha256':digest(p),'bytes':p.stat().st_size});return json.loads(p.read_text())
    for path,expected in [(OLD46/'artifact-manifest.json','9807c5a02554f57947db1d507d0ed49b10fa07fab88042976b44e60a879e0cd1'),(OLD47/'artifact-manifest.json','119a514692836954ab205284c35d1070a9a089cebcc9344fd27ded2c850ff1de')]:assert digest(path)==expected;read(path)
    source=read(OLD47/'source.json'); tables=[]
    for d in source['derived_files']:
        p=OLD47/'inputs'/d['input_path'];assert digest(p)==d['derived_sha256'];t=read(p);assert digest(OLD46/d['source_path'])==d['source_table_sha256'];original=read(OLD46/d['source_path']);assert t['q']==original['q'] and t['root']['serialized']==original['root']['serialized'];tables.append(t)
    assert len(tables)==512 and len({t['root']['owner'] for t in tables})==512
    # Epoch0 seeds are exactly those of each selected frozen checkpoint.
    result={}; per_root={}; cp_receipts={}
    small=set(read(OLD46/'training/small-cohort.json')['owners'])
    for study,store,cells,module in [('46',OLD46,['SxT','SxB','LxT','LxB'],student46),('47',OLD47,['A','P'],student47)]:
        x,groups=encode(tables,module)
        for cell in cells:
            cp=read(store/'training'/(cell+'.json')); asserted=dict(cp);asserted.pop('sha256');assert hashlib.sha256(json.dumps(asserted,sort_keys=True,separators=(',',':')).encode()).hexdigest()==cp['sha256'];initial=initialize(cp['capacity'],cp['seed']);selected=load(cp)
            with torch.no_grad(): rows=root_metrics(selected(x).flatten(),initial(x).flatten(),groups)
            key=study+'/'+cell;per_root[key]=rows;result[key]={'checkpoint_sha256':cp['sha256'],'seed':cp['seed'],'epoch':cp['epoch'],'parameters':cp['parameters'],'partition_metrics':aggregate(rows)}
            # Training loss uses actual training subset, not all384 for S.
            trs=[t for t in tables if t['root']['split']=='train' and (not cell.startswith('S') or t['root']['owner'] in small)]
            tx,tg=encode(trs,module)
            with torch.no_grad():
                result[key]['initial_actual_training_mse']=float(objective(initial(tx).flatten(),tg,'A')[0]);result[key]['selected_actual_training_mse']=float(objective(selected(tx).flatten(),tg,'A')[0])
            if study=='47':result[key]['epoch0_gradient']=gradient_diagnostic(initial,tx,tg,cell)
            optimization=read(store/'training/optimization.json')[cell]
            track=[r for r in optimization if r['seed']==cp['seed']]
            result[key]['frozen_optimization_track']={'first':track[0],'last':track[-1],'selected':next(r for r in track if r['epoch']==cp['epoch'])}
            ledger.update(stage=key,measured_cpu_seconds=time.process_time()-start);write(out/'execution.json',ledger)
            assert ledger['measured_cpu_seconds']+150<1800,'Builder analysis budget leaves Reviewer reserve'
    cells=defaultdict(int)
    for t in tables: r=t['root'];cells[(r['split'],r['family'],r['assisted'])]+=1
    write(out/'diagnostics.json',{'models':result,'coverage':[{'split':s,'family':f,'assisted':a,'roots':cells[(s,f,a)]} for s in ('train','validation','heldout') for f in ('MAP_SCREEN','REWARDS') for a in (False,True)],'formula':'Root-balanced option MSE against Q/60. Preference mean of softplus(-(s_high-s_low)) within informative root then average informative roots. Root cancellation norm(mean gradient)/mean(norm gradient). Exact mean root cosine over all pairs. No fitted diagnostic model.','heldout_retrospective_not_unseen':True})
    write(out/'per-root.json',per_root);write(out/'inputs.json',receipts)
    ledger.update(stage='DIAGNOSTICS_COMPLETE',measured_cpu_seconds=time.process_time()-start);write(out/'execution.json',ledger)
    print(json.dumps(ledger))
if __name__=='__main__':main()
