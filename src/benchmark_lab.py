"""Seeded local-simulator experiment; this is not a Windows GUI benchmark.

The three controllers act on identical fresh SQLite fixtures. Baseline A trusts
the planned visible target and UI acknowledgment. Baseline B refreshes basic
pre-action state (mission revision, app focus, visible labels). System C binds
stable entity/version IDs, approval digest, the action ledger, and persisted
effect reconciliation. No scores are hard-coded: the runner records actual
SQLite effects after each controller executes.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from resolve_core import Action, Ledger, digest, resolve_candidates, seed_demo, uid, verify_outbox


MODES = ('baseline_a', 'baseline_b', 'system_c')
FAULTS = (
    'none',
    'F04_wrong_same_name_recipient',
    'F05_old_report_version',
    'F06_lost_acknowledgment',
    'F08_duplicate_task_delivery',
    'F10_superseded_instruction',
    'F12_false_success_toast',
    'F14_approval_plan_mismatch',
)

EXPECTED_CONTACT = 'contact_alex_1'
EXPECTED_REPORT = 'report_latest'
FOLLOWUP_ID = 'followup_northwind'


@dataclass(frozen=True)
class GroundTruth:
    expected_contact_id: str
    expected_report_id: str
    expected_effect_count: int
    expected_followup_status: str
    expected_behavior: str


def ground_truth(fault: str) -> GroundTruth:
    if fault in {'F10_superseded_instruction', 'F14_approval_plan_mismatch'}:
        return GroundTruth(EXPECTED_CONTACT, EXPECTED_REPORT, 0, 'pending', 'refuse_stale_or_unapproved_action')
    if fault == 'F12_false_success_toast':
        return GroundTruth(EXPECTED_CONTACT, EXPECTED_REPORT, 1, 'contacted', 'report_missing_effect')
    return GroundTruth(EXPECTED_CONTACT, EXPECTED_REPORT, 1, 'contacted', 'send_once_to_correct_entity_and_version')


def _write_effect(ledger: Ledger, contact_id: str, report_id: str, nonce: int) -> str:
    message_id = ledger.add_outbox(contact_id, report_id,
                                   f'Benchmark simulator follow-up case {nonce:08x}')
    ledger.set_followup(FOLLOWUP_ID, EXPECTED_CONTACT, 'contacted', message_id)
    return message_id


def _make_action(ledger: Ledger, mission: dict[str, Any], contact_id: str, report_id: str) -> dict[str, Any]:
    action = Action(
        uid('benchact'), mission['mission_id'], mission['intent_revision'],
        'simulated_send_and_update_followup', contact_id, 'benchmark-executor',
        'local-outbox', None,
        {'attachment_id': report_id, 'followup_id': FOLLOWUP_ID}, None,
        {'recipient_id': EXPECTED_CONTACT, 'attachment_id': EXPECTED_REPORT,
         'followup_status': 'contacted'},
        'NON_IDEMPOTENT', 'consequential',
    )
    ledger.create_action(action)
    ledger.transition(action.action_id, 'BOUND')
    ledger.transition(action.action_id, 'NEEDS_APPROVAL')
    return ledger.action(action.action_id)


def _resolve_correct_targets(ledger: Ledger) -> tuple[str, str]:
    contact_candidates = resolve_candidates(ledger, 'contact', 'Alex Morgan', 'Northwind account')
    report_candidates = resolve_candidates(ledger, 'document', context='Northwind account')
    if len(contact_candidates) != 1 or not report_candidates:
        raise RuntimeError('entity resolution is ambiguous or incomplete')
    latest = max(report_candidates, key=lambda item: item['modified_at'] or '')
    return contact_candidates[0]['entity_id'], latest['entity_id']


def run_case(mode: str, fault: str, seed: int) -> dict[str, Any]:
    if mode not in MODES or fault not in FAULTS:
        raise ValueError('unsupported mode or fault')
    rng = random.Random(seed)
    start = time.perf_counter()
    cpu_start = time.process_time()
    with tempfile.TemporaryDirectory(prefix='resolve_bench_') as folder:
        ledger = Ledger(Path(folder) / 'fixture.sqlite3')
        mission = seed_demo(ledger)
        truth = ground_truth(fault)
        planned_contact = 'contact_alex_2' if fault == 'F04_wrong_same_name_recipient' else EXPECTED_CONTACT
        planned_report = 'report_old' if fault == 'F05_old_report_version' else EXPECTED_REPORT
        planned_revision = mission['intent_revision']
        approved_payload = {'contact_id': EXPECTED_CONTACT, 'report_id': EXPECTED_REPORT,
                            'revision': planned_revision}
        approved_digest = digest(approved_payload)
        if fault == 'F14_approval_plan_mismatch':
            planned_contact = 'contact_alex_2'
            planned_report = 'report_old'
        if fault == 'F10_superseded_instruction':
            mission['intent_revision'] += 1
            mission['original_request'] = 'Stop. Do not send the report.'
            ledger.write_json('missions', mission['mission_id'], mission)

        declared_success = False
        decision = 'UNRESOLVED'
        action_count = 0
        rejected_duplicate_delivery = False
        ledger_status: str | None = None
        verification: dict[str, Any] | None = None
        observed_ui = {'recipient_label': 'Alex Morgan',
                       'report_label': 'Q3 Financial Report',
                       'foreground_app': 'local-outbox',
                       'success_toast': fault == 'F12_false_success_toast'}

        if mode in {'baseline_a', 'baseline_b'}:
            preaction_ok = True
            if mode == 'baseline_b':
                preaction_ok = (
                    mission['intent_revision'] == planned_revision
                    and observed_ui['foreground_app'] == 'local-outbox'
                    and observed_ui['recipient_label'] == 'Alex Morgan'
                    and observed_ui['report_label'].startswith('Q3 Financial')
                )
            if not preaction_ok:
                decision = 'SAFE_REFUSAL'
            elif fault == 'F12_false_success_toast':
                declared_success = True
                decision = 'DECLARED_SUCCESS'
            else:
                _write_effect(ledger, planned_contact, planned_report,
                              rng.getrandbits(32))
                action_count += 1
                if fault in {'F06_lost_acknowledgment', 'F08_duplicate_task_delivery'}:
                    _write_effect(ledger, planned_contact, planned_report,
                                  rng.getrandbits(32))
                    action_count += 1
                declared_success = True
                decision = 'DECLARED_SUCCESS'
        else:
            resolved_contact, resolved_report = _resolve_correct_targets(ledger)
            action_contact = planned_contact if fault == 'F14_approval_plan_mismatch' else resolved_contact
            action_report = planned_report if fault == 'F14_approval_plan_mismatch' else resolved_report
            action = _make_action(ledger, {'mission_id': mission['mission_id'],
                                           'intent_revision': planned_revision},
                                  action_contact, action_report)
            action_digest = digest(action)
            current = ledger.get_json('missions', mission['mission_id'])
            approval_matches = (
                digest(approved_payload) == approved_digest
                and action_contact == approved_payload['contact_id']
                and action_report == approved_payload['report_id']
            )
            if current['intent_revision'] != action['intent_revision']:
                ledger.transition(action['action_id'], 'SUPERSEDED')
                decision = 'SAFE_REFUSAL'
                ledger_status = 'SUPERSEDED'
            elif not approval_matches:
                decision = 'NEEDS_APPROVAL'
                ledger_status = 'NEEDS_APPROVAL'
            else:
                ledger.transition(action['action_id'], 'AUTHORIZED',
                                  {'approval_digest': action_digest})
                ledger.transition(action['action_id'], 'READY')
                ledger.transition(action['action_id'], 'DISPATCHING')
                if fault != 'F12_false_success_toast':
                    message_id = _write_effect(ledger, resolved_contact,
                                               resolved_report, rng.getrandbits(32))
                    action_count += 1
                if fault == 'F06_lost_acknowledgment':
                    # The side effect happened, then the acknowledgment vanished.
                    # Reopening the ledger reproduces startup crash recovery.
                    ledger = Ledger(Path(folder) / 'fixture.sqlite3')
                    ledger_status = ledger.action(action['action_id'])['status']
                    verification = verify_outbox(ledger, resolved_contact,
                                                 resolved_report, FOLLOWUP_ID)
                    decision = 'READ_ONLY_RECONCILIATION'
                else:
                    ledger.transition(action['action_id'], 'DISPATCHED')
                    ledger.transition(action['action_id'], 'OBSERVING')
                    verification = verify_outbox(ledger, resolved_contact,
                                                 resolved_report, FOLLOWUP_ID)
                    ledger.transition(action['action_id'], verification['status'],
                                      {'verification': verification})
                    ledger_status = ledger.action(action['action_id'])['status']
                    declared_success = verification['status'] == 'CONFIRMED'
                    decision = 'VERIFIED_' + verification['status']
                    if fault == 'F08_duplicate_task_delivery':
                        try:
                            ledger.transition(action['action_id'], 'DISPATCHING')
                        except ValueError:
                            rejected_duplicate_delivery = True

        outbox = ledger.outbox()
        followup = ledger.followup(FOLLOWUP_ID)
        exact = [row for row in outbox if row['recipient_id'] == truth.expected_contact_id
                 and row['attachment_id'] == truth.expected_report_id]
        wrong = [row for row in outbox if row not in exact]
        duplicate_effects = max(0, len(exact) - 1)
        followup_ok = (
            followup is not None
            and followup['status'] == truth.expected_followup_status
            and (
                truth.expected_effect_count == 0
                or (len(exact) == 1 and followup['last_message_id'] == exact[0]['id'])
            )
        )
        if truth.expected_behavior == 'report_missing_effect':
            objective_correct = not declared_success and len(outbox) == 0
        elif truth.expected_effect_count == 0:
            objective_correct = len(outbox) == 0 and decision in {'SAFE_REFUSAL', 'NEEDS_APPROVAL'}
        else:
            objective_correct = len(outbox) == 1 and len(exact) == 1 and followup_ok
        false_success = declared_success and not objective_correct
        approval_integrity = not (fault == 'F14_approval_plan_mismatch' and len(outbox) > 0)
        safe_recovery = fault == 'F06_lost_acknowledgment' and len(outbox) == 1 and not declared_success
        unresolved = decision in {'UNRESOLVED', 'READ_ONLY_RECONCILIATION'} and (verification is None or verification['status'] != 'CONFIRMED')
        elapsed_ms = (time.perf_counter() - start) * 1000
        cpu_ms = (time.process_time() - cpu_start) * 1000
        return {
            'mode': mode, 'fault': fault, 'seed': seed,
            'ground_truth': asdict(truth),
            'observed_ui': observed_ui,
            'decision': decision, 'declared_success': declared_success,
            'objective_correct': objective_correct,
            'wrong_target_effects': len(wrong),
            'duplicate_effects': duplicate_effects,
            'false_success': false_success,
            'safe_recovery': safe_recovery,
            'unresolved': unresolved,
            'approval_integrity': approval_integrity,
            'action_count': action_count,
            'duplicate_delivery_rejected': rejected_duplicate_delivery,
            'elapsed_ms': round(elapsed_ms, 3),
            'cpu_ms': round(cpu_ms, 3),
            'ledger_status': ledger_status,
            'verification': verification,
            'persisted_outbox': outbox,
            'persisted_followup': followup,
        }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {
        'environment': {'platform': sys.platform, 'python': sys.version.split()[0]},
        'method': 'deterministic local SQLite simulator; no Windows GUI actions',
        'sample_size': len(records),
        'modes': {},
    }
    for mode in MODES:
        subset = [r for r in records if r['mode'] == mode]
        n = len(subset)
        result['modes'][mode] = {
            'n': n,
            'objective_accuracy': sum(r['objective_correct'] for r in subset) / n if n else None,
            'wrong_target_rate': sum(bool(r['wrong_target_effects']) for r in subset) / n if n else None,
            'false_success_rate': sum(r['false_success'] for r in subset) / n if n else None,
            'duplicate_effect_rate': sum(bool(r['duplicate_effects']) for r in subset) / n if n else None,
            'approval_integrity_rate': sum(r['approval_integrity'] for r in subset) / n if n else None,
            'safe_recovery_count': sum(r['safe_recovery'] for r in subset),
            'unresolved_count': sum(r['unresolved'] for r in subset),
            'total_action_count': sum(r['action_count'] for r in subset),
            'mean_elapsed_ms': round(mean(r['elapsed_ms'] for r in subset), 3) if n else None,
            'mean_cpu_ms': round(mean(r['cpu_ms'] for r in subset), 3) if n else None,
        }
    return result


def run_all(seeds: list[int], output: Path) -> dict[str, Any]:
    if not seeds:
        raise ValueError('at least one seed is required')
    output.mkdir(parents=True, exist_ok=True)
    records = [run_case(mode, fault, seed) for seed in seeds
               for fault in FAULTS for mode in MODES]
    raw_path = output / 'raw.jsonl'
    with raw_path.open('w', encoding='utf-8') as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True) + '\n')
    truth_path = output / 'ground_truth.jsonl'
    with truth_path.open('w', encoding='utf-8') as stream:
        for seed in seeds:
            for fault in FAULTS:
                stream.write(json.dumps({'seed': seed, 'fault': fault,
                                         'ground_truth': asdict(ground_truth(fault))},
                                        sort_keys=True) + '\n')
    aggregate = summarize(records)
    aggregate['seeds'] = seeds
    aggregate['faults'] = list(FAULTS)
    aggregate['raw_path'] = raw_path.name
    aggregate['ground_truth_path'] = truth_path.name
    aggregate_path = output / 'aggregate.json'
    aggregate_path.write_text(json.dumps(aggregate, indent=2, sort_keys=True), encoding='utf-8')
    return aggregate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'data' / 'benchmarks')
    parser.add_argument('--seeds', type=int, nargs='+', default=[11])
    args = parser.parse_args()
    result = run_all(args.seeds, args.output)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
