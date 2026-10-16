"""Lossless typed public context prototype; no learned scorer or simulator rules.

Input is adapter JSON BEFORE treatment. Native indices are used only to decode
public reward-group associations, then discarded. Exact semantic tokens avoid
hash-bucket collisions. Canonical content keys are local, not native identity.
"""
import copy
import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def key(kind, value):
    return kind + ':' + hashlib.sha256(canonical(value).encode()).hexdigest()


_RESOURCE_SCALARS = ('current_hp', 'max_hp', 'gold', 'potion_count', 'potion_capacity')
_RESOURCE_COLLECTIONS = ('deck', 'relics', 'potion_identities')
_CARD_FIELDS = frozenset({
    'id_label', 'type', 'rarity', 'upgraded', 'cost', 'damage', 'block',
    'upgrade_count', 'misc', 'counter', 'max_hp_gain', 'gold',
    'unavailable_attributes',
})
_RELIC_FIELDS = frozenset({
    'id_label', 'counter', 'misc', 'max_hp_gain', 'gold',
})
_POTION_FIELDS = frozenset({
    'id_label', 'potion_index', 'counter', 'misc', 'gold',
})


def _entity(value, allowed):
    if not isinstance(value, Mapping):
        raise ValueError('public entity must be an object')
    return {name: copy.deepcopy(value[name]) for name in allowed if name in value}


def _available(value, transform=lambda item: item):
    """Keep the declared value and availability state, excluding diagnostics."""
    if not isinstance(value, Mapping) or value.get('availability') not in {
        'available', 'unavailable', 'unsupported', 'not_applicable'
    }:
        return {'availability': 'unavailable'}
    state = value['availability']
    result = {'availability': state}
    if state == 'available' and 'value' in value:
        result['value'] = transform(value['value'])
    return result


def _collection(value, allowed):
    def sanitize(items):
        if not isinstance(items, list):
            raise ValueError('available public entity collection must be a list')
        return [_entity(item, allowed) for item in items]
    return _available(value, sanitize)


def _keys(value):
    if not isinstance(value, Mapping):
        raise ValueError('available public key flags must be an object')
    return {
        name: value[name] for name in ('blue_key', 'green_key', 'red_key')
        if isinstance(value.get(name), bool)
    }


def _resources(value):
    if not isinstance(value, Mapping):
        return {}
    result = {
        name: _available(value.get(name))
        for name in _RESOURCE_SCALARS
    }
    result.update({
        'deck': _collection(value.get('deck'), _CARD_FIELDS),
        'relics': _collection(value.get('relics'), _RELIC_FIELDS),
        'potion_identities': _collection(value.get('potion_identities'), _POTION_FIELDS),
        'keys': _available(value.get('keys'), _keys),
    })
    return result


def _map_graph(value):
    if not isinstance(value, list):
        return None
    nodes = []
    for node in value:
        if not isinstance(node, Mapping):
            raise ValueError('public map node must be an object')
        edges = node.get('outgoing_edges', [])
        if not isinstance(edges, list):
            raise ValueError('public map edges must be a list')
        nodes.append({
            'x': node.get('x'), 'y': node.get('y'),
            'room_symbol': node.get('room_symbol'),
            'outgoing_edges': [
                {'x': edge.get('x'), 'y': edge.get('y')}
                for edge in edges if isinstance(edge, Mapping)
            ],
        })
    return sorted(nodes, key=canonical)


def _semantics(value):
    if not isinstance(value, Mapping):
        raise ValueError('public candidate semantics must be an object')
    result = {}
    for name in ('choice_type', 'gold', 'max_hp_gain', 'key', 'sapphire_key_is_lost'):
        if name in value:
            result[name] = copy.deepcopy(value[name])
    for name, fields in (
        ('card', _CARD_FIELDS), ('relic', _RELIC_FIELDS),
        ('potion', _POTION_FIELDS), ('relic_removed_with_key', _RELIC_FIELDS),
    ):
        if name in value:
            result[name] = _entity(value[name], fields)
    destination = value.get('destination')
    if isinstance(destination, Mapping):
        result['destination'] = {
            name: copy.deepcopy(destination[name])
            for name in ('location', 'x', 'y', 'room_symbol') if name in destination
        }
    return result


