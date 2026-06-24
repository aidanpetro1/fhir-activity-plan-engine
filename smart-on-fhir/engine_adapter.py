"""Bridge between the engine and the dashboard UI.

Takes already-loaded patient data (from any source), runs logic.apply_plan, and
produces the dashboard's ITEMS list and final HTML. The status-derivation rules
here mirror build_supervision_dashboard.py exactly (orderables only, flat list,
overdue > due > complete > hidden) so the web app and the CLI generator agree.

The fiddly helpers (to_date, fmt_result, latest_matching_obs) are imported from
build_supervision_dashboard so there is a single source of truth for them.
"""
import json
import re
from datetime import date
from functools import lru_cache

from config import config          # import first: also puts the repo root on sys.path
import logic                       # the engine (lives in the repo root)
import build_supervision_dashboard as bsd  # reuse to_date / fmt_result / latest_matching_obs


@lru_cache(maxsize=1)
def load_engine():
    """Load + precompile the FHIR knowledge artifacts once per process."""
    return logic.load_fixtures(str(config.FIXTURES_DIR))


# --- Counseling reference doc ------------------------------------------------
# Counseling guidance now lives as a plain-Markdown reference doc, NOT a FHIR
# artifact: fixtures/counseling/aap-t21-counseling-guidelines.md. The engine
# (logic.load_fixtures) only reads activity-definitions/ and plan-definitions/,
# so apply_plan is unaffected. We parse the doc here solely to feed the
# dashboard's "Discuss today" card, keeping the Markdown as the single source
# of truth for counseling content.
#
# The age bands are stable, so each "## <heading>" is mapped to a month range
# here rather than parsed out of prose; the "- " bullets under a heading are its
# topics. (low inclusive, high exclusive; high=None is open-ended.)
COUNSELING_DOC = config.FIXTURES_DIR / "counseling" / "aap-t21-counseling-guidelines.md"

_AGE_BANDS = [
    ("Birth - 1 month", 0, 1),
    ("1 month - 1 year", 1, 12),
    ("1 - 5 years", 12, 60),
    ("5 - 12 years", 60, 144),
    ("12 - 21 years", 144, None),
]


def _norm_heading(s):
    """Normalize a heading for matching: lowercase, unify dashes, drop whitespace."""
    s = s.lower().replace("–", "-").replace("—", "-")
    return "".join(s.split())


_BAND_MONTHS = {_norm_heading(h): (lo, hi) for h, lo, hi in _AGE_BANDS}


def _clean_topic(line):
    """Bullet line -> display label: drop the leading '- ' and the *(once)* emphasis."""
    t = line.lstrip()[2:].strip()            # strip the "- " marker
    return t.replace("*(once)*", "(once)").strip()


@lru_cache(maxsize=1)
def load_counseling():
    """Parse the counseling Markdown reference doc into age bands.

    Returns a list of {"low", "high", "topics"} dicts (months; high=None is
    open-ended), one per "## <heading>" section whose heading is a known age
    band. Returns [] if the doc is absent, leaving "Discuss today" empty.
    """
    if not COUNSELING_DOC.exists():
        return []
    bands, current = [], None
    for raw in COUNSELING_DOC.read_text(encoding="utf-8").splitlines():
        if raw.startswith("## "):
            months = _BAND_MONTHS.get(_norm_heading(raw[3:]))
            current = {"low": months[0], "high": months[1], "topics": []} if months else None
            if current is not None:
                bands.append(current)
        elif current is not None and raw.lstrip().startswith("- "):
            current["topics"].append(_clean_topic(raw))
    return bands


def _age_months(dob, today):
    """Whole calendar months between dob and today (mirrors the UI's ageAt)."""
    mo = (today.year - dob.year) * 12 + (today.month - dob.month)
    if today.day < dob.day:
        mo -= 1
    return max(0, mo)


