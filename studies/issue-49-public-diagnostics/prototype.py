"""Lossless typed public context prototype; no learned scorer or simulator rules.

Input is adapter JSON BEFORE treatment. Native indices are used only to decode
public reward-group associations, then discarded. Exact semantic tokens avoid
hash-bucket collisions. Canonical content keys are local, not native identity.
"""
import copy, hashlib, json
from collections import Counter,defaultdict

def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False)
def key(kind,x):return kind+':'+hashlib.sha256(canonical(x).encode()).hexdigest()
def normalize(public):
    if isinstance(public,str):public=json.loads(public)
    state=copy.deepcopy(public['public_state'])
    # Set-valued public entity collections are canonicalized with multiplicity.
    # History remains ordered. Map node objects retain directed edge endpoints.
    for name in ('deck','relics','map_graph'):
        value=state.get(name)
        if isinstance(value,list):state[name]=sorted(value,key=canonical)
    group_members=defaultdict(list);options=[]
    for ordinal,option in enumerate(public['legal_options']):
        a=option['action'];sem=copy.deepcopy(option['semantics']);kind=a['kind']
        clean={'kind':kind,'semantics':sem}
        if kind=='reward_card':
            if 'idx1' not in a or 'idx2' not in a:raise ValueError('public group relationship unavailable; never infer group from ordinal')
            group_members[a['idx1']].append((a['idx2'],sem['card']))
        options.append((clean,a))
    group_content={}
    for native_group,members in group_members.items():
        if len({idx for idx,_ in members})!=len(members):raise ValueError('duplicate public card pointer')
        group_content[native_group]=sorted([card for _,card in members],key=canonical)
    groups=sorted(group_content.values(),key=canonical)
    candidates=[]
    for clean,a in options:
        target=copy.deepcopy(clean)
        if a['kind']=='reward_card':
            group=group_content[a['idx1']]
            target['group_key']=key('card-group',group)
            target['group_alternatives']=group
            target['card_key']=key('card',clean['semantics']['card'])
        # Semantics carry public potion slot/identity for discard/use; opaque
        # execution fields are never retained. Ordinal is only external tie-break.
        candidates.append(target)
    facts={'public_state':state,'availability':copy.deepcopy(public.get('availability',{})),
           'card_reward_groups':groups,'legal_candidates':sorted(candidates,key=canonical)}
    # If a future public adapter explicitly supplies inspectable nonlegal facts,
    # retain them. This pin exposes current rewards as legal choices even when
    # taking a potion at full inventory will forfeit it; no future offer inferred.
    if 'inspectable_unresolved' in public:facts['inspectable_unresolved']=copy.deepcopy(public['inspectable_unresolved'])
    return {'facts':facts,'candidates':candidates}
def features(public,index):
    observation=normalize(public);candidate=observation['candidates'][index]
    # This exact typed/candidate-conditioned feature map is an audit prototype,
    # not a capacity-bounded neural architecture or game-strength model.
    target=canonical(candidate);context=canonical(observation['facts'])
    return {'candidate:'+target:1,'public-context:'+context:1,
            'candidate-x-context:'+canonical([candidate,observation['facts']]):1}
def mapped_action(public,index,current_candidates):
    """External action guard: mapping never enters features; reject stale IDs."""
    option=public['legal_options'][index];action=option['action']
    identity={k:action[k] for k in ('scope','kind','idx1','idx2','idx3') if k in action}
    matches=[a for a in current_candidates if all(a.get(k)==v for k,v in identity.items())]
    if len(matches)!=1:raise ValueError('stale or ambiguous native candidate identity')
    return matches[0]
