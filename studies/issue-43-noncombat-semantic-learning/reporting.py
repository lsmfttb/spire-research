"""Issue43 fixed reports. Trusted analysis; no model selection or game rules."""
import json
from collections import Counter, defaultdict
from formal import CONFIG, cohort, clustered_interval, digest, evaluation_plan
from learning import load


def interval(values,seed):
    return clustered_interval(values,seed,CONFIG['bootstrap_replicates'])


def heldout(tables,roots,checkpoints):
    selected=cohort(roots,'heldout')
    expected={digest(__import__('formal').keys(r)):r for r in selected}
    if len(tables)!=len(expected) or {t['root_id'] for t in tables}!=set(expected):
        raise ValueError('INCOMPLETE_HELDOUT')
    models={a:load(c) for a,c in checkpoints.items()}
    metrics={a:defaultdict(lambda:defaultdict(list)) for a in models}
    disagreements=defaultdict(list); rows=[]
    for table in tables:
        root=expected[table['root_id']]
        if table['split']!='heldout' or table['owner']!=root['owner'] or table['serialized']!=root['serialized_public_input']:
            raise ValueError('heldout identity changed')
        choices={}; row={'root_id':table['root_id'],'owner':root['owner'],'family':root['family']}
        for arm,model in models.items():
            scores=[60*x for x in model.scores(table['serialized'],arm)]
            q=table['q']
            if len(q)!=root['legal_count'] or len(scores)!=len(q): raise ValueError('incomplete Q table')
            chosen=model.choose(table['serialized'],arm); choices[arm]=chosen
            values={'mse':sum((s-y)**2 for s,y in zip(scores,q))/len(q),
                'regret':max(q)-q[chosen],'chosen_q':q[chosen],
                'optimal_action':float(q[chosen]==max(q)),
                'pairwise_ranking':sum((scores[i]-scores[j])*(q[i]-q[j])>0 for i in range(len(q))
                    for j in range(i) if q[i]!=q[j])/max(1,sum(q[i]!=q[j] for i in range(len(q)) for j in range(i)))}
            row[arm]={'ordinal':chosen,**values}
            for family in ('all',root['family']):
                for name,value in values.items(): metrics[arm][family+'_'+name][root['owner']].append(value)
        disagreements[root['owner']].append(float(choices['visible']!=choices['masked']))
        rows.append(row)
    return {'status':'VALID_HELDOUT','estimand':'frozen realized-world q_floor; source owner clustered',
        'checkpoint_sha256':{a:c['checkpoint_sha256'] for a,c in checkpoints.items()},'rows':rows,
        'disagreement':interval(disagreements,CONFIG['bootstrap_seeds']['heldout']),
        'metrics':{a:{name:interval(v,CONFIG['bootstrap_seeds']['heldout']) for name,v in m.items()} for a,m in metrics.items()}}


def descriptors(serialized):
    """Public empirical descriptors, without fitted preprocessing or value tiers."""
    value=json.loads(serialized)
    out=Counter({'family:'+str(value['public_state']['screen']):1,'legal_count:'+str(len(value['legal_options'])):1})
    def visit(item,path):
        if isinstance(item,dict):
            for key,v in item.items(): visit(v,path+'.'+key)
        elif isinstance(item,list):
            for v in item: visit(v,path+'[]')
        elif path.endswith(('.id','.room_symbol','.upgraded','.visible_act_boss','.kind')):
            out[path+':'+str(item)]+=1
    visit(value['legal_options'],'options'); visit(value['public_state'].get('map_graph',{}),'map')
    return out


def shift(roots,runs):
    source=Counter(); fresh={a:Counter() for a in ('visible','masked','expert_source')}
    for root in roots: source.update(descriptors(root['serialized_public_input']))
    for run in runs:
        for choice in run['choices']:
            if 'serialized_public_input' in choice: fresh[run['arm']].update(descriptors(choice['serialized_public_input']))
    def compare(counts):
        left=sum(source.values()); right=sum(counts.values())
        return {'counts':dict(counts),'unseen_descriptors':{k:v for k,v in counts.items() if k not in source},
            'total_variation':sum(abs(source[k]/max(1,left)-counts[k]/max(1,right)) for k in source.keys()|counts.keys())/2}
    return {'source_counts':dict(source),'fresh':{a:compare(c) for a,c in fresh.items()},
        'description':'empirical public descriptor frequency; not a causal explanation or fitted model feature'}


