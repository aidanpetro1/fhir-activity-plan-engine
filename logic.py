import json
import sys
from pathlib import Path
from datetime import date
from dateutil.relativedelta import relativedelta

from fhir.resources.R4B.plandefinition import PlanDefinition
from fhir.resources.R4B.activitydefinition import ActivityDefinition
from fhir.resources.R4B.patient import Patient
from fhir.resources.R4B.servicerequest import ServiceRequest
from fhir.resources.R4B.observation import Observation
from fhir.resources.R4B.procedure import Procedure
from fhir.resources.R4B.encounter import Encounter
from fhir.resources.R4B.flag import Flag

from fhirpathpy import compile


# Constant FHIRPath expressions are compiled once at import time so we don't
# pay the compile cost on every patient.
_GET_DOB = compile("Patient.birthDate")


# --- Loaders ---

def load_fixtures(fixtures_dir):
    """Load PlanDefinitions and ActivityDefinitions from fixture directories.

    Also precompiles every FHIRPath applicability condition found on plan
    actions, returning them in a dict keyed by ``(plan.id, action.id)``. This
    means apply_plan never calls ``compile()`` in its hot path, and a
    malformed FHIRPath expression fails loudly at fixture-load time rather
    than the first patient unlucky enough to trigger it — which is the
    behavior we want for clinical knowledge artifacts.

    Returns:
        (activity_definitions, plan_definitions, compiled_conditions)
    """
    base_path = Path(fixtures_dir)

    plan_definitions = {}
    activity_definitions = {}

    for f in (base_path / "activity-definitions").glob("*.json"):
        with open(f) as file:
            r = ActivityDefinition(**json.load(file))
            activity_definitions[r.id] = r

    for f in (base_path / "plan-definitions").glob("*.json"):
        with open(f) as file:
            r = PlanDefinition(**json.load(file))
            plan_definitions[r.id] = r

    # Precompile all applicability conditions. We mirror apply_plan and only
    # walk top-level plan.action entries (no nested action[] traversal).
    compiled_conditions = {}
    for plan in plan_definitions.values():
        for action in (plan.action or []):
            if not action.condition:
                continue
            expr_str = action.condition[0].expression.expression
            try:
                compiled_conditions[(plan.id, action.id)] = compile(expr_str)
            except Exception as e:
                raise ValueError(
                    f"Failed to compile FHIRPath in PlanDefinition "
                    f"'{plan.id}', action '{action.id}': {expr_str!r} ({e})"
                )

    return activity_definitions, plan_definitions, compiled_conditions


def load_patient_data(patient_dir):
    """Load a Patient resource and associated Observations from a patient directory."""
    base_path = Path(patient_dir)

    with open(base_path / "patient.json") as file:
        patient = Patient(**json.load(file))

    observations = {}
    for f in (base_path / "observations").glob("*.json"):
        with open(f) as file:
            r = Observation(**json.load(file))
            observations[r.id] = r

    procedures = {}
    for f in (base_path / "procedures").glob("*.json"):
        with open(f) as file:
            r = Procedure(**json.load(file))
            procedures[r.id] = r

    encounters = {}
    enc_dir = base_path / "encounters"
    if enc_dir.exists():
        for f in enc_dir.glob("*.json"):
            with open(f) as file:
                r = Encounter(**json.load(file))
                encounters[r.id] = r

    return patient, observations, procedures, encounters


# --- Helpers ---

def build_fhirpath_context(observations,procedures):
    """Build a context dict for FHIRPath condition evaluation."""
    return {
        "resourceType": "Bundle",
        "Observation": [obs.dict() for obs in observations.values()],
        "Procedure": [proc.dict() for proc in procedures.values()]
    }

def evaluate_condition(check, context):
    """Evaluate a precompiled FHIRPath applicability condition against an
    observation context. ``check`` is the compiled callable produced by
    ``load_fixtures``. Returns True if the action should apply, False if it
    should be skipped."""
    result = check({}, {"observations": context["Observation"], "procedures": context["Procedure"]})
    return bool(result and result[0])


def generate_recurring_dates(initial_low, initial_high, timing, dob):
    """Generate recurring date windows from a timingTiming repeat definition.
    Returns a list of (low, high) tuples including the initial occurrence."""
    period = int(timing.repeat.period)
    period_max = int(timing.repeat.periodMax)
    bounds_months = int(timing.repeat.boundsDuration.value)

    end_date = dob + relativedelta(months=bounds_months)
    window_width = period_max - period

    current_low = initial_low
    occurrences = [(initial_low, initial_high)]

    while current_low <= end_date:
        current_low = current_low + relativedelta(months=period)
        current_high = current_low + relativedelta(months=window_width)
        occurrences.append((current_low, current_high))

    return occurrences

