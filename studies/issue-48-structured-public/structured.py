"""Frozen public-only entity encoder and pure Python inference (no native imports).

All public_state and selected semantic option fields are retained. Executor fields
are removed by the exact comparator treatment. Lists retain positions and length;
scalar siblings remain bound within their dictionary entity. Map relations receive
additional destination/incoming/outgoing pools, using actual public coordinates.
"""
import math
from collections import Counter
from student import treatment

WIDTH=8
VOCAB_LIMIT=3000

def number(value):
    return math.copysign(math.log1p(abs(value)),value)/10

def entities(value,path='state',positions=()):
    own=[]; children=[]
    if isinstance(value,dict):
        for key,item in sorted(value.items()):
            name=path+'.'+key
            if isinstance(item,(dict,list)):
                children.extend(entities(item,name,positions))
            else:
                # Shared identity categories across deck and reward candidates.
                category=('card' if 'deck' in path or '.card' in path else
                          'relic' if 'relic' in path else 'potion' if 'potion' in path else path)
                if key in ('id','id_label','name','upgraded','upgrade_count'):
                    own.append((category+':'+key+'='+str(item),1.))
                elif isinstance(item,(int,float)) and not isinstance(item,bool):
                    own.extend([(name+':present',1.),(name+':number',number(item))])
                else: own.append((name+'='+str(item),1.))
        own.append((path+':object',1.))
    elif isinstance(value,list):
        own.extend([(path+':list',1.),(path+':length',number(len(value)))])
        for i,item in enumerate(value): children.extend(entities(item,path+'[]',positions+((path,i),)))
    else:
        own.append((path+'='+str(value),1.))
    own.extend((p+':position',number(i)) for p,i in positions)
    return [own]+children

def raw_channels(serialized,index):
    data=treatment(serialized,'visible'); state=data['public_state']; option=data['legal_options'][index]
    destination=option.get('semantics',{}).get('destination',{})
    nodes=state.get('map_graph',[])
    key=lambda n:(n.get('x'),n.get('y'))
    dest=next((n for n in nodes if key(n)==key(destination)),None) if destination else None
    outgoing=set(key(e) for e in (dest or {}).get('outgoing_edges',[]))
    incoming=[n for n in nodes if dest and key(dest) in {key(e) for e in n.get('outgoing_edges',[])}]
    return [entities(state),entities(option,'option'),
            entities(dest or {},'map_node'),
            sum((entities(n,'map_node') for n in incoming),[]),
            sum((entities(n,'map_node') for n in nodes if key(n) in outgoing),[])], [number(len(data['legal_options'])),number(index)]

def vocabulary(training):
    counts=Counter()
    for table in training:
        for i in range(len(table['q'])):
            channels,_=raw_channels(table['root']['serialized'],i)
            counts.update(token for channel in channels for entity in channel for token,value in entity)
    # Cap declared before data: frequency then lexical ties, train-only OOV0.
    selected=sorted(counts,key=lambda t:(-counts[t],t))[:VOCAB_LIMIT]
    return {'<OOV>':0,**{token:i+1 for i,token in enumerate(sorted(selected))}}

def encode(serialized,index,vocab):
    channels,extra=raw_channels(serialized,index)
    return [[[ (vocab.get(t,0),v) for t,v in entity] for entity in channel] for channel in channels],extra

def linear(x,w,b):
    return [math.tanh(sum(a*v for a,v in zip(row,x))+bias) for row,bias in zip(w,b)]

class Structured:
    def __init__(self,checkpoint): self.vocab=checkpoint['vocabulary']; self.state=checkpoint['state']
    def score(self,encoded):
        channels,extra=encoded; s=self.state; pools=[]
        for channel in channels:
            hidden=[]
            for entity in channel:
                x=[sum(s['embedding'][t][j]*v for t,v in entity)/len(entity) for j in range(WIDTH)]
                hidden.append(linear(x,s['entity_w'],s['entity_b']))
            pools.extend([sum(h[j] for h in hidden)/len(hidden) if hidden else 0. for j in range(WIDTH)])
        h=linear(pools+extra,s['score_w'],s['score_b'])
        return sum(a*b for a,b in zip(h,s['out_w']))+s['out_b']
    def scores(self,serialized,condition='visible'):
        assert condition=='visible'
        n=len(treatment(serialized,'visible')['legal_options'])
        return [self.score(encode(serialized,i,self.vocab)) for i in range(n)]
    def choose(self,serialized,condition='visible'):
        scores=self.scores(serialized,condition); return max(range(len(scores)),key=lambda i:scores[i])
