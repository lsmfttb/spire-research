"""Honest public-format synthetic witnesses, no generated game or Q labels."""
import copy,json,time,hashlib,sys
from pathlib import Path
import prototype,student47
def card(identity,upgraded=False,misc=0):return {'id':identity,'upgraded':upgraded,'misc':misc}
def action(kind,sem,g=0,c=0):return {'candidate_index':0,'action':{'scope':'game','kind':kind,'idx1':g,'idx2':c,'idx3':0,'label':'unused'},'semantics':sem}
def fixture():
    options=[action('reward_card',{'choice_type':'card','card':card(1)},0,0),action('reward_card',{'choice_type':'card','card':card(2)},0,1),action('reward_card',{'choice_type':'card','card':card(3)},1,0),action('reward_card',{'choice_type':'card','card':card(4)},1,1),action('reward_potion',{'choice_type':'potion','potion':{'id':5}},0),action('game_potion_discard',{'choice_type':'potion_discard','potion':{'id':6,'slot':0}}),action('skip',{'choice_type':'skip'})]
    for i,o in enumerate(options):o['candidate_index']=i
    return {'availability':{'screen_payload':'available'},'public_state':{'deck':[card(10),card(11,True)],'potions':[{'id':6,'slot':0},{'id':7,'slot':1}],'potion_capacity':2,'map_graph':[{'x':0,'y':0,'room_symbol':'M','outgoing_edges':[{'x':1,'y':1}]}]},'legal_options':options}
def old(p,i):return student47.features(student47.treatment(json.dumps(p),'visible'),i)
def main():
    start=time.process_time();p=fixture();checks={}
    for kind,target,changed in [('other-group-card',0,2),('potion-offer',5,4)]:
        q=copy.deepcopy(p)
        if changed==2:q['legal_options'][changed]['semantics']['card']['id']=33
        else:q['legal_options'][changed]['semantics']['potion']['id']=55
        assert old(p,target)==old(q,target);assert prototype.features(p,target)!=prototype.features(q,target)
        checks[kind]={'old_max_diff':0,'v2_distinct':True,'target_ordinal_and_legal_count_fixed':True}
    q=copy.deepcopy(p);q['public_state']['deck'][0]['upgraded']=True;q['public_state']['deck'][1]['upgraded']=False
    assert old(p,0)==old(q,0);assert prototype.features(p,0)!=prototype.features(q,0);checks['deck-binding']=True
    q=copy.deepcopy(p);q['legal_options'][1]['action']['idx1']=1;q['legal_options'][1]['action']['idx2']=1;q['legal_options'][3]['action']['idx1']=0;q['legal_options'][3]['action']['idx2']=1
    assert prototype.features(p,5)!=prototype.features(q,5);checks['same-card-multiset-different-group']=True
    q=copy.deepcopy(p);order=[3,2,1,0,6,5,4];q['legal_options']=[q['legal_options'][i] for i in order]
    for i,o in enumerate(q['legal_options']):
        o['candidate_index']=i
        if o['action']['kind']=='reward_card':o['action']['idx1']=9-o['action']['idx1'];o['action']['idx2']=1-o['action']['idx2']
    for i,j in enumerate(order):assert prototype.features(q,i)==prototype.features(p,j)
    checks['group-card-reorder-equivariant']=True
    q=copy.deepcopy(p);q['legal_options'].insert(1,copy.deepcopy(q['legal_options'][0]));q['legal_options'][1]['action']['idx2']=2
    assert sorted(map(len,prototype.normalize(q)['facts']['card_reward_groups']))==[2,3]
    assert prototype.normalize(p)['facts']!=prototype.normalize(q)['facts'];checks['duplicates-multiplicity']=True
    q=copy.deepcopy(p);q['hidden_rng']='never admitted';q['ungenerated_future_reward']={'id':999}
    assert prototype.features(p,5)==prototype.features(q,5);checks['hidden-top-level-input-excluded']=True
    # This is an admission-boundary witness: it does not establish that an
    # arbitrary malicious nested public_state object would be truly public.
    candidates=[o['action'] for o in p['legal_options']]
    assert prototype.mapped_action(p,0,candidates,current_public=p)==candidates[0]
    try:prototype.mapped_action(p,0,candidates[1:],current_public=p)
    except ValueError:checks['stale-mapping-rejected']=True
    else:raise AssertionError('stale accepted')
    f=prototype.normalize(p);assert len(f['facts']['card_reward_groups'])==2
    checks['discard-complete-card-and-offer-context']=len(f['facts']['card_reward_groups'])==2 and any(c['kind']=='reward_potion' for c in f['facts']['legal_candidates'])
    receipt={'passed':True,'fixtures':'synthetic public-format; reward misc deliberately synthetic since pinned native reward projection does not emit it','checks':checks,'cpu_seconds':time.process_time()-start,'fresh_games':0,'training_steps':0,'feature_kind':'exact semantic/candidate-cross tokens, no learned weights','source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True);(out/'prototype-verification.json').write_text(json.dumps(receipt,sort_keys=True,indent=2)+'\n');(out/'synthetic-public-fixture.json').write_text(json.dumps(p,sort_keys=True,indent=2)+'\n');print(json.dumps(receipt))
if __name__=='__main__':main()
