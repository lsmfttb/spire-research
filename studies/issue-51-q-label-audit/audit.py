"""Issue51: read original frozen receipts only; no simulator or learning imports."""
import hashlib
import itertools
import json
import math
from pathlib import Path
import statistics
import time

S46 = Path('/home/lsmft/.cache/issue46-source-capacity')
S47 = Path('/home/lsmft/.cache/issue47-action-preference')
OUT = Path('/home/lsmft/.cache/issue51-q-label-audit')
DRIVERS = (446001, 446002)
SEALS = {'46': '9807c5a02554f57947db1d507d0ed49b10fa07fab88042976b44e60a879e0cd1',
         '47': '119a514692836954ab205284c35d1070a9a089cebcc9344fd27ded2c850ff1de'}

def digest_bytes(b): return hashlib.sha256(b).hexdigest()
def digest(v): return digest_bytes(json.dumps(v, sort_keys=True, separators=(',', ':'), allow_nan=False).encode())
def load(p): return json.loads(Path(p).read_text())
def write(p, v):
    p = Path(p); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp'); tmp.write_text(json.dumps(v, sort_keys=True, indent=2, allow_nan=False)+'\n'); tmp.replace(p)
def mean(v): return statistics.mean(v) if v else None
def sign(v): return (v > 0)-(v < 0)
def midranks(v): return [1+sum(x > y for x in v)+sum(x == y for x in v)/2-.5 for y in v]
def corr(x,y):
    a=mean(x); b=mean(y); vx=sum((v-a)**2 for v in x); vy=sum((v-b)**2 for v in y)
    return sum((u-a)*(v-b) for u,v in zip(x,y))/math.sqrt(vx*vy) if vx*vy else None

def metrics(floors):
    f1=[v[0] for v in floors]; f2=[v[1] for v in floors]; q=[sum(v)/2 for v in floors]
    pairs=[]; counts=dict(concordant=0, reversal=0, one_driver_tie=0, both_driver_tie=0)
    for i,j in itertools.combinations(range(len(q)),2):
        a=f1[i]-f1[j]; b=f2[i]-f2[j]; c=q[i]-q[j]
        kind='both_driver_tie' if a==b==0 else 'one_driver_tie' if a==0 or b==0 else 'concordant' if a*b>0 else 'reversal'
        counts[kind]+=1
        pairs.append({'i':i,'j':j,'driver1_gap':a,'driver2_gap':b,'mean_q_gap':c,'signs':[sign(a),sign(b),sign(c)],'kind':kind,'concealed_opposition':c==0 and a*b<0})
    top1=[i for i,x in enumerate(f1) if x==max(f1)]; top2=[i for i,x in enumerate(f2) if x==max(f2)]
    union=set(top1)|set(top2); overlap=set(top1)&set(top2)
    m=mean(q); center=[x-m for x in q]
    # Kendall tau-b treats driver-specific ties separately; undefined if either constant.
    den=math.sqrt(sum(p['driver1_gap']!=0 for p in pairs)*sum(p['driver2_gap']!=0 for p in pairs))
    return {'q':q,'floors':floors,'pairs':pairs,'pair_counts':counts,'pair_count':len(pairs),
      'all_mean_q_tied':len(set(q))==1,'both_drivers_all_action_tied':len(set(f1))==len(set(f2))==1,
      'all_option_driver_floors_identical':len(set(f1+f2))==1,
      'mean_q_tied_but_driver_action_variation':len(set(q))==1 and (len(set(f1))>1 or len(set(f2))>1),
      'ranking_eligible':len(q)>1,'mean_q_informative':len(set(q))>1,'driver_ranking_informative':len(set(f1))>1 or len(set(f2))>1,
      'root_mean_floor':m,'q_over_60':[x/60 for x in q],'action_centered_q':center,
      'within_root_variance':mean([x*x for x in center]),'q_spread':max(q)-min(q),
      'mean_abs_driver_disagreement':mean([abs(a-b) for a,b in floors]),
      'mean_abs_pair_gap':mean([abs(p['mean_q_gap']) for p in pairs]),
      'concealed_opposition_pairs':sum(p['concealed_opposition'] for p in pairs),
      'mean_q_equal_pairs':sum(p['mean_q_gap']==0 for p in pairs),
      'top1':top1,'top2':top2,'top_sets_equal':top1==top2,'top_sets_disjoint':not overlap,'top_jaccard':len(overlap)/len(union),
      'midranks1':midranks(f1),'midranks2':midranks(f2),'spearman_midrank':corr(midranks(f1),midranks(f2)),
      'kendall_tau_b':(counts['concordant']-counts['reversal'])/den if den else None}

