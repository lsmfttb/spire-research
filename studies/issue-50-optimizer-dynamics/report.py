"""Prespecified owner-paired retrospective bootstrap; no checkpoint selection."""
import json,sys,time,random,math
from pathlib import Path
from collections import defaultdict
from freeze import write
def mean(xs):return sum(xs)/len(xs)
def percentile(xs,p):
    ys=sorted(xs);at=(len(ys)-1)*p;low=int(at);return ys[low]+(ys[min(low+1,len(ys)-1)]-ys[low])*(at-low)
def compact(rs):
    return {'roots':len(rs),'mean_regret':mean([r['regret'] for r in rs]),'initial_mean_regret':mean([r['initial_regret'] for r in rs]),'choice_changes':sum(r['choice_changed'] for r in rs),'mean_margin':mean([r['margin'] for r in rs]),'initial_mean_margin':mean([r['initial_margin'] for r in rs]),'mean_common_score_shift':mean([r['common_score_shift'] for r in rs]),'mean_centered_score_motion_rms':mean([r['centered_score_motion_rms'] for r in rs]),'informative_roots':sum(r['positive_pairs']>0 for r in rs),'positive_pairs':sum(r['positive_pairs'] for r in rs),'q_optimal_tie_roots':sum(r['optimal_q_ties']>1 for r in rs)}
