"""Generate a multi-patient debug timeline visualization.

Loads every patient under C:/T21/patients/, runs apply_plan for each, and writes
a single HTML page with toggle buttons for filtering patients in/out.
Each lane = one ActivityDefinition (labeled by code.text), bars = ServiceRequests.

Usage:
    python debug_timeline.py
"""

import json
from pathlib import Path
from logic import load_fixtures, load_patient_data, apply_plan

fixtures_path = "C:/T21/fixtures"
patients_root = Path("C:/T21/patients")
output_path = "C:/T21/debug_timeline.html"

activity_definitions, plan_definitions, compiled_conditions = load_fixtures(fixtures_path)

# Build canonical URL -> human label from ActivityDefinitions
label_map = {}
for ad in activity_definitions.values():
    label = (ad.code.text if ad.code and ad.code.text else None) or ad.title or ad.url
    label_map[ad.url] = label

# Run pipeline for every patient folder
patient_dirs = sorted([d for d in patients_root.iterdir() if d.is_dir()])
patients_data = []
for pd in patient_dirs:
    patient, observations, procedures, encounters = load_patient_data(str(pd))
    srs, _, flags = apply_plan(
        patient, activity_definitions, plan_definitions, compiled_conditions,
        observations, procedures, encounters
    )
    overdue_sr_ids = set()
    for f in flags:
        for ext in (f.extension or []):
            if ext.url == "https://t21app.example.org/fhir/StructureDefinition/flag-triggering-resource":
                sr_id = ext.valueReference.reference.replace("ServiceRequest/", "", 1)
                overdue_sr_ids.add(sr_id)
    patients_data.append({
        "id": pd.name,
        "patient": patient.dict(),
        "serviceRequests": [sr.dict() for sr in srs],
        "overdueIds": list(overdue_sr_ids),
    })

data = {"labels": label_map, "patients": patients_data}
data_json = json.dumps(data, default=str)