def summarize(rows):
    eligible=[r for r in rows if r['ranking_eligible']]; informative=[r for r in rows if r['mean_q_informative']]
    driver_info=[r for r in eligible if r['driver_ranking_informative']]
    mus=[r['root_mean_floor'] for r in rows]; grand=mean(mus); within=mean([r['within_root_variance'] for r in rows])
    between=mean([(x-grand)**2 for x in mus]) if rows else None
    energy=mean([x*x for x in mus]); total=energy+within if rows else None
    result={'owners':len(rows),'ranking_eligible_owners':len(eligible),'single_action_owners':len(rows)-len(eligible),'mean_q_informative_owners':len(informative),
      'all_mean_q_tied_owners':sum(r['all_mean_q_tied'] for r in rows),
      'both_drivers_all_action_tied_owners':sum(r['both_drivers_all_action_tied'] for r in rows),
      'all_option_driver_floors_identical_owners':sum(r['all_option_driver_floors_identical'] for r in rows),
      'either_driver_informative_owners':len(driver_info),
      'mean_q_tied_but_driver_action_variation_owners':sum(r['mean_q_tied_but_driver_action_variation'] for r in rows),
      'owners_with_reversal':sum(r['pair_counts']['reversal']>0 for r in eligible),
      'owners_with_concealed_opposition':sum(r['concealed_opposition_pairs']>0 for r in eligible),
      'root_weighted_pair_rates':{k:mean([r['pair_counts'][k]/r['pair_count'] for r in eligible]) for k in ('concordant','reversal','one_driver_tie','both_driver_tie')},
      'root_weighted_concealed_opposition_rate':mean([r['concealed_opposition_pairs']/r['pair_count'] for r in eligible]),
      'root_weighted_mean_q_equal_pair_rate':mean([r['mean_q_equal_pairs']/r['pair_count'] for r in eligible]),
      'raw_pair_counts_descriptive_only':{k:sum(r['pair_counts'][k] for r in rows) for k in ('concordant','reversal','one_driver_tie','both_driver_tie')},
      'raw_pairs_descriptive_only':sum(r['pair_count'] for r in rows),
      'top_sets_equal_owners':sum(r['top_sets_equal'] for r in eligible),'top_sets_disjoint_owners':sum(r['top_sets_disjoint'] for r in eligible),
      'mean_top_jaccard_eligible':mean([r['top_jaccard'] for r in eligible]),
      'mean_top_jaccard_mean_q_informative':mean([r['top_jaccard'] for r in informative]),
      'mean_spearman_defined':mean([r['spearman_midrank'] for r in eligible if r['spearman_midrank'] is not None]),
      'spearman_defined_owners':sum(r['spearman_midrank'] is not None for r in eligible),
      'mean_kendall_tau_b_defined':mean([r['kendall_tau_b'] for r in eligible if r['kendall_tau_b'] is not None]),
      'kendall_defined_owners':sum(r['kendall_tau_b'] is not None for r in eligible),
      'mean_root_floor':grand,'mean_q_over60':grand/60 if rows else None,'mean_within_root_variance':within,'between_root_variance':between,
      'global_centered_label_variance':between+within if rows else None,
      'between_fraction_global_centered_variance':between/(between+within) if rows and between+within else None,
      'root_common_squared_energy':energy,'total_label_squared_energy':total,'common_fraction_zero_prediction_mse':energy/total if total else None,
      'mean_q_spread':mean([r['q_spread'] for r in rows]),'mean_abs_driver_disagreement':mean([r['mean_abs_driver_disagreement'] for r in rows]),
      'mean_abs_pair_gap_eligible':mean([r['mean_abs_pair_gap'] for r in eligible])}
    result['informative_root_pair_rates']={k:mean([r['pair_counts'][k]/r['pair_count'] for r in informative]) for k in ('concordant','reversal','one_driver_tie','both_driver_tie')}
    result['either_driver_informative_pair_rates']={k:mean([r['pair_counts'][k]/r['pair_count'] for r in driver_info]) for k in ('concordant','reversal','one_driver_tie','both_driver_tie')}
    return result

