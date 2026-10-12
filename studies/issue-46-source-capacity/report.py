"""Prespecified paired primary and exploratory secondary contrasts, fail closed."""
import json
from pathlib import Path
import random
import sys
from collections import Counter
from common import CONFIG,write,owner_plan

def interval(values):
    rng=random.Random(CONFIG['bootstrap_seed']); n=len(values)
    samples=sorted(sum(rng.choices(values,k=n))/n for _ in range(CONFIG['bootstrap_replicates']))
    def percentile(p):
        pos=(len(samples)-1)*p; low=int(pos)
        return samples[low]+(samples[min(low+1,len(samples)-1)]-samples[low])*(pos-low)
    return {'mean':sum(values)/n,'ci95':[percentile(.025),percentile(.975)],'positive':sum(x>0 for x in values),'negative':sum(x<0 for x in values),'ties':sum(x==0 for x in values),'pairs':n,'replicates':CONFIG['bootstrap_replicates'],'bootstrap_seed':CONFIG['bootstrap_seed'],'percentile_interpolation':'linear'}

def main():
    store=Path(sys.argv[1]); arms=('SxT','SxB','LxT','LxB','Expert'); rows=[]
    for seed in range(441001,441129):
        for arm in arms:
            row=json.loads((store/'fresh'/f'{seed}-{arm}.json').read_text())
            if row['seed']!=seed or row['arm']!=arm or not row['terminal'] or row['problems'] or row['outcome'] not in ('PLAYER_WIN','PLAYER_LOSS'): raise ValueError('INCOMPLETE_FRESH')
            rows.append(row)
    bykey={(r['seed'],r['arm']):r for r in rows}; primary=interval([bykey[(s,'LxB')]['floor']-bykey[(s,'SxT')]['floor'] for s in range(441001,441129)])
    summaries={}
    for arm in arms:
        selected=[r for r in rows if r['arm']==arm]; counts=Counter(); uses=Counter(); screens=Counter()
        for row in selected: counts.update(row['metrics']); uses.update(row['learner_uses']); screens.update(row['screens'])
        summaries[arm]={'runs':128,'mean_floor':sum(r['floor'] for r in selected)/128,'floor_sum':sum(r['floor'] for r in selected),'outcomes':dict(Counter(r['outcome'] for r in selected)),'loss_types':dict(Counter(str(r['loss_type']) for r in selected)),'loss_encounters':dict(Counter(str(r['loss_encounter']) for r in selected)),'act2':sum(r['max_act']>=2 for r in selected),'act3':sum(r['max_act']>=3 for r in selected),'act4':sum(r['max_act']>=4 for r in selected),'heart_reached':sum(r['heart_reached'] for r in selected),'heart_wins':sum(r['heart_win'] for r in selected),'final_hp_sum':sum(r['final_hp'] for r in selected),'metrics':dict(counts),'learner_uses':dict(uses),'screen_occupancy':dict(screens),'potion_offer_option_occurrences':sum(r['potion_offer_option_occurrences'] for r in selected),'worker_cpu':sum(r['cpu'] for r in selected),'search_cpu':sum(r['search_cpu'] for r in selected),'search_calls':sum(r['search_calls'] for r in selected)}
    for seed in range(441001,441129):
        if any(bykey[(seed,arm)]['initial_raw']!=bykey[(seed,'Expert')]['initial_raw'] for arm in arms): raise ValueError('PAIRED_INITIAL_STATE_MISMATCH')
    coverage=summaries['LxB']['learner_uses']
    if coverage.get('MAP_SCREEN',0)<64 or coverage.get('REWARDS',0)<64: classification='PUBLIC_INPUT_INVALID'
    elif primary['ci95'][0]>0: classification='SOURCE_AND_CAPACITY_RUN_GAIN_ESTABLISHED'
    elif primary['ci95'][1]<0: classification='SOURCE_AND_CAPACITY_RUN_HARM_CONFIRMED'
    else: classification='IMPROVEMENT_NOT_ESTABLISHED'
    secondary={}
    for lhs,rhs in [('SxB','SxT'),('LxT','SxT'),('LxB','LxT'),('LxB','SxB'),('SxT','Expert'),('SxB','Expert'),('LxT','Expert'),('LxB','Expert')]:
        secondary[lhs+'-'+rhs]={'exploratory_interval':True,**interval([bykey[(s,lhs)]['floor']-bykey[(s,rhs)]['floor'] for s in range(441001,441129)])}
    roots=[json.loads((store/str(p['owner'])/'root.json').read_text()) for p in owner_plan()]
    result={'classification':classification,'purpose':'STUDY_ONLY','primary':primary,'secondary':secondary,'arms':summaries,'source_roots':{split:dict(Counter(f"act{r['act']}/floor{r['floor']}/{r['family']}/assisted={r['assisted']}" for r in roots if r['split']==split)) for split in ('train','validation','heldout')},'same_initial_states_all128':True,'supported_silent_fallbacks':0,'rows':[{k:v for k,v in r.items() if k not in ('initial_raw','final_raw')} for r in rows],'limitations':['Teacher-assisted privileged Battle, not normal-public/noSL evaluation.','Nested source scale under one frozen encoder, not independent-dataset or assistance-only causal inference.','Exploratory secondary intervals do not select checkpoints or a winner.','Partial data cannot establish gain/harm.']}
    write(store/'report.json',result)

if __name__=='__main__': main()
