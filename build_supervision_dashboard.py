#!/usr/bin/env python3
"""Generate the Trisomy 21 supervision dashboard for a patient from live engine output.

Mirrors debug_timeline.py: runs the engine (logic.apply_plan), derives a current
status per ORDERABLE action, maps it into the dashboard's ITEMS schema, and injects
that JSON into the flat-list HTML template (trisomy21_dashboard_v18.html).

Scope (per project decisions 2026-05-27):
  - orderables only; counseling/discussion items are left as static UI elsewhere
  - flat list, no body-system grouping
  - read-only: "Order all" is a placeholder; real ordering will go through Epic

Usage:  python build_supervision_dashboard.py [patient-dir]   (default patients/patient-1)
"""
import json, sys, os, re
from datetime import date, datetime
import logic

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
TEMPLATE = os.path.join(HERE, "trisomy21_dashboard_v18.html")


def to_date(x):
    if x is None:
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return date.fromisoformat(str(x)[:10])


def fmt_result(obs):
    if obs.valueQuantity is not None:
        v = obs.valueQuantity
        return f"{v.value} {v.unit or ''}".strip()
    if obs.valueCodeableConcept is not None:
        cc = obs.valueCodeableConcept
        if cc.text:
            return cc.text
        if cc.coding:
            return cc.coding[0].display or cc.coding[0].code
    if getattr(obs, "valueString", None):
        return obs.valueString
    return "Result on file"


def latest_matching_obs(observations, code):
    matches = [o for o in observations.values()
               if code in {c.code for c in (o.code.coding or []) if c.code} and o.effectiveDateTime]
    return max(matches, key=lambda o: to_date(o.effectiveDateTime)) if matches else None


def citation(adef):
    """AAP guideline citation from the ActivityDefinition.relatedArtifact
    (type=citation), or None. This is the data-driven source for an order's
    guideline text shown in the dashboard."""
    for ra in (adef.relatedArtifact or []):
        if getattr(ra, "type", None) == "citation":
            txt = getattr(ra, "citation", None) or getattr(ra, "display", None) or getattr(ra, "label", None)
            if txt:
                return str(txt).strip()
    return None


def build_items(patient_dir):
    ad, pd_, cc = logic.load_fixtures(FIXTURES)
    patient, obs, procs, encs = logic.load_patient_data(patient_dir)
    srs, sched, flags = logic.apply_plan(patient, ad, pd_, cc, obs, procs, encs)

    today = date.today()
    flag_ids = {f.id for f in flags}

    # guideline context: first PlanDefinition.description that references each AD
    plan_desc = {}
    for plan in pd_.values():
        for a in plan.action:
            if a.definitionCanonical and a.definitionCanonical not in plan_desc:
                plan_desc[a.definitionCanonical] = plan.description or ""

    items = []
    for adef in ad.values():
        code = adef.code.coding[0].code
        ad_srs = [sr for sr in srs if adef.url in (sr.instantiatesCanonical or [])]

        overdue = any(sr.id in flag_ids for sr in ad_srs)
        current = False
        for sr in ad_srs:
            if sr.id in flag_ids or not sr.occurrencePeriod:
                continue
            lo, hi = to_date(sr.occurrencePeriod.start), to_date(sr.occurrencePeriod.end)
            if lo and hi and lo <= today <= hi:
                current = True
                break
        ful = latest_matching_obs(obs, code)

        if overdue:
            st = "overdue"
        elif current:
            st = "due"
        elif ful:
            st = "complete"
        else:
            st = "na"

        if st == "na":
            continue  # not an active orderable for this patient — hidden

        g = citation(adef)
        if not g:
            g = (adef.description or "").strip()
            if plan_desc.get(adef.url):
                g = (g + " " + plan_desc[adef.url]).strip()

        item = {"s": "", "n": adef.title, "st": st,
                "b": "order" if st in ("overdue", "due") else None,
                "g": g, "d": "", "r": "",
                "sched": plan_desc.get(adef.url, "")}
        if st == "complete" and ful:
            item["d"] = to_date(ful.effectiveDateTime).isoformat()
            item["r"] = fmt_result(ful)
        items.append(item)

    rank = {"overdue": 0, "due": 1, "complete": 2}
    items.sort(key=lambda x: (rank.get(x["st"], 9), x["n"]))
    return patient, items


def main():
    pdir = sys.argv[1] if len(sys.argv) > 1 else "patients/patient-1"
    patient, items = build_items(pdir)

    html = open(TEMPLATE, encoding="utf-8").read()
    START, END = "/* DATA-START */", "/* DATA-END */"
    i = html.index(START) + len(START)
    j = html.index(END)
    data = "\nconst SECTIONS = [];\nconst ITEMS = " + json.dumps(items, indent=2) + ";\n"
    html = html[:i] + data + html[j:]

    dob = to_date(patient.birthDate).isoformat()
    html = re.sub(r"const DOB = new Date\('[^']*'\);",
                  f"const DOB = new Date('{dob}');", html)

    pid = os.path.basename(os.path.normpath(pdir))
    out = os.path.join(HERE, f"supervision_dashboard.{pid}.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)

    print(f"patient={patient.id}  dob={dob}  orderable items={len(items)}")
    for it in items:
        print(f"  [{it['st']:8}] {it['n']:34} {it['r']}")
    print("wrote", out)


if __name__ == "__main__":
    main()
