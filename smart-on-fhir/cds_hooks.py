"""CDS Hooks service for T21 health supervision.

Exposes the existing T21 engine as a CDS Hooks 2.0 service. When a clinician
opens a patient's chart in Epic (the `patient-view` hook), Epic calls this
service. For each AAP Trisomy 21 screening that is due or overdue, the service
returns a card with a "suggestion" whose action creates a ServiceRequest. When
the clinician accepts the suggestion, Epic places the order through its own
(signed) ordering workflow, so Epic stays the system of record.

Nothing in the engine changes. This module reuses:
  - engine_adapter.load_engine   cached load of the FHIR knowledge artifacts
  - logic.apply_plan             the due / overdue reasoning
  - EpicFhirSource               live patient data from Epic (via the hook token)
  - LocalFixtureSource           repo fixtures, for running this with no Epic

Endpoints (registered as a Flask blueprint by app.py):
  GET  /cds-services             discovery
  POST /cds-services/<id>        invocation (patient-view)

Spec: https://cds-hooks.hl7.org/2.0/
Epic: https://fhir.epic.com/Documentation?docId=cds-hooks
Design + production notes: CDS_HOOKS.md
"""
from __future__ import annotations

import datetime as _dt
import json
import uuid

from flask import Blueprint, request, jsonify

from config import config
import logic
import engine_adapter
import build_supervision_dashboard as bsd   # reuse to_date (single source of truth)
from fhir_source import EpicFhirSource, LocalFixtureSource

cds = Blueprint("cds_hooks", __name__)

SERVICE_ID = "t21-screening-supervision"
GUIDELINE_LABEL = "AAP Trisomy 21 Health Supervision"
GUIDELINE_URL = "https://github.com/aidanpetro1/fhir-activity-plan-engine"


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def service_catalog():
    """The CDS Hooks discovery document.

    No `prefetch` is declared, so Epic provides `fhirServer` + `fhirAuthorization`
    and the service fetches what it needs via the existing EpicFhirSource.
    Declaring prefetch templates is a later optimization (see CDS_HOOKS.md).
    """
    return {
        "services": [
            {
                "hook": "patient-view",
                "id": SERVICE_ID,
                "title": "Trisomy 21 health supervision",
                "description": (
                    "Surfaces AAP Trisomy 21 screenings that are due or overdue "
                    "for the patient and offers each as an orderable suggestion."
                ),
            }
        ]
    }


@cds.route("/cds-services", methods=["GET"])
def discovery():
    return jsonify(service_catalog())


# --------------------------------------------------------------------------- #
# Invocation
# --------------------------------------------------------------------------- #
@cds.route("/cds-services/<service_id>", methods=["POST"])
def invoke(service_id):
    if service_id != SERVICE_ID:
        return jsonify({"error": f"Unknown CDS service '{service_id}'"}), 404

    # 1) Verify the EHR-signed JWT. No-op in dev unless CDS_REQUIRE_JWT=true.
    try:
        verify_request_jwt(request)
    except JwtError as e:
        return jsonify({"error": str(e)}), 401

    body = request.get_json(silent=True) or {}
    if body.get("hook") != "patient-view":
        return jsonify({"error": "This service only handles the patient-view hook."}), 400

    ctx = body.get("context") or {}
    patient_id = ctx.get("patientId")
    if not patient_id:
        return jsonify({"error": "Missing context.patientId"}), 400

    # 2) Load the patient's data (Epic via token, or local fixtures for dev).
    try:
        patient, obs, procs, encs = _load_patient(body, patient_id)
    except Exception as e:
        return jsonify({"error": f"Could not load patient data: {e}"}), 502

    # 3) Run the engine and turn due / overdue screenings into cards.
    return jsonify({"cards": build_cards(patient, obs, procs, encs)})


def _load_patient(body, patient_id):
    """Resolve the 4-tuple the engine expects from the hook request.

    Epic path: the request carries `fhirServer` + `fhirAuthorization.access_token`,
    so we fetch live with the same EpicFhirSource the SMART app uses. Dev path:
    no token (or DATA_SOURCE=local) falls back to repo fixtures, treating
    context.patientId as a local patient folder name.
    """
    fhir_auth = body.get("fhirAuthorization") or {}
    fhir_server = body.get("fhirServer")
    token = fhir_auth.get("access_token")

    if fhir_server and token and config.DATA_SOURCE != "local":
        src = EpicFhirSource(
            fhir_server, token, patient_id,
            observation_categories=config.OBSERVATION_CATEGORIES,
            verify_tls=config.VERIFY_TLS,
        )
        return src.load()

    pdir = config.REPO_ROOT / "patients" / patient_id
    if not pdir.is_dir():
        pdir = config.REPO_ROOT / "patients" / config.DEFAULT_LOCAL_PATIENT
    return LocalFixtureSource(pdir).load()


