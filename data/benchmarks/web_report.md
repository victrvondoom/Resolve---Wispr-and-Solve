| policy | runs | unsafe_or_false | 95% Wilson | verdicts |
|---|---|---|---|---|
| A_naive | 24 | 21 | (0.69, 0.957) | {'wrong_target_write': 12, 'true_success': 3, 'stale_or_unapproved_version_sent': 3, 'duplicate_effect': 3, 'false_success': 3} |
| B_target_guard | 24 | 18 | (0.551, 0.88) | {'true_success': 6, 'stale_or_unapproved_version_sent': 6, 'duplicate_effect': 6, 'false_success': 6} |
| C_ieec_csl | 24 | 0 | (0, 0.138) | {'true_success': 12, 'safe_hold': 6, 'honest_failure_or_hold': 6} |
