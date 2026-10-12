"""Derive terminal context/opportunities from retained native transitions."""
import json
from pathlib import Path
import sys
import time
from worker import write

base = Path(sys.argv[1])
ledger = Path(sys.argv[2])
cpu = time.process_time()
state = json.loads(ledger.read_text())
assert not state['active']
report = json.loads((base/'report.json').read_text())
corrections = {}
for row in report['rows']:
    encounters = set()
    opportunities = 0
    eligible_opportunities = 0
    for line in (base/f"{row['seed']}-{row['arm']}.jsonl").open():
        step = json.loads(line)
        if step['battle_active']:
            encounter = step['snapshot_raw'].get('encounter_id')
            assert encounter is not None
            encounters.add(encounter)
            kinds = step['legal_action_kinds']
            opportunities += any(k in ('potion', 'potion_discard') for k in kinds)
            eligible_opportunities += any(kinds[i] in ('potion', 'potion_discard')
                                          for i in step['eligible_action_indices'])
    row['battle_encounters'] = sorted(encounters)
    row['loss_type'] = ('battle' if step['battle_active'] else 'nonbattle') if row['outcome']=='PLAYER_LOSS' else None
    row['loss_encounter'] = step['snapshot_raw'].get('encounter_id') if row['loss_type']=='battle' else None
    row['metrics']['battle_native_potion_action_opportunities'] = opportunities
    row['metrics']['battle_controller_eligible_potion_action_decisions'] = eligible_opportunities
    corrections[f"{row['seed']}-{row['arm']}"] = {
        key: row[key] for key in ('battle_encounters', 'loss_type', 'loss_encounter')}
for arm in ('off', 'on'):
    rows = [r for r in report['rows'] if r['arm']==arm and r['valid']]
    secondary = report['secondary'][arm]
    secondary['heart_reach'] = sum('THE_HEART' in r['battle_encounters'] for r in rows)
    secondary['heart_wins'] = sum(r['outcome']=='PLAYER_WIN' and 'THE_HEART' in r['battle_encounters'] for r in rows)
    secondary['loss_type_counts'] = {kind: sum(r['loss_type']==kind for r in rows) for kind in ('battle','nonbattle')}
    secondary['loss_encounters'] = [r['loss_encounter'] for r in rows if r['outcome']=='PLAYER_LOSS']
    for key in ('battle_native_potion_action_opportunities','battle_controller_eligible_potion_action_decisions'):
        secondary['metrics'][key] = sum(r['metrics'][key] for r in rows)
    secondary['potion_offer_option_occurrences'] = sum(r['potion_offer_option_occurrences'] for r in rows)
    secondary['potion_opportunity_floor_screens'] = sum(r['potion_opportunity_floor_screens'] for r in rows)
    secondary['potion_available_stratum'] = {
        label: {'n':len(group),'floors':[r['floor'] for r in group]}
        for label,group in [('observed',[r for r in rows if r['metrics']['battle_native_potion_action_opportunities']>0]),
                            ('unobserved',[r for r in rows if r['metrics']['battle_native_potion_action_opportunities']==0])]}
report['secondary_field_definitions'] = {
    'loss_context':'Actual last native transition, before battleActive clears at terminal.',
    'battle_encounters':'Native snapshot encounter_id in actual Battle transitions.',
    'battle_potion_eligible_decisions':'Historical field counts native legal potion-use opportunities only.',
    'battle_native_potion_action_opportunities':'Native legal use/discard availability, regardless of treatment filter.',
    'battle_controller_eligible_potion_action_decisions':'Actual arm-filtered eligible use/discard opportunities.',
    'potion_available_stratum':'Descriptive native legal use/discard availability; primary includes all96 pairs.'}
state['secondary_postprocess_cpu'] = time.process_time()-cpu
state['cpu'] += state['secondary_postprocess_cpu']
report['cpu'] = state['cpu']
write(ledger,state)
write(base/'report.json',report)
write(base/'secondary-corrections.json',{'producer':sys.argv[3],'corrections':corrections,
                                       'definitions':report['secondary_field_definitions']})
write(base/'current-execution-snapshot.json',state)
print('SECONDARY_CONTEXT_CORRECTION_PASS', state['cpu'])
