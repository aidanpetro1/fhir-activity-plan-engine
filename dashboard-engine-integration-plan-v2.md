# Supervision Dashboard ↔ Engine Integration — v2 (decisions locked, generator built & validated)

**Status:** decisions settled; generator implemented and validated across patient-1/2/3; karyotype data issues resolved.
**Supersedes:** dashboard-engine-integration-plan.md (v1, deleted).
**Last updated:** 2026-05-27

---

## Decisions (locked)

1. **No section separation.** Orderables show as a flat list — no body-system grouping. No taxonomy added to the artifacts.
2. **Orderables only.** Integration targets the orderable `ActivityDefinition`/`PlanDefinition` pairs in `fixtures/`. Counseling/discussion items are out of scope for engine-backing.
3. **Counseling stays a static placeholder.** The "Discuss today" card remains hand-authored UI, to be modeled later (those items aren't `ServiceRequest` orders).
4. **Ordering is read-only.** "Order all" is a placeholder. Real ordering will be wired through **Epic** later (expected SMART-on-FHIR launch + write `ServiceRequest` via Epic's FHIR API). No local write-back server.

## What was built

- **`trisomy21_dashboard_v18.html`** — dashboard template, flattened to a single orderables list, with `/* DATA-START */ … /* DATA-END */` sentinels around the data arrays. Works standalone.
- **`build_supervision_dashboard.py`** — the generator (mirrors `debug_timeline.py`). Run: `python build_supervision_dashboard.py patients/patient-1`. Runs `logic.apply_plan`, derives a current status per orderable, maps to the dashboard `ITEMS` schema, injects between the sentinels, sets `DOB`, and writes `supervision_dashboard.<patient>.html`.

## Status-derivation rule (per ActivityDefinition)

From the engine's `ServiceRequest`s, `Flag`s, and the patient's `Observation`s:

1. **overdue** — has a window whose id matches an active overdue `Flag`.
2. **due** — else, has an unfulfilled window currently open (start ≤ today ≤ end).
3. **complete** — else, a code-matching `Observation` exists (carry its date + value).
4. **na** — else (excluded by condition, or only future windows). `na` items are hidden.

`b` is `"order"` for overdue/due; `complete` items render read-only on the Historical tab.

## Field mapping (engine → `ITEMS`)

| Dashboard field | Source |
|---|---|
| `n` | `ActivityDefinition.title` |
| `st` | derived per the rule above |
| `b` | `"order"` for overdue/due, else `null` |
| `d` / `r` | fulfilling `Observation.effectiveDateTime` / formatted value |
| `g` | `ActivityDefinition.description` + referencing `PlanDefinition.description` |
| `s` | empty (flat list) |

## Validated output (run 2026-05-27)

The same generator produces coherent, differing output across three patient histories:

- **patient-1** (~17 mo, antibody-positive): 6 active / 2 historical. Anti-thyroid antibody (45 IU/mL) and karyotype (Complete trisomy 21) complete; CBC, hearing, ophthalmology, TSH, echo overdue; early intervention due.
- **patient-2** (~11 mo, nothing done): 8 active / 0 historical. Karyotype **overdue** (no result on file → still wanted). Antibody, CBC, EI due; hearing, ophthalmology, TSH, echo overdue.
- **patient-3** (~5 yr, antibody-negative 2.5): 5 active / 2 historical. Feeding Assessment **due** (exercises the condition-triggered path); antibody + karyotype complete; CBC, EI, hearing, TSH overdue.

All three pass JS validation; tab/order/undo logic verified in a JS sandbox.

## Resolved (was "Data issues")

- **Karyotype code mismatch — FIXED.** The karyotype observation is LOINC `29770-5` but the ActivityDefinition was SNOMED `117010004`, so the engine couldn't link them. Aligned the AD's primary code to LOINC `29770-5` (SNOMED kept as a secondary coding). This also matches the code the karyotype applicability condition already expected — confirming LOINC was the intended code. patient-1/3 karyotype now correctly shows complete.
- **Karyotype condition scope — BROADENED.** The applicability condition previously suppressed re-ordering only for a value of full trisomy 21 (`41040004`); translocation or mosaic karyotypes would have wrongly triggered a re-order. Now any resulted karyotype Observation (LOINC `29770-5` with any `valueCodeableConcept`) suppresses re-ordering. Verified with synthetic translocation/mosaic cases; the three real patients are unchanged.

## Open / deferred

- **Coverage:** only the ~9 engine-backed orderables are live; the broader supervision set (PSG, celiac, autism screen, etc.) is unmodeled. Grow the artifact library incrementally.
- **Recurring done-and-due:** the one-row-per-action model collapses status; an action screened once but due again shows "due" and drops the prior result. Extend the row model if last-result + next-due both matter.
- **Counseling items:** still static; revisit FHIR modeling (Communication/Task vs ServiceRequest) later.
- **Ordering:** revisit when the Epic integration path is defined.
- **Section grouping:** intentionally dropped; revisit only if a grouped view is wanted.