def main():
    start=time.process_time();store=Path(sys.argv[1]);study=Path(__file__).parent;ledger=json.loads((store/'execution.json').read_text());assert ledger['stage']=='FOUR_FITS_COMPLETE';data=json.loads((store/'functional.json').read_text())
    summaries={}
    for key,epochs in data.items():
        summaries[key]={}
        for epoch,sets in epochs.items():
            summaries[key][epoch]={}
            for split,result in sets.items():
                cells=defaultdict(list)
                for r in result['roots']:cells[('assisted' if r['assisted'] else 'natural')+'/'+r['family']].append(r)
                summaries[key][epoch][split]={'all':{**compact(result['roots']),'pairwise_logistic_loss':result['pairwise_logistic_loss']},'cells':{cell:compact(rs) for cell,rs in sorted(cells.items())}}
    def pairs(split):
        maps={k:{r['owner']:r for r in epochs['epoch100'][split]['roots']} for k,epochs in data.items()}
        rows=[]
        for owner,r in maps['S-446101'].items():
            d=mean([maps[f'D-{s}'][owner]['regret'] for s in (446101,446102)]);c=mean([maps[f'S-{s}'][owner]['regret'] for s in (446101,446102)])
            rows.append({'owner':owner,'cell':('assisted' if r['assisted'] else 'natural')+'/'+r['family'],'D':d,'S':c,'D_minus_S':d-c,'D_seed_regrets':[maps[f'D-{s}'][owner]['regret'] for s in (446101,446102)],'S_seed_regrets':[maps[f'S-{s}'][owner]['regret'] for s in (446101,446102)]})
        return sorted(rows,key=lambda r:r['owner'])
    rows=pairs('diagnostic');cells=defaultdict(list)
    for r in rows:cells[r['cell']].append(r['D_minus_S'])
    assert len(rows)==64 and sorted(map(len,cells.values()))==[16]*4
    rng=random.Random(501999)
    def interval(groups):
        samples=[mean([mean([rng.choice(g) for _ in g]) for g in groups]) for _ in range(5000)]
        return [percentile(samples,.025),percentile(samples,.975)]
    primary={'mean_D_minus_S':mean([r['D_minus_S'] for r in rows]),'95pct_linear_percentile_CI':interval([vs for k,vs in sorted(cells.items())]),'D_lower_regret':sum(r['D_minus_S']<0 for r in rows),'D_higher_regret':sum(r['D_minus_S']>0 for r in rows),'ties':sum(r['D_minus_S']==0 for r in rows),'owners':64,'replicates':5000,'seed':501999,'rng':'Python random.Random','stratified_equal_four_cells':True,'interpretation':'Retrospective within-diagnostic empirical Q comparison; not independent deployment/game-strength evidence'}
    cell_intervals={cell:{'mean_D_minus_S':mean(vs),'95pct_linear_percentile_CI':interval([vs])} for cell,vs in sorted(cells.items())}
    seed_specific={}
    for seed in (446101,446102):
        ds={r['owner']:r['regret'] for r in data[f'D-{seed}']['epoch100']['diagnostic']['roots']};ss={r['owner']:r['regret'] for r in data[f'S-{seed}']['epoch100']['diagnostic']['roots']}
        groups=[[ds[r['owner']]-ss[r['owner']] for r in rows if r['cell']==cell] for cell in sorted(cells)]
        seed_specific[str(seed)]={'mean_D_minus_S':mean([x for g in groups for x in g]),'95pct_linear_percentile_CI':interval(groups)}
    trainrows=pairs('train');report={'primary':primary,'diagnostic_cells':cell_intervals,'seed_specific':seed_specific,'summaries':summaries,'training_mean_D_minus_S':mean([r['D_minus_S'] for r in trainrows]),'diagnostic_minus_training_D_minus_S_gap':primary['mean_D_minus_S']-mean([r['D_minus_S'] for r in trainrows]),'no_A20_strength_classification':True,'no_model_selection_or_post_result_variant':True,'missing_public_context_frozen':True,'historically_exposed_labels':True,'label_noise':'Two Expert continuations share native root future RNG; empirical Q ties and noise are not independent future-world evaluations'}
    write(store/'report.json',report);write(store/'paired-diagnostic.json',rows);write(store/'paired-training.json',trainrows)
    lines=['# Issue 50 optimizer-only retrospective diagnostic','','Study only. SGD0.01 versus Adam0.001 is the sole frozen intervention; exact #47 public features, 2,081-parameter network, initialization and pairwise objective. Two seeds, exactly 100 full-batch epochs each. No checkpoint selection, fresh native games, new owners or new Q labels.','',
        '## Primary diagnostic','','D−S is negative when Adam has lower empirical Q regret. Each owner averages the two seed-specific regrets; seeds are not additional owners.','',f"Mean D−S: **{primary['mean_D_minus_S']:.8g} floors**; stratified owner-paired 95% linear percentile interval **{primary['95pct_linear_percentile_CI']}**. Adam lower/higher/tied regret: **{primary['D_lower_regret']}/{primary['D_higher_regret']}/{primary['ties']}** of 64 owners.",'',
        '| Diagnostic cell | D−S floors | 95% within-cell empirical interval |','|---|---|---|']
    for cell,r in cell_intervals.items():lines.append(f"| {cell} | {r['mean_D_minus_S']:.8g} | {r['95pct_linear_percentile_CI']} |")
    lines+=['','## Initialization versus epoch 100','','| Arm / seed | Split | Initial → final regret | Choice changes | Initial → final margin | Common shift / centered motion RMS |','|---|---|---|---|---|---|']
    for key,s in summaries.items():
        for split,r0 in s['epoch100'].items():
            r=r0['all'];lines.append(f"| {key} | {split} | {r['initial_mean_regret']:.8g} → {r['mean_regret']:.8g} | {r['choice_changes']}/{r['roots']} | {r['initial_mean_margin']:.8g} → {r['mean_margin']:.8g} | {r['mean_common_score_shift']:.8g} / {r['mean_centered_score_motion_rms']:.8g} |")
    lines+=['',f"Training mean D−S: {report['training_mean_D_minus_S']:.8g}; diagnostic minus training contrast gap: {report['diagnostic_minus_training_D_minus_S_gap']:.8g}.",'',
        '## Limits and disposition','','Both 320 training and 64 diagnostic roots were historical #47 training data and retrospectively exposed. The balanced diagnostic partition is not a pristine test. Its intervals summarize these empirical noisy Q labels; they are not confidence intervals for normal A20 game outcomes. Separate seed robustness and all four source/family cells are reported without hiding natural MAP.','',
        'Function-space gate evidence consists of choice changes and within-root margin/motion, diagnostic regret versus initialization/control, and the natural MAP cell. This report does not automatically authorize a full policy test. Any null, ambiguous or harmful result is complete; no optimizer/loss/encoder variants follow from the outcome.','',
        'Old hash encoder limitations, missing joint reward-choice context and native reward misc/exact upgrade count remain unresolved. This optimizer contrast cannot adjudicate a fully public structured non-combat agent. Return independently reviewed evidence to Planner; no core merge or promotion.','',
        '## Reproduction and integrity','','Original #47 student.py, train.py (local train47.py), common.py, adapter.py (local adapter47.py), and config are byte-pinned. freeze.py seals owner metadata before reading Q. Source hashes, epoch0/100 states, all 400 training records, full per-root scores/Q/choices, optimizer manual-moment checks and CPU boundaries are retained for independent review. Run under Python 3.13 / Torch2.12.0+cpu, float64, one thread; no simulator import or constructor is invoked.','']
    (study/'REPORT.md').write_text('\n'.join(lines));ledger['stage']='REPORT_COMPLETE';ledger['measured_stage_cpu_seconds']['report']=time.process_time()-start;write(store/'execution.json',ledger);print(json.dumps(primary))
if __name__=='__main__':main()