# --------------------------------------------------------------------------- #
# Engine output -> CDS Hooks cards
# --------------------------------------------------------------------------- #
def build_cards(patient, observations, procedures, encounters):
    """Run the engine and build one card per actionable (due/overdue) screening."""
    ad, pd_, cc = engine_adapter.load_engine()
    srs, _sched, flags = logic.apply_plan(
        patient, ad, pd_, cc, observations, procedures, encounters
    )
    flag_ids = {f.id for f in flags}
    today = _dt.date.today()
    title_by_canon = {a.url: (a.title or a.name or "Screening") for a in ad.values()}

    # Pick the actionable ServiceRequest per screening: an overdue window wins,
    # else the window covering today (due now). Skip future-only and fulfilled.
    chosen = {}  # canonical -> (ServiceRequest, is_overdue)
    for sr in srs:
        canon = (sr.instantiatesCanonical or [None])[0]
        if canon is None:
            continue
        overdue = sr.id in flag_ids
        due_now = False
        if sr.occurrencePeriod:
            lo = bsd.to_date(sr.occurrencePeriod.start)
            hi = bsd.to_date(sr.occurrencePeriod.end)
            due_now = bool(lo and hi and lo <= today <= hi)
        if not (overdue or due_now):
            continue
        prev = chosen.get(canon)
        if prev is None or (overdue and not prev[1]):
            chosen[canon] = (sr, overdue)

    cards = [
        _card_for(sr, title_by_canon.get(canon, "Screening"), overdue)
        for canon, (sr, overdue) in chosen.items()
    ]
    # Overdue (warning) first, then alphabetical for a stable order.
    cards.sort(key=lambda c: (0 if c["indicator"] == "warning" else 1, c["summary"]))
    return cards


def _card_for(sr, title, overdue):
    status_word = "overdue" if overdue else "due"
    when = ""
    if sr.occurrencePeriod:
        lo, hi = bsd.to_date(sr.occurrencePeriod.start), bsd.to_date(sr.occurrencePeriod.end)
        if lo and hi:
            when = f" (window {lo.isoformat()} to {hi.isoformat()})"

    return {
        "uuid": str(uuid.uuid4()),
        "summary": f"{title} is {status_word}",
        "indicator": "warning" if overdue else "info",
        "detail": (
            f"Per the {GUIDELINE_LABEL} schedule, **{title}** is {status_word} "
            f"for this patient{when}."
        ),
        "source": {"label": GUIDELINE_LABEL, "url": GUIDELINE_URL},
        "selectionBehavior": "at-most-one",
        "suggestions": [
            {
                "label": f"Order {title}",
                "uuid": str(uuid.uuid4()),
                "isRecommended": True,
                "actions": [
                    {
                        "type": "create",
                        "description": f"Create an order for {title}",
                        "resource": _order_resource(sr),
                    }
                ],
            }
        ],
    }


def _order_resource(sr):
    """A clean ServiceRequest to propose as a new order.

    The engine builds the resource with status 'active'; for a suggestion it is a
    proposed, unsigned order, so we set status 'draft' and drop the engine's
    internal id/note and let Epic assign the real order identity on accept.
    """
    res = _to_dict(sr)
    res["status"] = "draft"
    res.pop("id", None)
    res.pop("note", None)
    return res


def _to_dict(model):
    """Serialize a fhir.resources model to a plain dict (v1 .json / v2 fallback)."""
    try:
        return json.loads(model.json())
    except AttributeError:
        return json.loads(model.model_dump_json())


# --------------------------------------------------------------------------- #
# JWT verification (CDS Hooks security)
# --------------------------------------------------------------------------- #
class JwtError(Exception):
    """Raised when the EHR-signed request JWT is missing or invalid."""


def verify_request_jwt(req):
    """Validate the CDS Hooks request JWT Epic signs on every call.

    Disabled by default for local dev (CDS_REQUIRE_JWT=false). In any deployment
    Epic can reach, set CDS_REQUIRE_JWT=true and populate the allowlists. Epic
    signs every Discovery and service call; we verify the signature against the
    EHR's JWKS (the `jku` header), and check `iss` and `aud`.
    """
    if not config.CDS_REQUIRE_JWT:
        return

    auth = req.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise JwtError("Missing Bearer JWT in Authorization header")
    token = auth[len("Bearer "):]

    try:
        import jwt
        from jwt import PyJWKClient
    except ImportError as e:  # pragma: no cover - only hit in prod without the dep
        raise JwtError("PyJWT not installed. Run: pip install 'PyJWT[crypto]'") from e

    try:
        header = jwt.get_unverified_header(token)
        claims = jwt.decode(token, options={"verify_signature": False})
    except Exception as e:
        raise JwtError(f"Malformed JWT: {e}") from e

    iss = claims.get("iss")
    if config.CDS_TRUSTED_ISS and iss not in config.CDS_TRUSTED_ISS:
        raise JwtError(f"Untrusted issuer: {iss}")

    jku = header.get("jku")
    if not jku:
        raise JwtError("JWT header missing 'jku' (the EHR's JWKS URL)")
    if config.CDS_TRUSTED_JKU and jku not in config.CDS_TRUSTED_JKU:
        raise JwtError(f"Untrusted jku: {jku}")

    try:
        signing_key = PyJWKClient(jku).get_signing_key_from_jwt(token).key
        jwt.decode(
            token,
            signing_key,
            algorithms=["RS384", "ES384", "RS256"],
            audience=config.CDS_SERVICE_BASE_URL or None,
            options={"verify_aud": bool(config.CDS_SERVICE_BASE_URL)},
        )
    except Exception as e:
        raise JwtError(f"JWT signature/claims invalid: {e}") from e
