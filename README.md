# FHIR Activity Plan Engine

A small Python engine that reads FHIR `PlanDefinition` and `ActivityDefinition` resources and turns them into patient-specific `ServiceRequest` orders, overdue `Flag` resources, and an HTML dashboard. The engine has no clinical logic of its own. The example artifact library implements the AAP Trisomy 21 (Down syndrome) screening guidelines so there is something concrete to run.

## What it does

You give it:

- a set of `PlanDefinition` resources (the screening protocols)
- a set of `ActivityDefinition` resources (the orderable actions)
- a patient with their `Observation`, `Procedure`, and `Encounter` history

It gives back:

- `ServiceRequest` resources for screenings that are due now
- `Flag` resources for windows that have already passed without being fulfilled
- an HTML dashboard showing the whole picture for one patient

The engine is stateless. Every run recomputes what should exist from scratch, so editing an artifact or fixing a bad observation takes effect on the next run with no migration step. Reconciliation against already-persisted requests happens only at write time.

## Why it is built this way

Most clinical decision support hard-codes the rules in application code. This one keeps them out of the engine. Timing windows, recurrence, applicability conditions, and code matching all live in FHIR knowledge artifacts under `fixtures/`. The engine only knows how to read FHIR.

That buys a few things. Changing a rule like "annual TSH" to "every 9 months for antibody-positive patients" is a JSON edit you can review in a pull request, with no code change. The same engine can run a different guideline set, such as primary care wellness or pre-op workup, by swapping the artifact library. And because every `ServiceRequest` it generates points back to the canonical URL of the artifacts behind it, you can always trace why something was ordered.

## How an artifact becomes an order

Here is the TSH plan from the example library, simplified:

```
PlanDefinition: aap-t21-tsh-schedule
  action: annual-tsh
    definitionCanonical -> ActivityDefinition: t21-tsh-screening (LOINC 11580-8)
    timingTiming: every 11-13 months through age 21
    condition (FHIRPath): no positive anti-thyroid antibody result exists
```

For a 4-year-old with no antibody-positive observations, the engine:

1. Works out the timing windows from the patient's date of birth
2. Checks the FHIRPath condition against the patient's observations (it passes)
3. For each window, looks for a matching `Observation` (LOINC 11580-8) already in that window
4. Generates a `ServiceRequest` for any window that is not fulfilled
5. Generates an overdue `Flag` for any window whose end date has already passed

## The dashboards

Two scripts render the engine output as HTML. Both are self-contained pages with no server and no build step. Open them in a browser.

`build_supervision_dashboard.py` is the clinical view. It runs the engine for one patient and produces a supervision dashboard from the `trisomy21_dashboard_v18.html` template, listing the orderable actions with their current status (due, overdue, or complete). This is the view the Epic app serves.

```bash
python build_supervision_dashboard.py patients/patient-1
# writes supervision_dashboard.patient-1.html
```

`debug_timeline.py` is the developer view. It loads every patient under `patients/`, runs the engine for each, and writes one timeline page with toggles to show or hide patients. Each lane is one `ActivityDefinition` and the bars are `ServiceRequest`s. It is the quickest way to check whether an artifact change did what you expected.

```bash
python debug_timeline.py
# writes debug_timeline.html
```

## Running it inside Epic

`smart-on-fhir/` puts the engine behind Epic in two ways: a SMART on FHIR app that shows a read-only supervision dashboard in the chart, and a CDS Hooks service that surfaces due and overdue screenings as orderable suggestions. The clinician accepts a suggestion and Epic places the signed order, so Epic stays the system of record. Both run against Epic's public sandbox, which is free and needs no Epic contract to try. See `smart-on-fhir/README.md` for how it works and how to register it on Epic.

## The example library: AAP Trisomy 21 screening

`fixtures/` holds 9 `ActivityDefinition` and 9 `PlanDefinition` resources covering the American Academy of Pediatrics screening recommendations for children with Down syndrome:

- TSH screening (newborn, 6 months, then annual or semi-annual depending on antibody status)
- Anti-thyroid antibody one-time screen
- Confirmatory karyotype (prenatal)
- CBC (newborn and annual through age 21)
- Transthoracic echocardiogram (newborn)
- Newborn hearing assessment
- Feeding assessment (triggered by hypotonia or low weight-for-length)
- Infant ophthalmology referral
- Early intervention referral

There is also a counseling artifact (`fixtures/counseling/`) for discussion topics, which the dashboard currently shows as static text rather than running through the engine. Swap `fixtures/` and `patients/` for a different guideline set and cohort and the engine runs unchanged.

## Quickstart

```bash
pip install fhir.resources fhirpathpy python-dateutil

# Build the supervision dashboard for one patient
python build_supervision_dashboard.py patients/patient-1
# then open supervision_dashboard.patient-1.html

# Or run the engine directly and print a summary to stdout
python logic.py patients/patient-1
```

## Project layout

```
fixtures/
  activity-definitions/   # 9 orderable actions (T21 example)
  plan-definitions/       # 9 screening protocols (T21 example)
  counseling/             # discussion topics (shown as static UI)
patients/
  patient-{1,2,3}/        # example patients with observations, procedures, encounters
logic.py                  # the engine: load_fixtures, apply_plan, reconcile_service_requests
build_supervision_dashboard.py  # builds the per-patient supervision dashboard
trisomy21_dashboard_v18.html    # dashboard template
debug_timeline.py         # builds the multi-patient debug timeline
smart-on-fhir/            # SMART on FHIR app + CDS Hooks service for Epic
```

## Tech stack

Python 3.10+, `fhir.resources` for typed FHIR R4B models (the same version Epic serves), `fhirpathpy` for FHIRPath evaluation, and `python-dateutil` for date math. The dashboards are plain HTML generated by Python, with no JavaScript build step.

## Status

Teaching and portfolio project. It demonstrates the artifact-driven CDS pattern end to end, but it is not for clinical use. The artifact library is illustrative, the FHIRPath conditions are simplified, and there is no security model in the engine itself.

## License

No license file yet. Add one if you plan to share it.