def normalize(public):
    if isinstance(public, str):
        public = json.loads(public)
    if not isinstance(public, Mapping):
        raise ValueError('public policy input must be an object')
    raw_state = public.get('public_state')
    if not isinstance(raw_state, Mapping):
        raise ValueError('public_state must be an object')
    state = {'screen': raw_state.get('screen')}
    if 'persistent_resources' in raw_state:
        state['persistent_resources'] = _resources(raw_state['persistent_resources'])
    if 'visible_act_boss' in raw_state:
        state['visible_act_boss'] = copy.deepcopy(raw_state['visible_act_boss'])
    if 'map_graph' in raw_state:
        state['map_graph'] = _map_graph(raw_state['map_graph'])
    if 'current_map_node' in raw_state:
        current = raw_state['current_map_node']
        if isinstance(current, Mapping):
            state['current_map_node'] = {
                name: copy.deepcopy(current[name])
                for name in ('location', 'x', 'y') if name in current
            }
        else:
            state['current_map_node'] = None
    screen_context = raw_state.get('screen_context')
    if isinstance(screen_context, Mapping):
        state['screen_context'] = {
            name: screen_context[name] for name in ('coverage_status',)
            if name in screen_context
        }
    location = raw_state.get('ordinary_location')
    if isinstance(location, Mapping):
        state['ordinary_location'] = {
            name: copy.deepcopy(location[name]) for name in ('act', 'floor')
            if name in location
        }

    allowed_availability = (
        'screen_identity', 'visible_act_boss', 'visible_map_graph',
        'current_map_node', 'immediately_legal_routes',
        'persistent_resources', 'screen_payload', 'candidate_actions',
    )
    raw_availability = public.get('availability', {})
    if not isinstance(raw_availability, Mapping):
        raw_availability = {}
    availability = {}
    for name in allowed_availability:
        value = raw_availability.get(name, 'unavailable')
        if isinstance(value, Mapping):
            value = value.get('availability', 'unavailable')
        availability[name] = value if value in {
            'available', 'unavailable', 'unsupported', 'not_applicable'
        } else 'unavailable'

    group_members = defaultdict(list)
    options = []
    for option in public.get('legal_options', []):
        if not isinstance(option, Mapping) or not isinstance(option.get('action'), Mapping):
            raise ValueError('public legal option is malformed')
        action = copy.deepcopy(option['action'])
        semantics = _semantics(option.get('semantics'))
        kind = action.get('kind')
        clean = {'kind': kind, 'semantics': semantics}
        if kind == 'reward_card':
            if 'idx1' not in action or 'idx2' not in action:
                raise ValueError('public group relationship unavailable; never infer group from ordinal')
            if semantics.get('choice_type') == 'singing_bowl_max_hp' and 'card' not in semantics:
                conflict = {
                    'kind': 'max_hp_conflict',
                    'choice_type': 'singing_bowl_max_hp',
                    'max_hp_gain': semantics.get('max_hp_gain', 2),
                    'unavailable_attributes': ['card', 'misc', 'upgrade_count'],
                }
                group_members[action['idx1']].append((action['idx2'], conflict))
            else:
                card = semantics.get('card')
                if semantics.get('choice_type') != 'card' or not isinstance(card, Mapping):
                    raise ValueError('reward_card choice has neither a card nor the supported Singing Bowl alternative')
                card = copy.deepcopy(card)
                card.setdefault('unavailable_attributes', [])
                for field in ('misc', 'upgrade_count'):
                    if field not in card and field not in card['unavailable_attributes']:
                        card['unavailable_attributes'].append(field)
                semantics['card'] = card
                clean['semantics'] = semantics
                group_members[action['idx1']].append((action['idx2'], card))
        options.append((clean, action))
    group_content = {}
    for native_group, members in group_members.items():
        if len({index for index, _ in members}) != len(members):
            raise ValueError('duplicate public card pointer')
        group_content[native_group] = sorted([card for _, card in members], key=canonical)
    groups = sorted(group_content.values(), key=canonical)
    candidates = []
    for clean, action in options:
        target = copy.deepcopy(clean)
        if action['kind'] == 'reward_card':
            group = group_content[action['idx1']]
            target['group_key'] = key('card-group', group)
            target['group_alternatives'] = group
            if clean['semantics'].get('choice_type') == 'singing_bowl_max_hp':
                target['special_key'] = key('special', {
                    'choice_type': 'singing_bowl_max_hp',
                    'max_hp_gain': clean['semantics'].get('max_hp_gain', 2),
                })
            else:
                target['card_key'] = key('card', clean['semantics']['card'])
        candidates.append(target)
    facts = {
        'public_state': state,
        'availability': availability,
        'card_reward_groups': groups,
        'legal_candidates': sorted(candidates, key=canonical),
    }
    return {'facts': facts, 'candidates': candidates}
def features(public,index):
    observation=normalize(public);candidate=observation['candidates'][index]
    # This exact typed/candidate-conditioned feature map is an audit prototype,
    # not a capacity-bounded neural architecture or game-strength model.
    target=canonical(candidate);context=canonical(observation['facts'])
    return {'candidate:'+target:1,'public-context:'+context:1,
            'candidate-x-context:'+canonical([candidate,observation['facts']]):1}
def mapped_action(public,index,current_candidates,*,current_public=None):
    """External snapshot guard; executor identities never enter features.

    Reward groups and potion slots can reuse the same indices for different
    entities. Require a freshly prepared public observation, not indices alone.
    The caller must obtain current_public and current_candidates together from
    the current native boundary; supplying an old snapshot violates this API.
    """
    if current_public is None:raise ValueError('fresh current-public snapshot required')
    if isinstance(public,str):public=json.loads(public)
    if isinstance(current_public,str):current_public=json.loads(current_public)
    if canonical(public)!=canonical(current_public):raise ValueError('stale public observation or choice set')
    def identity(a):return {k:a[k] for k in ('scope','kind','idx1','idx2','idx3') if k in a}
    projected=[identity(o['action']) for o in current_public['legal_options']]
    if projected!=[identity(a) for a in current_candidates]:raise ValueError('current candidates do not match current-public snapshot')
    option=public['legal_options'][index];action=option['action']
    identity={k:action[k] for k in ('scope','kind','idx1','idx2','idx3') if k in action}
    matches=[a for a in current_candidates if all(a.get(k)==v for k,v in identity.items())]
    if len(matches)!=1:raise ValueError('stale or ambiguous native candidate identity')
    return matches[0]