html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>T21 Debug Timeline (multi-patient)</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #e2e8f0; padding: 20px; }
  h1 { font-size: 16px; font-weight: 600; margin-bottom: 10px; }
  .controls { display: flex; gap: 8px; margin-bottom: 14px; flex-wrap: wrap; align-items: center; }
  .controls .lbl { font-size: 12px; color: #94a3b8; margin-right: 4px; }
  .pbtn { font-family: ui-monospace, monospace; font-size: 12px; padding: 6px 12px; border-radius: 4px; border: 1px solid #334155; background: #1e293b; color: #cbd5e1; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; }
  .pbtn .swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
  .pbtn.off { opacity: 0.4; }
  .pbtn:hover { border-color: #64748b; }
  .meta { font-size: 12px; color: #94a3b8; margin-bottom: 12px; font-family: ui-monospace, monospace; }
  .chart { background: #1e293b; border: 1px solid #334155; border-radius: 6px; padding: 16px; }
  .ruler { position: relative; height: 18px; margin-left: 220px; border-bottom: 1px solid #334155; }
  .ruler span { position: absolute; font-size: 11px; color: #94a3b8; transform: translateX(-50%); }
  .lane { display: flex; align-items: stretch; margin-top: 6px; border-top: 1px solid #1e293b; }
  .lane-label { width: 220px; padding: 4px 12px 4px 0; text-align: right; font-size: 12px; font-weight: 600; color: #e2e8f0; white-space: nowrap; display: flex; align-items: center; justify-content: flex-end; }
  .lane-label .count { color: #64748b; margin-left: 6px; font-weight: 400; font-family: ui-monospace, monospace; font-size: 11px; }
  .lane-track { flex: 1; position: relative; background: #0f172a; border: 1px solid #1e293b; border-radius: 3px; min-width: 600px; }
  .lane-track .grid { position: absolute; top: 0; bottom: 0; width: 1px; background: #1e293b; }
  .row { position: relative; height: 18px; }
  .row .bar { position: absolute; top: 3px; bottom: 3px; border-radius: 2px; opacity: 0.9; cursor: default; }
  .row .bar:hover { opacity: 1; outline: 1px solid #fff; }
  .row .bar.overdue { outline: 2px solid #ef4444; outline-offset: -1px; background-image: repeating-linear-gradient(135deg, transparent, transparent 3px, rgba(239,68,68,0.3) 3px, rgba(239,68,68,0.3) 6px); }
  .age-axis { position: relative; height: 18px; margin-top: 8px; margin-left: 220px; border-top: 1px solid #334155; }
  .age-axis span { position: absolute; font-size: 11px; color: #64748b; transform: translateX(-50%); padding-top: 2px; }
  .tip { position: fixed; background: #f1f5f9; color: #0f172a; padding: 8px 10px; font-size: 11px; font-family: ui-monospace, monospace; border-radius: 4px; pointer-events: none; display: none; z-index: 10; max-width: 480px; line-height: 1.5; white-space: pre; }
  .empty { color: #475569; font-size: 11px; font-style: italic; padding: 30px; text-align: center; }
</style>
</head>
<body>
<h1>T21 Debug Timeline</h1>
<div class="controls" id="controls"></div>
<div class="meta" id="meta"></div>
<div class="chart" id="chart"></div>
<div class="tip" id="tip"></div>
<script>
var DATA = __DATA__;

var palette = ["#3b82f6","#f59e0b","#10b981","#ec4899","#8b5cf6","#06b6d4","#ef4444","#84cc16"];
var patients = DATA.patients;
var labels = DATA.labels;

// Assign a color per patient and build overdue lookup
var patientColor = {};
var overdueMap = {};
for (var i = 0; i < patients.length; i++) {
  patientColor[patients[i].id] = palette[i % palette.length];
  var oset = {};
  var oids = patients[i].overdueIds || [];
  for (var j = 0; j < oids.length; j++) oset[oids[j]] = true;
  overdueMap[patients[i].id] = oset;
}

// Track which patients are visible
var visible = {};
for (var i = 0; i < patients.length; i++) visible[patients[i].id] = true;

// Render filter buttons
var ctrl = document.getElementById("controls");
var labelEl = document.createElement("span");
labelEl.className = "lbl";
labelEl.textContent = "Patients:";
ctrl.appendChild(labelEl);
for (var i = 0; i < patients.length; i++) {
  (function(p) {
    var btn = document.createElement("button");
    btn.className = "pbtn";
    btn.dataset.pid = p.id;
    var nm = (p.patient.name && p.patient.name[0]) || {};
    var who = ((nm.given || []).join(" ") + " " + (nm.family || "")).trim() || p.id;
    btn.innerHTML = '<span class="swatch" style="background:' + patientColor[p.id] + '"></span>' + p.id + ' (' + who + ')';
    btn.onclick = function() {
      visible[p.id] = !visible[p.id];
      btn.classList.toggle("off", !visible[p.id]);
      render();
    };
    ctrl.appendChild(btn);
  })(patients[i]);
}

function pct(iso, minD, totalMs) {
  return ((new Date(iso + "T00:00:00") - minD) / totalMs) * 100;
}

function render() {
  var visibleIds = patients.filter(function(p) { return visible[p.id]; }).map(function(p) { return p.id; });
  var chart = document.getElementById("chart");
  var meta = document.getElementById("meta");

  if (visibleIds.length === 0) {
    chart.innerHTML = '<div class="empty">No patients selected — click a button above to show one.</div>';
    meta.textContent = "";
    return;
  }

  // Collect visible SRs
  var visibleSrs = [];
  for (var i = 0; i < patients.length; i++) {
    if (!visible[patients[i].id]) continue;
    for (var j = 0; j < patients[i].serviceRequests.length; j++) {
      var sr = patients[i].serviceRequests[j];
      visibleSrs.push({pid: patients[i].id, sr: sr});
    }
  }

  // Group by canonical
  var groups = {};
  for (var i = 0; i < visibleSrs.length; i++) {
    var c = (visibleSrs[i].sr.instantiatesCanonical && visibleSrs[i].sr.instantiatesCanonical[0]) || "(no canonical)";
    if (!groups[c]) groups[c] = {};
    if (!groups[c][visibleSrs[i].pid]) groups[c][visibleSrs[i].pid] = [];
    groups[c][visibleSrs[i].pid].push(visibleSrs[i].sr);
  }
  var canonicals = Object.keys(groups).sort();

  // Time bounds across all visible
  var allDates = [];
  for (var i = 0; i < visibleSrs.length; i++) {
    allDates.push(visibleSrs[i].sr.occurrencePeriod.start);
    allDates.push(visibleSrs[i].sr.occurrencePeriod.end);
  }
  allDates.sort();
  var minD = new Date(allDates[0] + "T00:00:00");
  var maxD = new Date(allDates[allDates.length - 1] + "T00:00:00");
  var totalMs = maxD - minD;

  // Meta line
  var totals = visibleIds.map(function(pid) {
    var n = 0; var nf = 0;
    for (var i = 0; i < patients.length; i++) {
      if (patients[i].id === pid) {
        n = patients[i].serviceRequests.length;
        nf = (patients[i].overdueIds || []).length;
      }
    }
    return pid + ":" + n + " SRs" + (nf > 0 ? " (" + nf + " overdue)" : "");
  }).join("  ");
  meta.textContent = "Showing " + visibleIds.length + " patient(s) - " + totals + "  |  " + canonicals.length + " activities";

  // Year ruler
  var startYear = minD.getFullYear();
  var endYear = maxD.getFullYear();
  var years = [];
  var rulerHtml = '<div class="ruler">';
  for (var y = startYear; y <= endYear; y++) {
    var p = pct(y + "-01-01", minD, totalMs);
    if (p >= 0 && p <= 100) {
      years.push({y: y, p: p});
      rulerHtml += '<span style="left:' + p + '%">' + y + '</span>';
    }
  }
  rulerHtml += '</div>';

  // Lanes
  var lanesHtml = "";
  for (var g = 0; g < canonicals.length; g++) {
    var canonical = canonicals[g];
    var label = labels[canonical] || canonical;
    var perPatient = groups[canonical];
    var total = 0;
    for (var k in perPatient) total += perPatient[k].length;

    lanesHtml += '<div class="lane">';
    lanesHtml += '<div class="lane-label" title="' + canonical + '">' + label + '<span class="count">(' + total + ')</span></div>';
    lanesHtml += '<div class="lane-track">';
    // Gridlines on full lane height
    for (var i = 0; i < years.length; i++) {
      lanesHtml += '<div class="grid" style="left:' + years[i].p + '%"></div>';
    }
    // One sub-row per visible patient (even if that patient has no SRs in this lane, keeps rows aligned)
    for (var pi = 0; pi < visibleIds.length; pi++) {
      var pid = visibleIds[pi];
      var color = patientColor[pid];
      lanesHtml += '<div class="row">';
      var items = perPatient[pid] || [];
      for (var i = 0; i < items.length; i++) {
        var sr = items[i];
        var l = Math.max(0, pct(sr.occurrencePeriod.start, minD, totalMs));
        var r = Math.min(100, pct(sr.occurrencePeriod.end, minD, totalMs));
        var w = Math.max(0.25, r - l);
        var isOverdue = overdueMap[pid] && overdueMap[pid][sr.id];
        var odTag = isOverdue ? " [OVERDUE]" : "";
        var odClass = isOverdue ? " overdue" : "";
        var noteText = (sr.note && sr.note.length > 0) ? sr.note[0].text : "";
        var condLine = noteText ? "\\n  condition: " + noteText : "\\n  condition: (none)";
        var tip = pid + "  " + sr.id + odTag + "\\n  start: " + sr.occurrencePeriod.start + "\\n  end:   " + sr.occurrencePeriod.end + "\\n  canonical: " + canonical + condLine;
        lanesHtml += '<div class="bar' + odClass + '" style="left:' + l + '%;width:' + w + '%;background:' + color + '" data-tip="' + tip + '"></div>';
      }
      lanesHtml += '</div>';
    }
    lanesHtml += '</div></div>';
  }

  // Age axis - only meaningful when one patient is visible (DOBs differ)
  var ageHtml = "";
  if (visibleIds.length === 1) {
    var thePatient = patients.filter(function(p) { return p.id === visibleIds[0]; })[0].patient;
    ageHtml = '<div class="age-axis">';
    var dob = new Date(thePatient.birthDate + "T00:00:00");
    for (var a = 0; a <= 22; a++) {
      var ad = new Date(dob); ad.setFullYear(ad.getFullYear() + a);
      var iso = ad.toISOString().slice(0, 10);
      var p = pct(iso, minD, totalMs);
      if (p >= 0 && p <= 100) ageHtml += '<span style="left:' + p + '%">' + a + 'y</span>';
    }
    ageHtml += '</div>';
  }

  chart.innerHTML = rulerHtml + lanesHtml + ageHtml;
}

// Tooltip
var tip = document.getElementById("tip");
document.getElementById("chart").addEventListener("mouseover", function(e) {
  if (e.target.classList && e.target.classList.contains("bar")) {
    tip.textContent = e.target.getAttribute("data-tip").replace(/\\\\n/g, "\\n");
    tip.style.display = "block";
  }
});
document.getElementById("chart").addEventListener("mousemove", function(e) {
  if (tip.style.display === "block") {
    tip.style.left = (e.clientX + 14) + "px";
    tip.style.top = (e.clientY + 12) + "px";
  }
});
document.getElementById("chart").addEventListener("mouseout", function(e) {
  if (e.target.classList && e.target.classList.contains("bar")) tip.style.display = "none";
});

render();
</script>
</body>
</html>
"""

html = html.replace("__DATA__", data_json)

with open(output_path, "w", encoding="utf-8") as f:
    f.write(html)

print(f"Wrote {output_path}")
print(f"Patients ({len(patients_data)}):")
for p in patients_data:
    print(f"  {p['id']}: {len(p['serviceRequests'])} ServiceRequests")
print(f"Activity labels:")
for url, lbl in label_map.items():
    print(f"  {lbl:<35} <- {url}")
