"""Patient data sources for the engine.

The engine (logic.py) and the knowledge artifacts are now **FHIR R4(B)**, the same
version Epic serves. This module hides where the data comes from behind one
interface so the engine and dashboard never change:

    LocalFixtureSource(patient_dir).load()   ->  offline dev / demo (no Epic)
    EpicFhirSource(base, token, pid).load()  ->  live Epic R4 via SMART

Both return the 4-tuple logic.apply_plan expects:

    (patient, observations, procedures, encounters)

where observations/procedures/encounters are {id: <fhir.resources R4B model>} dicts.

Because the engine is R4-native, EpicFhirSource no longer renames any fields. It
just builds *minimal* R4B-valid models from Epic's search results (Epic resources
carry many extra fields/extensions; constructing minimal models keeps the engine
input clean and predictable) and truncates dates to YYYY-MM-DD to match the engine's
date-only window comparisons.
"""
from config import config  # imported first so the repo root is on sys.path for `import logic`

import requests

from fhir.resources.R4B.patient import Patient
from fhir.resources.R4B.observation import Observation
from fhir.resources.R4B.procedure import Procedure
from fhir.resources.R4B.encounter import Encounter

import logic  # the engine, imported from the repo root (path set up by config)

REQUEST_TIMEOUT = 30
MAX_PAGES = 10  # safety cap when following Bundle `next` links

# Value[x] elements the engine / dashboard may read.
_VALUE_KEYS = ("valueQuantity", "valueCodeableConcept", "valueString",
               "valueBoolean", "valueInteger")


# --------------------------------------------------------------------------- #
# Local fixtures (no Epic needed)
# --------------------------------------------------------------------------- #
class LocalFixtureSource:
    """Reads a patient folder from the repo's patients/ directory (R4 fixtures)."""

    def __init__(self, patient_dir):
        self.patient_dir = str(patient_dir)

    def load(self):
        return logic.load_patient_data(self.patient_dir)


# --------------------------------------------------------------------------- #
# Epic FHIR R4 (live, via a SMART access token)
# --------------------------------------------------------------------------- #
def _trunc(s):
    """Truncate a FHIR date/dateTime/instant to YYYY-MM-DD (or None)."""
    return s[:10] if isinstance(s, str) and len(s) >= 10 else (s or None)


class EpicFhirSource:
    def __init__(self, fhir_base, access_token, patient_id, *,
                 observation_categories=None, verify_tls=True):
        self.base = fhir_base.rstrip("/")
        self.token = access_token
        self.pid = patient_id
        self.categories = observation_categories or ["laboratory"]
        self.verify = verify_tls

    # ---- HTTP ----
    def _headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/fhir+json",
        }

    def _get(self, path, params=None):
        url = path if path.startswith("http") else f"{self.base}/{path}"
        r = requests.get(url, headers=self._headers(), params=params,
                         timeout=REQUEST_TIMEOUT, verify=self.verify)
        r.raise_for_status()
        return r.json()

    def _search(self, resource_type, params):
        """Run a search and follow `next` links, returning a list of resources."""
        out, bundle, pages = [], self._get(resource_type, params), 0
        while bundle and pages < MAX_PAGES:
            for entry in bundle.get("entry", []):
                res = entry.get("resource")
                if res and res.get("resourceType") == resource_type:
                    out.append(res)
            nxt = next((l["url"] for l in bundle.get("link", [])
                        if l.get("relation") == "next" and l.get("url")), None)
            if not nxt:
                break
            bundle = self._get(nxt)
            pages += 1
        return out

    # ---- Normalizers: Epic R4 resource dict -> minimal R4B-valid model ----
    def _subject(self, res):
        return res.get("subject") or {"reference": f"Patient/{self.pid}"}

    def _norm_patient(self, res):
        return Patient(**{
            "resourceType": "Patient",
            "id": res.get("id", self.pid),
            "birthDate": res.get("birthDate"),
            "name": res.get("name") or [{"family": "Patient", "given": ["Unknown"]}],
        })

    def _norm_observation(self, res):
        eff = (res.get("effectiveDateTime")
               or (res.get("effectivePeriod") or {}).get("start")
               or res.get("effectiveInstant"))
        d = {
            "resourceType": "Observation",
            "id": res.get("id"),
            "status": res.get("status") or "final",
            "code": res.get("code"),
            "effectiveDateTime": _trunc(eff),
        }
        for k in _VALUE_KEYS:
            if k in res:
                d[k] = res[k]
        return Observation(**d)

    def _norm_procedure(self, res):
        d = {
            "resourceType": "Procedure",
            "id": res.get("id"),
            "status": res.get("status") or "completed",
            "subject": self._subject(res),
            "code": res.get("code"),
        }
        # R4 performed[x] (truncated to date for the engine's comparisons)
        if res.get("performedDateTime"):
            d["performedDateTime"] = _trunc(res["performedDateTime"])
        elif res.get("performedPeriod"):
            p = res["performedPeriod"]
            d["performedPeriod"] = {"start": _trunc(p.get("start")),
                                    "end": _trunc(p.get("end"))}
        return Procedure(**d)

    def _norm_encounter(self, res):
        d = {
            "resourceType": "Encounter",
            "id": res.get("id"),
            "status": res.get("status") or "finished",
            # R4 Encounter.class is required (1..1, a single Coding). The engine
            # ignores it; pass Epic's through, else default to ambulatory.
            "class": res.get("class") or {
                "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                "code": "AMB", "display": "ambulatory"},
            "type": res.get("type"),
        }
        if res.get("period"):
            p = res["period"]
            d["period"] = {"start": _trunc(p.get("start")), "end": _trunc(p.get("end"))}
        return Encounter(**d)

    # ---- Public API ----
    def load(self):
        patient = self._norm_patient(self._get(f"Patient/{self.pid}"))

        # Epic's Observation search usually requires a category; fetch each and
        # de-dupe by id.
        obs_raw = {}
        for cat in self.categories:
            for r in self._search("Observation",
                                   {"patient": self.pid, "category": cat}):
                if r.get("id"):
                    obs_raw[r["id"]] = r
        observations = {oid: self._norm_observation(r) for oid, r in obs_raw.items()}

        procedures = {}
        for r in self._search("Procedure", {"patient": self.pid}):
            m = self._norm_procedure(r)
            procedures[m.id] = m

        encounters = {}
        for r in self._search("Encounter", {"patient": self.pid}):
            m = self._norm_encounter(r)
            encounters[m.id] = m

        return patient, observations, procedures, encounters