def has_observation_in_window(observations, code, low=None, high=None):
    """Check if an observation with the given code exists.
    If low and high are provided, the observation must fall within that window.
    If omitted, any observation with the matching code at any date counts."""
    for obs in observations.values():
        obs_codes = {c.code for c in (obs.code.coding or []) if c.code}
        if code not in obs_codes:
            continue
        if low is not None and high is not None:
            if obs.effectiveDateTime and low <= obs.effectiveDateTime <= high:
                return True
        else:
            if obs.effectiveDateTime:
                return True
    return False

def has_procedure_in_window(procedures, code, low=None, high=None):
    """Check if a procedure with the given code exists.
    If low and high are provided, the procedure must fall within that window.
    If omitted, any procedure with the matching code at any date counts."""
    for proc in procedures.values():
        proc_codes = {c.code for c in (proc.code.coding or []) if c.code}
        if code not in proc_codes:
            continue
        occurred = proc.performedDateTime or (proc.performedPeriod.start if proc.performedPeriod else None)
        if low is not None and high is not None:
            if occurred and low <= occurred <= high:
                return True
        else:
            if occurred:
                return True
    return False

def has_encounter_in_window(encounters, code, low=None, high=None):
    """Check if an encounter with the given code exists in its type coding.
    Uses period.start (R4) for the encounter date.
    If low and high are provided, the encounter must fall within that window.
    If omitted, any encounter with the matching code at any date counts."""
    for enc in encounters.values():
        for type_cc in (enc.type or []):
            enc_codes = {c.code for c in (type_cc.coding or []) if c.code}
            if code not in enc_codes:
                continue
            enc_date = enc.period.start if enc.period else None
            if low is not None and high is not None:
                if enc_date and low <= enc_date <= high:
                    return True
            else:
                if enc_date:
                    return True
    return False

# --- Core Logic ---

def apply_plan(patient, activity_definitions, plan_definitions, compiled_conditions,
               observations=None, procedures=None, encounters=None):
    """Apply PlanDefinitions to a patient, generating scheduled ServiceRequests.
    Evaluates FHIRPath conditions against observations to determine which
    actions apply.

    ``compiled_conditions`` is the dict of precompiled FHIRPath callables
    returned by ``load_fixtures``, keyed by ``(plan.id, action.id)``."""

    dob = _GET_DOB(patient.dict())[0]
    today = date.today()

    scheduled_dates = {}
    context = build_fhirpath_context(observations or {}, procedures or {})

    activity_def_by_url = {ad.url: ad for ad in activity_definitions.values()}

    for plan in plan_definitions.values():
        for activity in plan.action:

            # Evaluate applicability conditions
            condition_text = None
            if activity.condition:
                check = compiled_conditions[(plan.id, activity.id)]
                if not evaluate_condition(check, context):
                    continue
                condition_text = activity.condition[0].expression.description

            # Calculate initial date window
            if activity.timingRange:
                low_value = dob + relativedelta(months=int(activity.timingRange.low.value))
                high_value = dob + relativedelta(months=int(activity.timingRange.high.value))
                scheduled_dates[activity.id] = {
                    "canonical": activity.definitionCanonical,
                    "conditionText": condition_text,
                    "occurrences": [(low_value, high_value)]
                }

            elif activity.relatedAction:
                ref = activity.relatedAction[0]
                ref_dates = scheduled_dates[ref.actionId]["occurrences"][0]
                low_value = ref_dates[0] + relativedelta(months=int(ref.offsetRange.low.value))
                high_value = ref_dates[0] + relativedelta(months=int(ref.offsetRange.high.value))
                scheduled_dates[activity.id] = {
                    "canonical": activity.definitionCanonical,
                    "conditionText": condition_text,
                    "occurrences": [(low_value, high_value)]
                }

            else:
                # Condition-only action with no time window — applies now if condition passed
                scheduled_dates[activity.id] = {
                    "canonical": activity.definitionCanonical,
                    "conditionText": condition_text,
                    "anyTime": True,
                    "occurrences": [(today, today)]
                }

            # Generate recurring occurrences
            if activity.timingTiming:
                initial = scheduled_dates[activity.id]["occurrences"][0]
                occurrences = generate_recurring_dates(initial[0], initial[1], activity.timingTiming, dob)
                scheduled_dates[activity.id] = {
                    "canonical": activity.definitionCanonical,
                    "conditionText": condition_text,
                    "occurrences": occurrences
                }

    # Generate ServiceRequests and Flags from scheduled dates
    service_requests = []
    flags = []

    for action_id, data in scheduled_dates.items():
        act_def = activity_def_by_url[data["canonical"]]

        for i, (low, high) in enumerate(data["occurrences"]):

            code = act_def.code.coding[0].code
            _encounters = encounters or {}
            if data.get("anyTime"):
                if (has_observation_in_window(observations, code)
                        or has_procedure_in_window(procedures, code)
                        or has_encounter_in_window(_encounters, code)):
                    continue
            else:
                if (has_observation_in_window(observations, code, low, high)
                        or has_procedure_in_window(procedures, code, low, high)
                        or has_encounter_in_window(_encounters, code, low, high)):
                    continue

            sr_note = [{"text": data["conditionText"]}] if data.get("conditionText") else None
            service_request = ServiceRequest(
                id=f"{action_id}-{i}",
                status="active",
                intent="order",
                instantiatesCanonical=[data["canonical"]],
                code=act_def.code.dict(),
                subject={"reference": f"Patient/{patient.id}"},
                occurrencePeriod={"start": low.isoformat(), "end": high.isoformat()},
                note=sr_note
            )
            service_requests.append(service_request)

            # Generate overdue Flag if the window has passed
            if high < today:
                flag = Flag(
                    id=f"{action_id}-{i}",
                    status="active",
                    category=[{
                        "coding": [{
                            "system": "http://terminology.hl7.org/CodeSystem/flag-category",
                            "code": "admin",
                            "display": "Administrative"
                        }]
                    }],
                    code={
                        "coding": [{
                            "system": "http://snomed.info/sct",
                            "code": "441586009",
                            "display": "Overdue for screening"
                        }],
                        "text": f"Overdue: {act_def.code.text or act_def.title}"
                    },
                    subject={"reference": f"Patient/{patient.id}"},
                    period={"start": high.isoformat()},
                    extension=[{
                        "url": "https://t21app.example.org/fhir/StructureDefinition/flag-triggering-resource",
                        "valueReference": {"reference": f"ServiceRequest/{action_id}-{i}"}
                    }]
                )
                flags.append(flag)

    return service_requests, scheduled_dates, flags