def build_discuss(patient, today=None):
    """Age-appropriate counseling topics for the dashboard's "Discuss today" list.

    Selects the age band (PlanDefinition top-level action whose timingRange in
    months covers the patient's age; an absent high bound is open-ended) and
    returns its sub-action titles as DISCUSS items.
    """
    dob = bsd.to_date(patient.birthDate) if patient.birthDate else None
    if not dob:
        return []
    age = _age_months(dob, today or date.today())
    items = []
    for band in load_counseling():
        if age < band["low"]:
            continue
        if band["high"] is not None and age >= band["high"]:
            continue
        items.extend({"n": t, "done": False} for t in band["topics"] if t)
    return items


def build_items(patient, observations, procedures, encounters, engine=None):
    """Run the engine for one patient and map output to the dashboard ITEMS schema."""
    ad, pd_, cc = engine or load_engine()
    srs, sched, flags = logic.apply_plan(
        patient, ad, pd_, cc, observations, procedures, encounters
    )

    today = date.today()
    flag_ids = {f.id for f in flags}

    # Map each orderable (ActivityDefinition canonical) to its PlanDefinition's
    # human-readable AAP schedule, for the dashboard's "AAP schedule" detail.
    sched_by_canon = {}
    for plan in pd_.values():
        desc = (plan.description or "").strip()
        for action in (plan.action or []):
            canon = getattr(action, "definitionCanonical", None)
            if canon and desc:
                sched_by_canon.setdefault(canon, desc)

    items = []
    for adef in ad.values():
        code = adef.code.coding[0].code
        ad_srs = [sr for sr in srs if adef.url in (sr.instantiatesCanonical or [])]

        overdue = any(sr.id in flag_ids for sr in ad_srs)
        current = False
        for sr in ad_srs:
            if sr.id in flag_ids or not sr.occurrencePeriod:
                continue
            lo, hi = bsd.to_date(sr.occurrencePeriod.start), bsd.to_date(sr.occurrencePeriod.end)
            if lo and hi and lo <= today <= hi:
                current = True
                break
        ful = bsd.latest_matching_obs(observations, code)

        if overdue:
            st = "overdue"
        elif current:
            st = "due"
        elif ful:
            st = "complete"
        else:
            st = "na"

        if st == "na":
            continue  # not an active orderable for this patient -- hidden

        item = {"s": "", "n": adef.title, "st": st,
                "b": "order" if st in ("overdue", "due") else None,
                "d": "", "r": "",
                "sched": sched_by_canon.get(adef.url, "")}
        if st == "complete" and ful:
            item["d"] = bsd.to_date(ful.effectiveDateTime).isoformat()
            item["r"] = bsd.fmt_result(ful)
        items.append(item)

    rank = {"overdue": 0, "due": 1, "complete": 2}
    items.sort(key=lambda x: (rank.get(x["st"], 9), x["n"]))
    return items


def render_dashboard(patient, items, discuss=None):
    """Inject ITEMS + DISCUSS + DOB into the dashboard template and return the HTML.

    Uses the exact same sentinel-injection technique as build_supervision_dashboard.py
    so the served page is identical to the CLI-generated one.
    """
    html = config.DASHBOARD_TEMPLATE.read_text(encoding="utf-8")
    START, END = "/* DATA-START */", "/* DATA-END */"
    i = html.index(START) + len(START)
    j = html.index(END)
    data = "\nconst SECTIONS = [];\nconst ITEMS = " + json.dumps(items, indent=2) + ";\n"
    html = html[:i] + data + html[j:]

    DS, DE = "/* DISCUSS-START */", "/* DISCUSS-END */"
    if discuss is None:
        discuss = build_discuss(patient)
    k = html.index(DS) + len(DS)
    m = html.index(DE)
    dd = "\nconst DISCUSS = " + json.dumps(discuss, indent=2) + ";\n"
    html = html[:k] + dd + html[m:]

    dob = bsd.to_date(patient.birthDate).isoformat()
    html = re.sub(r"const DOB = new Date\('[^']*'\);",
                  f"const DOB = new Date('{dob}');", html)
    return html
