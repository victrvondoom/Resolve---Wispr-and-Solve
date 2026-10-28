| mechanism removed | condition | runs | unsafe/false | 95% Wilson | verdicts |
|---|---|---|---|---|---|
| no_approval_digest | ablated | 4 | 4 | (0.51, 1) | {'wrong_target_write': 4} |
| no_approval_digest | full_C | 4 | 0 | (0, 0.49) | {'safe_hold': 4} |
| no_entity_binding | ablated | 4 | 2 | (0.15, 0.85) | {'wrong_target_write': 2, 'true_success': 2} |
| no_entity_binding | full_C | 4 | 0 | (0, 0.49) | {'true_success': 4} |
| no_fencing | ablated | 4 | 4 | (0.51, 1) | {'concurrent_dispatch': 4} |
| no_fencing | full_C | 4 | 0 | (0, 0.49) | {'serialized': 4} |
| no_independent_verifier | ablated | 4 | 4 | (0.51, 1) | {'false_success': 4} |
| no_independent_verifier | full_C | 4 | 0 | (0, 0.49) | {'honest_failure_or_hold': 4} |
| no_reconcile_ledger | ablated | 4 | 4 | (0.51, 1) | {'duplicate_effect': 4} |
| no_reconcile_ledger | full_C | 4 | 0 | (0, 0.49) | {'true_success': 4} |
| no_temporal_guard | ablated | 4 | 4 | (0.51, 1) | {'stale_or_unapproved_version_sent': 4} |
| no_temporal_guard | full_C | 4 | 0 | (0, 0.49) | {'safe_hold': 4} |
