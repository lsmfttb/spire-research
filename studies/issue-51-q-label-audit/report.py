"""Descriptive owner-weighted reporting; never reads a model or new cohort."""
import collections
import json
import time
from audit import OUT, load, write, mean

def main():
    started=time.process_time(); rows=load(OUT/'owner_metrics.json'); ranks=load(OUT/'rank_report.json'); traces=load(OUT/'trace_summary.json'); examples=load(OUT/'public_examples.json')
    assert traces['complete'] and not traces['violations'] and not traces['orphan_files']
    receipt_groups={}
    for t in traces['receipts']: receipt_groups.setdefault(t['owner'],[]).append(t)
    patterns={}; severity={}
    cells={}
    for r in rows: cells.setdefault(f"{r['split']}/{'assisted' if r['assisted'] else 'natural'}/{r['family']}",[]).append(r)
    for cell,rs in cells.items():
        keys=sorted({k for r in rs for t in receipt_groups[r['owner']] for k in t['nonbattle_patterns']})
        patterns[cell]={'owners':len(rs),'trajectories':sum(len(receipt_groups[r['owner']]) for r in rs),
           'mean_nonbattle_decisions_per_continuation':mean([mean([sum(t['nonbattle_patterns'].values()) for t in receipt_groups[r['owner']]]) for r in rs]),
           'mean_counts_per_continuation_root_weighted':{k:mean([mean([t['nonbattle_patterns'].get(k,0) for t in receipt_groups[r['owner']]]) for r in rs]) for k in keys}}
        floors=sorted({l['floor'] for r in rs for l in r['label_outcomes']})
        acts=sorted({l['act'] for r in rs for l in r['label_outcomes']})
        severity[cell]={'owners':len(rs),'root_weighted_terminal_floor_mass':{str(f):mean([mean([int(l['floor']==f) for l in r['label_outcomes']]) for r in rs]) for f in floors},
          'root_weighted_max_act_mass':{str(a):mean([mean([int(l['act']==a) for l in r['label_outcomes']]) for r in rs]) for a in acts},
          'root_weighted_heart_win_fraction':mean([mean([int(l['heart_win']) for l in r['label_outcomes']]) for r in rs]),
          'mean_q_over60':mean([mean(r['q_over_60']) for r in rs]),
          'mean_abs_centered_q_over60':mean([mean([abs(q)/60 for q in r['action_centered_q']]) for r in rs]),
          'mean_squared_q_over60':mean([mean([q*q/3600 for q in r['q']]) for r in rs]),
          'mean_within_root_variance_over3600':mean([r['within_root_variance']/3600 for r in rs])}
    # Equal owner, then equal all action/driver continuations within owner.
    held={x['owner']:x for x in rows}; event_patterns={}
    for cell,rs in cells.items():
        cs={r['owner']:[p for p in traces['owner_driver_patterns'] if p['owner']==r['owner']] for r in rs}
        event_patterns[cell]={'owners':len(rs),
          'mean_nonbattle_decisions_holding_potions_per_continuation':mean([mean([p['patterns'].get('nonbattle_decisions_holding_potions',0)/held[p['owner']]['legal_count'] for p in cs[r['owner']]]) for r in rs]),
          'mean_net_nonbattle_potion_count_delta_per_continuation':mean([mean([p['patterns'].get('nonbattle_potion_count_delta',0)/held[p['owner']]['legal_count'] for p in cs[r['owner']]]) for r in rs])}
    report={'ranks':ranks,'trajectory_patterns':patterns,'potion_holding':event_patterns,'terminal_severity':severity,
      'category_weighting':'Conditional equal roots containing category, then equal options within that category; category cohorts overlap and must not be summed. All event statistics equal roots then equal option/driver continuations; decision events are not independent samples.',
      'gate':'STUDY_ONLY; no model promotion, merge or automatic next execution.',
      'inference':{'sampling_variation':'Observable strict within-root preference reversals and one-driver ties make finite two-driver resolution a plausible contributor, especially reward cells. They do not identify variance over independent hidden worlds or establish it as the major cause of optimizer diagnostic failure.',
        'ties':'Most all-mean-Q ties do not conceal opposing driver preferences: count the driver-internally constant cases separately, including single-action roots. Such ties still do not prove strategic indifference.',
        'state_confounding':'Natural versus assisted/origin/floor differences are observational source mixtures; old validation/heldout omit natural MAP and assisted REWARDS. They cannot identify a causal assistance or policy effect.',
        'target_mismatch':'Q under forced action then fixed Expert/Search continuation is different from value under repeated learned noncombat decisions. More samples under the same Expert improve precision of that target but cannot change the continuation policy.',
        'resolution':'Integer terminal-floor equality coexists with different public resources/decisions at matched public act/floor/screen gates. This exposes outcome resolution, not which choice increases Heart win probability.',
        'expert':'Pinned category-first weighted heuristic has positive low-probability category/action floors. Observed stochastic trajectories plus source establish sampling behavior; not causal weakness or comparative strength.'},
      'one_prospective_planner_option':{'name':'Controlled policy evaluation under one newly frozen public incumbent',
        'hypothesis':'On prospectively sampled owner-disjoint naturally reached public noncombat roots, fixed terminal-floor action advantages under a frozen public incumbent differ reproducibly from those under the frozen Expert continuation.',
        'design_outline':'Planner would separately preregister one incumbent, authentic independent root selection and split, continuation randomization/repetitions, paired initial-action protocol with same Battle/Search regime, uncertainty and compute cap. Compare fixed Expert versus incumbent continuation with terminal floor unchanged; evaluate preference consistency and counterfactual action ranking on heldout roots. No label/source/model selection from current exposed data.',
        'falsification':'If paired heldout advantages and ranking preference are stable across the two frozen continuation policies within prespecified uncertainty, evidence for substantial continuation-target mismatch is weakened.',
        'status':'NOT EXECUTED; requires separate Planner authorization. No extra sampling, labels, training or game started.'}}
    write(OUT/'report.json',report)
    train=ranks['split']['train']; lines=['# Issue51 — frozen two-driver Q audit','',
      'STUDY_ONLY. All 512 original owners, 2886 label JSON receipts and 2886 gzip traces verified against original seals. No games, new labels, learning or simulator import. Traces begin after the forced action; full original raw states are audit-only and are not exported.',
      '', '## Ranking stability','',
      '| Original cell | Owners | Multi-action | Q informative | All mean-Q ties | Roots with reversal | Root-weighted reversal | Top sets disjoint |',
      '|---|---:|---:|---:|---:|---:|---:|---:|']
    for cell,rs in sorted(cells.items()):
        s=ranks['strata'][cell]; lines.append(f"| {cell} | {s['owners']} | {s['ranking_eligible_owners']} | {s['mean_q_informative_owners']} | {s['all_mean_q_tied_owners']} | {s['owners_with_reversal']} | {100*s['root_weighted_pair_rates']['reversal']:.3f}% | {s['top_sets_disjoint_owners']} |")
    lines += ['',f"Train has {train['single_action_owners']}/384 single-action roots. Across {train['ranking_eligible_owners']} multi-action roots, equal-root pair rates are concordant {100*train['root_weighted_pair_rates']['concordant']:.3f}%, strict reversal {100*train['root_weighted_pair_rates']['reversal']:.3f}%, one-driver tie {100*train['root_weighted_pair_rates']['one_driver_tie']:.3f}%, both-driver tie {100*train['root_weighted_pair_rates']['both_driver_tie']:.3f}%. Raw legal pairs are descriptive, never independent replicates.",
      '',f"The 181/384 all-mean-Q ties include {train['both_drivers_all_action_tied_owners']} roots where each driver is internally constant and {train['mean_q_tied_but_driver_action_variation_owners']} roots with driver action variation hidden by the mean. {train['all_option_driver_floors_identical_owners']} roots have one identical floor across all options AND drivers. {train['owners_with_concealed_opposition']} training roots have some artificial mean-equality pair, even when other actions make the root informative.",
      '',f"Exact top-action sets have mean Jaccard {train['mean_top_jaccard_eligible']:.6f} across all multi-action roots and {train['mean_top_jaccard_mean_q_informative']:.6f} across 203 mean-Q-informative roots. Midrank Spearman {train['mean_spearman_defined']:.6f} and tau-b {train['mean_kendall_tau_b_defined']:.6f} are defined on only {train['spearman_defined_owners']} training roots; constant rankings are undefined. Either-driver informative roots (205) are also separately reported.",
      '', '## Floor-label signal','',
      f"Equal roots, equal legal options within root: train mean common floor {train['mean_root_floor']:.6f}; q/60 mean {train['mean_q_over60']:.6f}; within-root variance {train['mean_within_root_variance']:.6f} floors² versus between-root variance {train['between_root_variance']:.6f}. The state-common share of globally centered label variance is {100*train['between_fraction_global_centered_variance']:.3f}%. E[q²] = E[mu_root²] + E[within variance] = {train['root_common_squared_energy']:.6f} + {train['mean_within_root_variance']:.6f}. This is a target decomposition / hypothetical zero-prediction error, not a fitted model's MSE attribution. Divide squared terms by 3600 for q/60.",
      '', 'Full report includes floor/act, original provenance/origin, potion-held and potion-action strata, conditional action-category summaries, action-centered labels and terminal-floor/max-act masses. Denominators are explicit. Categories overlap; no reweighting or altered loss is proposed.',
      '', '## Public trace evidence and pinned policy','',
      f"All trace chains and terminal outcomes match. {len(examples['equal_floor_different_public_intermediates'])} deterministic equal-floor matched public-gate examples and {len(examples['paired_driver_decision_examples'])} paired-driver differing-decision examples are in public_examples.json with exact owner/root/ordinal/driver and label/gzip hashes. Only explicit public HP, gold, potion/deck identities, floor/act and chosen-action fields are projected. Matched gates need not be identical hidden states; no causal outcome comparison follows.",
      '', 'Pinned STSRL 7d172f7c24c9d29429b6946ba44f6b59760b0d96 Expert source is category-first weighted stochastic sampling from public resources and card/route labels, including positive 0.02 category/action floors. Fixed unguided privileged Battle Search-v2@400 includes potions. Source facts do not establish strong optimization or causally weak noncombat play. Battle remains privileged, with no learner features created.',
      '', '## Interpretation','']
    lines += [v+'\n' for v in report['inference'].values()]
    lines += ['Original validation and heldout each have 32 assisted MAP and 32 natural REWARDS; natural MAP and assisted REWARDS are absent. Old labels are retrospectively exposed. No #48/#49/#50 model outcome is used to select a variant.',
      '', '## Exactly one unexecuted next option','',report['one_prospective_planner_option']['name']+'. '+report['one_prospective_planner_option']['hypothesis'],report['one_prospective_planner_option']['design_outline'],report['one_prospective_planner_option']['falsification'],report['one_prospective_planner_option']['status'],
      '', '## Reproducibility and retention','',
      'Run audit.py, then run_traces.py, then report.py against the original sealed stores. Inputs, source pins, full per-root metrics and public-only examples have SHA256 receipts. Original #46/#47 seals and November12/13 retention remain unchanged. Issue51 derived evidence belongs to independent Reviewer and Planner through 2026-11-14. Code is disposable and has no permanent integration or merge disposition.']
    (OUT/'REPORT.md').write_text('\n'.join(lines)+'\n')
    ledger=load(OUT/'execution.json'); ledger['charges']['report_cpu']=time.process_time()-started; ledger['stage']='REPORT_COMPLETE'; write(OUT/'execution.json',ledger)
    print({'report_cpu':ledger['charges']['report_cpu']})

if __name__=='__main__': main()
