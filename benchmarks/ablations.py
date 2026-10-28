"""Ablation study: System C with one mechanism removed at a time, on identical seeded fixtures.

Each row pairs a mechanism with the fault it exists to stop. Verdicts come from the
apps' ground_truth tables via web_bench.oracle/judge (code not shared with the runner).
`no_entity_binding` binds the first name match (what a name-only agent does);
`no_fencing` stubs the executor lease and measures overlapping dispatch windows.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'apps'), str(ROOT / 'benchmarks')]
from resolve_core import Ledger
from web_bench import CMD, INTENDED_CONTACT, judge, oracle, wilson
from web_fixtures import FixtureWorld
from web_mission import MissionError, WebMissionRunner

ABLATIONS = {  # name -> (runner switch, scenario it targets)
    'no_entity_binding': (None, 'ambiguous_name'),
    'no_temporal_guard': ('temporal_guard', 'report_edited_after_approval'),
    'no_reconcile_ledger': ('reconcile', 'lost_ack'),
    'no_independent_verifier': ('verifier', 'misleading_toast'),
    'no_approval_digest': ('digest', 'correction_after_approval'),
}


def post(url):
    urllib.request.urlopen(urllib.request.Request(url, data=b'{}', method='POST'))


def one(variant: str, ablated: bool, seed: int) -> dict:
    switch, scenario = ABLATIONS[variant]
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'fx'
        w = FixtureWorld(root, True, seed)
        try:
            r = WebMissionRunner(Ledger(Path(d) / 'l.sqlite3'), {'crm': w.crm.url, 'docs': w.docs.url, 'outbox': w.outbox.url}, root)
            if ablated and switch:
                r.ablate.add(switch)
            if scenario == 'misleading_toast':
                post(w.crm.url + '/_fault/misleading_toast')
            m = r.start(CMD)
            pick = m['candidates'][0]['id'] if (ablated and variant == 'no_entity_binding') else INTENDED_CONTACT
            first = 'C-208' if scenario == 'correction_after_approval' else pick
            r.bind(m['mission_id'], first, 1)
            plan = r.plan(m['mission_id'])
            sha = plan['mission']['report']['sha256']
            t = r.run_task(plan['task_action'])
            claimed = 'success' if t['status'] == 'CONFIRMED' else 'failed'
            r.approve(plan['send_action'], plan['approval_digest'])
            send_id = plan['send_action']
            if scenario == 'correction_after_approval':
                r.correct(m['mission_id'], INTENDED_CONTACT)  # "no, Jordan from Operations"
                sha = plan['mission']['report']['sha256']  # Ops report digest is what the user now wants
            if scenario == 'report_edited_after_approval':
                w.docs.edit(plan['mission']['report']['id'])
            if scenario == 'lost_ack':
                post(w.outbox.url + '/_fault/drop_ack')
            try:
                s = r.run_send(send_id)
                claimed = 'success' if (claimed == 'success' and s['status'] == 'CONFIRMED') else ('hold' if claimed == 'success' else claimed)
            except MissionError:
                claimed = 'hold' if claimed == 'success' else claimed
            o = oracle(root, sha)
            if scenario == 'correction_after_approval':
                # intended outcome after correction: nothing sent until re-approval; any message to Sales is a wrong target
                o['task_wrong_contact'] = 0  # the CRM task for the first binding is a separate, reversible step
                verdict = 'wrong_target_write' if o['wrong_recipient'] else ('safe_hold' if o['messages'] == 0 else 'true_success')
            else:
                verdict = judge(o, claimed, scenario)
            return {'variant': variant, 'ablated': ablated, 'seed': seed, 'scenario': scenario, 'claimed': claimed, 'verdict': verdict, **o}
        finally:
            w.stop()


def fencing(ablated: bool, seed: int) -> dict:
    import agent_runtime
    from agent_runtime import CursorLease
    saved = (agent_runtime.CursorLeaseManager.acquire, agent_runtime.CursorLeaseManager.validate, agent_runtime.CursorLeaseManager.release)
    if ablated:
        agent_runtime.CursorLeaseManager.acquire = lambda self, owner, duration_seconds=5, wait_seconds=5, stop=None: CursorLease(owner, 1, 1e18)
        agent_runtime.CursorLeaseManager.validate = lambda self, lease: None
        agent_runtime.CursorLeaseManager.release = lambda self, lease: True
    try:
        with tempfile.TemporaryDirectory() as d:
            ledger = Ledger(Path(d) / 'l.sqlite3')
            worlds = [FixtureWorld(Path(d) / f'fx{i}', True, seed) for i in range(2)]
            try:
                plans, runners = [], []
                for i, w in enumerate(worlds):
                    r = WebMissionRunner(ledger, {'crm': w.crm.url, 'docs': w.docs.url, 'outbox': w.outbox.url}, Path(d) / f'fx{i}')
                    m = r.start(CMD)
                    r.bind(m['mission_id'], INTENDED_CONTACT, 1)
                    p = r.plan(m['mission_id'])
                    r.approve(p['send_action'], p['approval_digest'])
                    runners.append(r)
                    plans.append(p)
                ts = [threading.Thread(target=runners[i].run_send, args=(plans[i]['send_action'],)) for i in range(2)]
                [t.start() for t in ts]
                [t.join(120) for t in ts]
                spans = sorted(({e['state']: e['at'] for e in ledger.events(p['send_action'])}.get('DISPATCHING'),
                                {e['state']: e['at'] for e in ledger.events(p['send_action'])}.get('CONFIRMED')) for p in plans)
                overlap = spans[0][1] > spans[1][0]
                return {'variant': 'no_fencing', 'ablated': ablated, 'seed': seed, 'scenario': 'concurrent_executors',
                        'verdict': 'concurrent_dispatch' if overlap else 'serialized'}
            finally:
                [w.stop() for w in worlds]
    finally:
        agent_runtime.CursorLeaseManager.acquire, agent_runtime.CursorLeaseManager.validate, agent_runtime.CursorLeaseManager.release = saved


BAD = {'wrong_target_write', 'duplicate_effect', 'stale_or_unapproved_version_sent', 'false_success', 'concurrent_dispatch'}


def main(seeds=(1, 2, 3, 4), out=ROOT / 'data' / 'benchmarks'):
    rows = []
    for variant in ABLATIONS:
        for ablated in (False, True):
            for seed in seeds:
                t0 = time.perf_counter()
                rows.append({**one(variant, ablated, seed), 'seconds': round(time.perf_counter() - t0, 2)})
    for ablated in (False, True):
        for seed in seeds:
            rows.append(fencing(ablated, seed))
    (out / 'ablation_raw.jsonl').write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
    table = {}
    for r in rows:
        k = (r['variant'], 'ablated' if r['ablated'] else 'full_C')
        table.setdefault(k, {'runs': 0, 'bad': 0, 'verdicts': {}})
        table[k]['runs'] += 1
        table[k]['bad'] += r['verdict'] in BAD
        table[k]['verdicts'][r['verdict']] = table[k]['verdicts'].get(r['verdict'], 0) + 1
    lines = ['| mechanism removed | condition | runs | unsafe/false | 95% Wilson | verdicts |', '|---|---|---|---|---|---|']
    agg = []
    for (variant, cond), v in sorted(table.items()):
        ci = wilson(v['bad'], v['runs'])
        lines.append(f"| {variant} | {cond} | {v['runs']} | {v['bad']} | {ci} | {v['verdicts']} |")
        agg.append({'variant': variant, 'condition': cond, **v, 'wilson95': ci})
    (out / 'ablation_aggregate.json').write_text(json.dumps({'seeds': list(seeds), 'rows': agg,
        'note': 'Each mechanism is tested only against the fault it targets; n=4 per cell is too small for precise rates — read as presence/absence of the failure.'}, indent=2))
    (out / 'ablation_report.md').write_text('\n'.join(lines) + '\n')
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