# --- Persistence ---

def reconcile_service_requests(generated_srs, flags, patient_dir):
    """Reconcile generated ServiceRequests against persisted ones on disk.
    - New SRs (generated but not on disk) are written as active.
    - Fulfilled SRs (on disk but no longer generated) are marked completed.
    - Existing active SRs that were regenerated are left as-is.
    Returns (all_srs, active_flags) where active_flags only includes flags for still-active SRs."""

    sr_dir = Path(patient_dir) / "service-requests"
    sr_dir.mkdir(exist_ok=True)

    # Load existing SRs from disk
    existing_srs = {}
    for f in sr_dir.glob("*.json"):
        with open(f) as file:
            sr = ServiceRequest(**json.load(file))
            existing_srs[sr.id] = sr

    generated_ids = {sr.id for sr in generated_srs}
    existing_ids = set(existing_srs.keys())

    # New SRs: generated but not yet on disk → write as active
    for sr in generated_srs:
        if sr.id not in existing_ids:
            with open(sr_dir / f"{sr.id}.json", "w") as file:
                json.dump(json.loads(sr.json()), file, indent=2, default=str)
            existing_srs[sr.id] = sr

    # Fulfilled SRs: on disk and active, but not regenerated → mark completed
    for sr_id in existing_ids:
        if sr_id not in generated_ids and existing_srs[sr_id].status == "active":
            existing_srs[sr_id].status = "completed"
            with open(sr_dir / f"{sr_id}.json", "w") as file:
                json.dump(json.loads(existing_srs[sr_id].json()), file, indent=2, default=str)

    # Filter flags to only include those for still-active SRs
    active_ids = {sr_id for sr_id, sr in existing_srs.items() if sr.status == "active"}
    active_flags = [f for f in flags if f.extension and
                    f.extension[0].valueReference.reference.split("/")[-1] in active_ids]

    return list(existing_srs.values()), active_flags


# --- Entry point ---

if __name__ == "__main__":
    # Default paths — override patient with command line argument
    fixtures_path = "C:/T21/fixtures"
    patient_path = sys.argv[1] if len(sys.argv) > 1 else "C:/T21/patients/patient-1"

    activity_definitions, plan_definitions, compiled_conditions = load_fixtures(fixtures_path)
    patient, observations, procedures, encounters = load_patient_data(patient_path)
    service_requests, scheduled_dates, flags = apply_plan(
        patient, activity_definitions, plan_definitions, compiled_conditions,
        observations, procedures, encounters
    )

    # Reconcile against persisted SRs
    all_srs, active_flags = reconcile_service_requests(service_requests, flags, patient_path)
    active_srs = [sr for sr in all_srs if sr.status == "active"]
    completed_srs = [sr for sr in all_srs if sr.status == "completed"]

    print(f"Patient: {patient.name[0].given[0]} {patient.name[0].family}")
    print(f"ServiceRequests: {len(active_srs)} active, {len(completed_srs)} completed")
    print(f"Overdue flags: {len(active_flags)}")
    for f in active_flags:
        print(f"  {f.code.text} (overdue since {f.period.start})")