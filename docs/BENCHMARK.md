# Local simulator benchmark

The first RESOLVE experiment runs three controller policies against identical, fresh SQLite CRM/report/outbox fixtures. It executes real writes into each fixture and derives outcomes from the persisted outbox and follow-up record. It does **not** drive a Windows GUI or measure vision grounding, microphone accuracy, or external email delivery.

## Run

```powershell
.\scripts\benchmark.ps1
# or: python .\src\benchmark_lab.py --output .\data\benchmarks --seeds 11
```

The runner writes [raw.jsonl](../data/benchmarks/raw.jsonl), [ground_truth.jsonl](../data/benchmarks/ground_truth.jsonl), and [aggregate.json](../data/benchmarks/aggregate.json). Ground truth is persisted once per fault/seed apart from the controller results. Each raw row includes mode, injected fault, seed, visible UI signals, controller decision, ledger and verifier status, and the actual persisted simulator records. The generated SQLite fixtures are temporary and are removed after each case.

## Policies and injected faults

| Policy | Behavior in this experiment |
|---|---|
| Baseline A | Acts on the planned visible target and accepts UI acknowledgment. It has no action revision check or effect reconciliation. |
| Baseline B | Rechecks mission revision, foreground application, and visible labels immediately before action. Same-name contacts and similar report titles still pass. It accepts UI acknowledgment. |
| System C | Resolves contact by client context and stable ID, selects report by modified time, checks the action against reviewed approval, writes ledger transitions before dispatch, refuses stale plans, and checks persisted outbox/follow-up effects. |

The eight targeted conditions are a clean run plus F04 wrong same-name recipient, F05 old report version, F06 lost acknowledgment, F08 duplicate task delivery, F10 superseded instruction, F12 false success toast, and F14 approval applied to a changed plan. These are protocol-level simulator faults. In F06, the system effect is present but the acknowledgment is lost after dispatch. The restarted ledger remains `UNCERTAIN`; separate read-only reconciliation finds the single persisted effect. A second dispatch is not attempted.

## Measured result

The checked-in run used seed 11 on Windows with Python 3.14.7: 24 cases total, eight per policy. [aggregate.json](../data/benchmarks/aggregate.json) is the machine-readable source of the current figures.

| Metric | Baseline A | Baseline B | System C |
|---|---:|---:|---:|
| Correct outcome across eight targeted conditions | 1/8 | 2/8 | 8/8 |
| Cases with a wrong-target effect | 3/8 | 3/8 | 0/8 |
| Cases falsely declared successful | 7/8 | 6/8 | 0/8 |
| Cases with a duplicate effect | 2/8 | 2/8 | 0/8 |
| Approval integrity | 7/8 | 7/8 | 8/8 |
| Safe lost-ack recovery | 0 | 0 | 1 |

The sample is deliberately small and fault-targeted. These fractions are descriptive counts for this simulator, not estimates of general computer-use reliability. No confidence interval is reported. The controller policies are simplified, and the baseline comparison does not prove algorithmic novelty. Wall-clock and CPU time in the aggregate include SQLite fixture setup, so they are unsuitable as desktop interaction latency claims. No paid model cost or memory-use result was measured.

## Interpretation and next falsification

The result establishes that the implemented checks prevent the injected local simulator failures under their stated conditions. It does not establish that entity graphs outperform simpler stable keys, that UIA or a basic verifier could not solve the same cases, or that the guarded system remains reliable under live focus theft and delayed GUI commits. The next benchmark should run real instrumented windows and ablate entity binding, temporal guards, effect reconciliation, cursor arbitration, and independent verification independently.