def unit_checks():
    m=metrics([[10,12],[12,10]]); assert m['concealed_opposition_pairs']==1 and m['all_mean_q_tied'] and not m['both_drivers_all_action_tied']
    assert m['kendall_tau_b']==-1 and m['spearman_midrank']==-1
    m=metrics([[10,11],[10,12]]); assert m['pair_counts']['one_driver_tie']==1 and m['spearman_midrank'] is None
    m=metrics([[10,12],[10,12]]); assert m['both_drivers_all_action_tied'] and m['pair_counts']['both_driver_tie']==1
    assert not metrics([[10,12]])['ranking_eligible']; assert midranks([9,9,8])==[1.5,1.5,3.0]
    r=[metrics([[1,3],[3,5]]),metrics([[6,8],[6,8]])]; s=summarize(r)
    direct=mean([mean([q*q for q in x['q']]) for x in r]); assert abs(direct-s['total_label_squared_energy'])<1e-12
    return {'passed':True,'tests':['opposed_mean_tie','one_driver_tie_undefined_correlation','exact_action_ties','single_action_exclusion','descending_midranks','equal_root_energy_decomposition']}

def main():
    start=time.process_time(); ledger=load(OUT/'execution.json'); ledger['stage']='RECEIPTS_AND_RANKS'; write(OUT/'execution.json',ledger)
    seals={}; inputs=[]
    for k,p in [('46',S46),('47',S47)]:
        b=(p/'artifact-manifest.json').read_bytes(); assert digest_bytes(b)==SEALS[k]
        seals[k]=load(p/'artifact-manifest.json')
    indices={k:{v['path']:v for v in m['files']} for k,m in seals.items()}
    def verified(p,k,rel):
        b=(p/rel).read_bytes(); h=digest_bytes(b); assert h==indices[k][rel]['sha256'],(k,rel,'seal mismatch')
        inputs.append({'source':k,'path':rel,'sha256':h,'bytes':len(b)})
        return json.loads(b)
    source=verified(S47,'47','source.json'); lineage46=verified(S46,'46','source.json')
    owners=source['derived_files']; assert len(owners)==512 and len({x['owner'] for x in owners})==512
    rows=[]; expected=0; errors=[]; example_kinds={}
    for meta in sorted(owners,key=lambda x:x['owner']):
        owner=meta['owner']; prefix=str(owner)+'/'; r=verified(S46,'46',prefix+'root.json'); t=verified(S46,'46',meta['source_path']); d=verified(S47,'47','inputs/'+meta['input_path'])
        assert r==t['root']; assert digest({k:v for k,v in r.items() if k!='root_id'})==r['root_id']
        assert t['q']==d['q']; assert digest_bytes((S46/meta['source_path']).read_bytes())==meta['source_table_sha256']
        assert digest_bytes((S47/'inputs'/meta['input_path']).read_bytes())==meta['derived_sha256']
        assert (r['owner'],r['split'],r['assisted'],r['old45'])==(owner,meta['split'],meta['assisted'],meta['old45'])
        for key in ('root_id','serialized','public_sha256','family','act','floor','legal_count'):
            assert d['root'][key]==r[key]
        public=json.loads(r['serialized']); options=public['legal_options']; assert digest(public)==r['public_sha256']
        assert len(options)==r['legal_count']; assert [x['candidate_index'] for x in options]==list(range(len(options)))
        assert [digest(x) for x in options]==r['candidate_sha256']
        fs=[]; label_meta=[]; expected+=2*len(options)
        allowed_names={f'label-{i}-{driver}.{suffix}' for i in range(len(options)) for driver in DRIVERS for suffix in ('json','jsonl.gz')}
        actual_names={p.name for p in (S46/str(owner)).glob('label-*')}; assert actual_names==allowed_names,(owner,'missing/orphan labels')
        for i in range(len(options)):
            floors=[]
            for driver in DRIVERS:
                rel=prefix+f'label-{i}-{driver}.json'; label=verified(S46,'46',rel)
                assert (label['owner'],label['root_id'],label['ordinal'],label['driver_seed'])==(owner,r['root_id'],i,driver)
                assert label['forced_action_identity']==r['candidate_sha256'][i]
                assert label['forced_pre_raw']==r['root_raw']; assert label['initial_raw']==label['forced_post_raw']
                assert label['terminal'] and not label['problems'] and not label['new_assistance']
                assert label['final_raw']['floor_num']==label['terminal_floor']; assert label['final_raw']['outcome']==label['outcome']
                assert label['source']==lineage46 and label['producer']==lineage46['producer']
                floors.append(label['terminal_floor']); label_meta.append({'ordinal':i,'driver':driver,'floor':label['terminal_floor'],'act':label['max_act'],'outcome':label['outcome'],'heart_win':label['heart_win'],'metrics':label['metrics']})
            assert sum(floors)/2==t['q'][i]; fs.append(floors)
        m=metrics(fs); row={k:r[k] for k in ('owner','root_id','split','assisted','old45','family','act','floor','legal_count','candidate_sha256')}
        row.update(m); row['actions']=options; row['label_outcomes']=label_meta
        row['root_potion_count']=r['root_raw']['potion_count']; row['root_hp']=r['root_raw']['cur_hp']; row['root_gold']=r['root_raw']['gold']
        row['potion_action_present']=any('potion' in x['action']['kind'] for x in options)
        rows.append(row)
        for typ,match in [('reversal',any(p['kind']=='reversal' for p in m['pairs'])),('concealed_mean_tie',bool(m['concealed_opposition_pairs'])),('exact_both_driver_tie',m['both_drivers_all_action_tied'] and m['ranking_eligible'])]:
            if match and typ not in example_kinds: example_kinds[typ]=row
    assert len({r['root_id'] for r in rows})==512
    train=[r for r in rows if r['split']=='train']; assert len(train)==384 and sum(r['all_mean_q_tied'] for r in train)==181
    groups={}
    for r in rows:
        cell=f"{r['split']}/{'assisted' if r['assisted'] else 'natural'}/{r['family']}"
        keys=[cell,f"{r['split']}/origin/{'old45' if r['old45'] else 'fresh46'}",f"{r['split']}/floor/{r['floor']}",f"{r['split']}/act/{r['act']}",f"{r['split']}/potions-held/{r['root_potion_count']}",f"{r['split']}/potion-action/{r['potion_action_present']}"]
        for key in keys: groups.setdefault(key,[]).append(r)
    categories={}
    for r in rows:
        for kind in sorted({x['action']['kind'] for x in r['actions']}):
            ords=[i for i,x in enumerate(r['actions']) if x['action']['kind']==kind]
            categories.setdefault(r['split']+'/'+kind,[]).append({'owner':r['owner'],'mean_centered_q':mean([r['action_centered_q'][i] for i in ords]),'mean_q':mean([r['q'][i] for i in ords]),'mean_abs_driver_gap':mean([abs(r['floors'][i][0]-r['floors'][i][1]) for i in ords]),'actions':len(ords)})
    report={'purpose':'STUDY_ONLY','weighting':'Equal owners; action means within root; pair fractions within ranking-eligible root then equal-owner mean. Raw pair counts only descriptive; no independent-pair intervals.',
      'availability':{'owners':512,'train':384,'validation':64,'heldout':64,'labels_expected_verified':expected,'traces_expected':expected,'missing_or_orphan_label_records':0},
      'split':{s:summarize([r for r in rows if r['split']==s]) for s in ('train','validation','heldout')},
      'strata':{k:summarize(v) for k,v in sorted(groups.items())},
      'categories_root_weighted':{k:{'owners_with_category':len(v),'actions':sum(x['actions'] for x in v),'mean_centered_q':mean([x['mean_centered_q'] for x in v]),'mean_q':mean([x['mean_q'] for x in v]),'mean_abs_driver_gap':mean([x['mean_abs_driver_gap'] for x in v])} for k,v in sorted(categories.items())},
      'original_val_heldout_missing_cells':['natural/MAP_SCREEN','assisted/REWARDS'],
      'interpretation_limits':['Two fixed Expert seeds share restored Native root RNG; not independent hidden worlds.','Only descriptive retrospective evidence, no strategic indifference or true optimal-action claim.','Sampling variation, source confounding and continuation-policy mismatch are separate.','More Expert samples cannot by itself change Q under Expert into Q under repeated learned policy.']}
    write(OUT/'owner_metrics.json',rows); write(OUT/'ranking_examples.json',example_kinds); write(OUT/'rank_report.json',report)
    write(OUT/'input_receipts.json',{'seals':SEALS,'inputs':inputs,'source46':lineage46,'source47_metadata_sha256':digest_bytes((S47/'source.json').read_bytes()),'original_retention':{'46':'2026-11-12','47':'2026-11-13'}})
    write(OUT/'verification.json',unit_checks())
    cpu=time.process_time()-start; ledger['charges']['receipt_rank_cpu']=cpu; ledger['stage']='RANKS_COMPLETE'; write(OUT/'execution.json',ledger)
    print(json.dumps({'owners':len(rows),'labels':expected,'cpu':cpu,'train':report['split']['train']},sort_keys=True))

if __name__=='__main__': main()
