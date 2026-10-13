"""Descriptive exposure from existing512 tables, after checkpoint freeze."""
import itertools,json,sys,time
from pathlib import Path
from collections import Counter,defaultdict
from common import write,sha,owner_plan
from student import treatment,features as old_features
from binding_student import features as new_features

def main():
    start=time.process_time(); store=Path(sys.argv[1]); assert (store/'training/checkpoints.json').exists()
    cooccurrence={s:defaultdict(set) for s in ('train','validation','heldout')}; pairs={s:defaultdict(set) for s in cooccurrence}; unequal={s:defaultdict(set) for s in cooccurrence}; alias=defaultdict(set); examples=defaultdict(list); changed=Counter(); roots=[]
    for plan in owner_plan():
        table=json.loads((store/'inputs'/f'{plan["owner"]}.json').read_text()); root=table['root']; value=treatment(root['serialized'],'visible'); cards=value['public_state']['persistent_resources']['deck'].get('value',[]); split=root['split']; owner=root['owner']
        tuples=sorted(set((c['id'],c['upgraded'],c['upgrade_count'],c['misc']) for c in cards)); has_unequal=len(set(table['q']))>1
        for item in tuples: cooccurrence[split][json.dumps(item)].add(owner)
        for a,b in itertools.combinations(tuples,2):
            if a[0]==b[0]: continue
            key=json.dumps([a,b]); pairs[split][key].add(owner)
            if has_unequal: unequal[split][key].add(owner)
        differences=[]
        for i in range(len(table['q'])):
            old=old_features(value,i); new=new_features(value,i); differences.append(max(abs(a-b) for a,b in zip(old,new)))
            semantic={'state':value['public_state'],'option':value['legal_options'][i],'legal_count':len(value['legal_options']),'ordinal':i}; identity=sha(semantic); vector=tuple(old)
            alias[vector].add(identity); examples[vector].append([owner,i,identity]); changed[split]+=old!=new
        roots.append({'owner':owner,'split':split,'family':root['family'],'floor':root['floor'],'assisted':root['assisted'],'distinct_card_tuples':len(tuples),'upgraded_card_count':sum(c['upgraded'] for c in cards),'unequal_Q_supervision':has_unequal,'Q_range':max(table['q'])-min(table['q']),'old_new_max_feature_difference':max(differences),'changed_candidates':sum(x>0 for x in differences)})
    selected=sorted(pairs['train'],key=lambda k:(-len(pairs['train'][k]),k))[:30]
    clusters=[{'distinct_semantic_inputs':len(ids),'occurrences':examples[v]} for v,ids in alias.items() if len(ids)>1]
    write(store/'diagnostics.json',{'purpose':'STUDY_ONLY_DESCRIPTIVE','opened_after_checkpoint_freeze':True,'identity_upgrade_cooccurrences':{s:[{'tuple':json.loads(k),'owners':sorted(v),'owner_count':len(v)} for k,v in sorted(cooccurrence[s].items())] for s in cooccurrence},'common_pairs_selected_by_training_owner_frequency':[{'pair':json.loads(k),'splits':{s:{'owners':sorted(pairs[s].get(k,set())),'owner_count':len(pairs[s].get(k,set())),'owners_with_available_unequal_Q':sorted(unequal[s].get(k,set()))} for s in pairs}} for k in selected],'exact_observed_old_feature_alias_clusters':clusters,'alias_cluster_count':len(clusters),'changed_candidates_by_split':dict(changed),'roots':roots,'limitations':['No new labels or native roots.','Unequal-Q is available within the root choice table, not a causal label for the deck pair.','Zero empirical aliases does not negate constructive witness.','Root assistance provenance is descriptive only and excluded from features.'],'cpu':time.process_time()-start})
if __name__=='__main__': main()
