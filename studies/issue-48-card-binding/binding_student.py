"""Public JSON only. A small shared scorer; no native or teacher imports."""
import copy
import hashlib
import json
import math
import random

DIM = 128
HIDDEN = 16


def with_public_history(serialized, history, current_context=None):
    value = json.loads(serialized)
    # Retain past public resource changes and screen transitions. Omit prior
    # candidate labels/identities to prevent a masked choice-content side channel.
    value['public_state']['history'] = [
        {'pre_screen': entry.get('pre_decision', {}).get('screen'),
         'post_screen': entry.get('post_decision', {}).get('screen'),
         'resources': entry.get('resource_change', {}),
         'chosen_ordinal': entry.get('selected_action', {}).get('index')}
        for entry in history]
    location=(current_context or {}).get('current',{}).get('location',{})
    value['public_state']['ordinary_location']={key:location.get(key) for key in ('act','floor','room_type')}
    return json.dumps(value, sort_keys=True, separators=(',',':'))


def treatment(serialized, arm):
    value = json.loads(serialized)
    if arm not in ('visible', 'masked'):
        raise ValueError(arm)
    if arm == 'masked':
        state = value['public_state']
        for name in ('visible_act_boss', 'map_graph', 'current_map_node', 'screen_context'):
            state[name] = None
        for option in value['legal_options']:
            option['action'] = {'kind': option['action']['kind']}
            option['semantics'] = None
    else:
        # Ordinals/order are supplied separately; label strings and executor
        # indices are never model features in either arm.
        for option in value['legal_options']:
            option['action'] = {'kind': option['action']['kind']}
    return value


def deck_token(item):
    fields = ('id', 'upgraded', 'upgrade_count', 'misc')
    if not isinstance(item, dict):
        raise ValueError('public deck item must be a card object')
    if type(item.get('upgraded')) is not bool or any(type(item.get(k)) is not int for k in ('id','upgrade_count','misc')):
        raise ValueError('public deck identity/attribute type invalid')
    return '/state/persistent_resources/deck/value=card:' + json.dumps({k:item[k] for k in fields},sort_keys=True,separators=(',',':'),allow_nan=False)
def _tokens(value, path=''):
    if isinstance(value, dict):
        for key in sorted(value):
            yield from _tokens(value[key], path + '/' + key)
    elif isinstance(value, list):
        for item in value:
            if path == '/state/persistent_resources/deck/value':
                yield deck_token(item), 1.0
            if isinstance(item,dict) and '/map_graph' in path:
                # Set encoder includes complete public node/edge tuples,
                # preserving endpoint/room associations rather than only marginals.
                yield path + '=relation:' + json.dumps(item,sort_keys=True,separators=(',',':')), 1.0
            yield from _tokens(item, path + '[]')
    elif type(value) in (int, float):
        # IDs and public graph coordinates are categorical; numeric resources
        # remain continuous, with a fixed transform shared by both arms.
        if path.endswith(('/id', '/x', '/y', '/candidate_index')):
            yield path + '=id:' + str(value), 1.0
        else:
            yield path + '=number', math.copysign(math.log1p(abs(value))/10, value)
    else:
        yield path + '=' + json.dumps(value, sort_keys=True), 1.0


def features(value, index):
    option = value['legal_options'][index]
    data = {'state': value['public_state'], 'option': option,
            'legal_count': len(value['legal_options']), 'ordinal': index}
    result = [0.0] * DIM
    for token, weight in _tokens(data):
        digest = hashlib.sha256(token.encode()).digest()
        result[int.from_bytes(digest[:4], 'little') % DIM] += weight * (1 if digest[4] & 1 else -1)
    return [math.tanh(x) for x in result]


class Scorer:
    def __init__(self, seed):
        rng = random.Random(seed)
        self.w = [[rng.uniform(-0.05,0.05) for _ in range(DIM)] for _ in range(HIDDEN)]
        self.b = [0.0]*HIDDEN
        self.v = [rng.uniform(-0.05,0.05) for _ in range(HIDDEN)]
        self.c = 0.0

    def forward(self, x):
        h = [math.tanh(sum(a*b for a,b in zip(row,x))+bias) for row,bias in zip(self.w,self.b)]
        return sum(a*b for a,b in zip(self.v,h))+self.c, h

    def update(self, rows, lr=0.01):
        dw = [[0.0]*DIM for _ in range(HIDDEN)]
        db = [0.0]*HIDDEN
        dv = [0.0]*HIDDEN
        dc = 0.0
        for x,y,weight in rows:
            pred,h = self.forward(x)
            grad = 2*(pred-y)*weight
            dc += grad
            for j in range(HIDDEN):
                dv[j] += grad*h[j]
                z = grad*self.v[j]*(1-h[j]*h[j])
                db[j] += z
                for k in range(DIM):
                    dw[j][k] += z*x[k]
        scale = lr/sum(weight for _,_,weight in rows)
        for j in range(HIDDEN):
            self.v[j] -= scale*dv[j]
            self.b[j] -= scale*db[j]
            for k in range(DIM):
                self.w[j][k] -= scale*dw[j][k]
        self.c -= scale*dc
        return sum(abs(z) for row in dw for z in row)+sum(map(abs,dv))+abs(dc)

    def scores(self, serialized, arm):
        value = treatment(serialized,arm)
        return [self.forward(features(value,i))[0] for i in range(len(value['legal_options']))]

    def choose(self, serialized, arm):
        scores = self.scores(serialized,arm)
        return max(range(len(scores)), key=lambda i:scores[i])

    def state(self):
        return copy.deepcopy({'w':self.w,'b':self.b,'v':self.v,'c':self.c})