def fresh(runs,checkpoints,train_roots,synthetic=False):
    plan=evaluation_plan(checkpoints); expected={(p['seed'],p['arm']):p for p in plan}
    by={(r['seed'],r['arm']):r for r in runs}
    if len(runs)!=len(expected) or set(by)!=set(expected): raise ValueError('INCOMPLETE: exact 128 x 3 required')
    for key,row in by.items():
        p=expected[key]
        if row['key']!=[p['key'],p['seed'],p['arm']] or not row['terminal'] or row['problems']:
            raise ValueError('INCOMPLETE: invalid fresh result')
        if row['outcome'] not in ('PLAYER_VICTORY','PLAYER_LOSS'): raise ValueError('INCOMPLETE: nonterminal outcome')
    counts={a:Counter() for a in ('visible','masked','expert_source')}; reach={a:Counter() for a in counts}
    shared=Counter(); disagreement=Counter(); pairs=[]
    for seed in range(CONFIG['fresh_seeds'][0],CONFIG['fresh_seeds'][1]+1):
        rows={a:by[seed,a] for a in counts}
        for arm,row in rows.items():
            for act in (2,3,4): reach[arm]['act'+str(act)]+=int(row['max_act']>=act)
            reach[arm]['heart_encountered']+=int(row['heart_encountered']); reach[arm]['heart_win']+=int(row['heart_win'])
            for c in row['choices']:
                counts[arm][c['family']+'_'+c.get('route','expert')]+=1
                if arm!='expert_source' and c['family'] in ('MAP_SCREEN','REWARDS') and c.get('route')!='learned':
                    raise ValueError('PUBLIC_INPUT_INVALID: supported fallback')
        # Public hash plus occurrence number: repeated identical checkpoints are
        # matched within a seed, never by ordinal position after divergence.
        def indexed(row):
            seen=Counter(); out={}
            for c in row['choices']:
                if 'public_sha256' not in c or 'ordinal' not in c: continue
                h=c['public_sha256']; n=seen[h]; seen[h]+=1; out[h,n]=c
            return out
        v,m=indexed(rows['visible']),indexed(rows['masked'])
        for key in v.keys()&m.keys():
            if v[key]['serialized_public_input']!=m[key]['serialized_public_input']:
                raise ValueError('public hash collision/mismatch')
            f=v[key]['family']; shared[f]+=1; disagreement[f]+=v[key]['ordinal']!=m[key]['ordinal']
        pairs.append({'seed':seed,**{a:rows[a]['floor'] for a in rows},
            'visible_minus_masked':rows['visible']['floor']-rows['masked']['floor'],
            'visible_minus_expert':rows['visible']['floor']-rows['expert_source']['floor']})
    vm=interval({p['seed']:[p['visible_minus_masked']] for p in pairs},CONFIG['bootstrap_seeds']['visible_minus_masked'])
    ve=interval({p['seed']:[p['visible_minus_expert']] for p in pairs},CONFIG['bootstrap_seeds']['visible_minus_expert'])
    sufficient=all(counts[a][f+'_learned']>=64 for a in ('visible','masked') for f in ('MAP_SCREEN','REWARDS'))
    claims=[]
    if not sufficient: claims=['FRESH_LEARNED_CONTROL_INSUFFICIENT']
    elif vm['lower']>0:
        claims=['PUBLIC_SEMANTICS_LEARNING_GAIN_ESTABLISHED']
        if ve['lower']>0 and reach['visible']['act2']>=reach['expert_source']['act2']:
            claims.append('NONCOMBAT_TEACHER_ASSISTED_RUN_IMPROVEMENT_ESTABLISHED')
    elif vm['upper']<0: claims=['PUBLIC_SEMANTICS_HARM_CONFIRMED']
    else: claims=['LEARNING_IMPROVEMENT_NOT_ESTABLISHED']
    return {'status':'SYNTHETIC_READINESS_ONLY' if synthetic else 'VALID_FRESH',
        'claims':[] if synthetic else claims,'synthetic_gate_exercise':claims if synthetic else None,
        'information_regime':'teacher-assisted noncombat; privileged Search-v2@400 Battle, not noSL',
        'disposition':'STUDY_ONLY','pairs':pairs,'visible_minus_masked':vm,'visible_minus_expert':ve,
        'choice_counts':{a:dict(c) for a,c in counts.items()},'progress_counts':{a:dict(c) for a,c in reach.items()},
        'shared_public_state_counts':dict(shared),'shared_public_disagreement':dict(disagreement),
        'source_to_fresh_shift':shift(train_roots,runs),'runs':runs}
