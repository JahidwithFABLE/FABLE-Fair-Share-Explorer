"""Validate uploaded reports and call the released scientific engine."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import json
import math
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook
from database_import import database_rows
from openpyxl.utils.exceptions import InvalidFileException
import fair_share as engine

ROOT = Path(__file__).resolve().parent
APPROACHES = {"AP": "Ability to Pay", "HR": "Historical Responsibility", "EQ": "Equity (IEPC)"}
RATES = ("calcallagrico2e", "calcalllandco2e")
YEARS = tuple(engine.BLOCK_END_YEARS)
MAX_UPLOAD_MB = 100
MAX_INDICATOR_ROWS = 60_000
MAX_UNCOMPRESSED_MB = 900

@dataclass(frozen=True)
class Release:
    label: str
    weights: object
    benchmark: object
    fingerprint: str

def typed_label(value):
    return "" if value is None else str(value).strip()

def event_value(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and float(value).is_integer():
        return str(int(value))
    return typed_label(value)

def trade_flag(value):
    text = typed_label(value).lower()
    if text in {"true", "yes", "1", "1.0"}:
        return True
    if text in {"false", "no", "0", "0.0"}:
        return False
    raise ValueError(f"Unrecognized trade_adj value {value!r}.")

def release_for_app():
    configs = json.loads((ROOT / "releases.json").read_text(encoding="utf-8"))
    releases = {}
    for event, config in configs.items():
        wpath = ROOT / config["weights"]
        bpath = ROOT / config["benchmark"] if config.get("benchmark") else None
        weights = engine.load_weights(wpath)
        benchmark = engine.load_benchmark(path=bpath, weights=weights)
        if benchmark.scenathon_id != event:
            raise ValueError(f"Release for Scenathon {event} has a different benchmark event ID.")
        # Fingerprint the actual frozen scientific inputs, not a filename or display label.
        payload = [event, weights.version, weights.budget_mtco2e, weights.relative_gap_basis,
                   benchmark.version, benchmark.snapshot_id,
                   sorted(benchmark.net_emissions.items()), sorted(weights.percentages.items()),
                   sorted(benchmark.allowances.items())]
        digest = sha256(json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        releases[event] = Release(config["title"], weights, benchmark, digest)
    return releases

@dataclass(frozen=True)
class Report:
    sheet: str
    fields: frozenset
    rows: tuple
    digest: str

def read_report(blob: bytes) -> Report:
    if not blob or len(blob) > MAX_UPLOAD_MB * 1024**2:
        raise ValueError(f"Choose a non-empty .xlsx file up to {MAX_UPLOAD_MB} MB.")
    try:
        with ZipFile(BytesIO(blob)) as z:
            files = z.infolist()
            if len(files) > 10_000 or sum(i.file_size for i in files) > MAX_UNCOMPRESSED_MB * 1024**2:
                raise ValueError("Workbook is too large to validate. Use a smaller Scenathon Excel export.")
        # Scenathon full database exports use different column names and may
        # contain a very large Trade sheet. Parse only Indicators first.
        extracted = database_rows(blob, max_rows=MAX_INDICATOR_ROWS)
        if extracted is not None:
            return Report("Indicators", frozenset(extracted[0]), tuple(extracted),
                          sha256(blob).hexdigest())
        workbook = load_workbook(BytesIO(blob), read_only=True, data_only=True, keep_links=False)
    except (BadZipFile, InvalidFileException, KeyError, OSError) as err:
        raise ValueError("The upload is not a readable Scenathon .xlsx workbook.") from err
    try:
        sheets = {s.strip().casefold(): s for s in workbook.sheetnames}
        sheet = sheets.get("indicators") or sheets.get("indicators report")
        if sheet is None:
            raise ValueError("No Indicators or Indicators Report sheet was found.")
        it = workbook[sheet].values
        header = [typed_label(x).casefold() for x in next(it, ())]
        keys = Counter(x for x in header if x)
        required = {"country", "year", "pathway_name_default", *RATES}
        wanted = required | {"scenathon_id", "trade_adj", "snapshot_id"}
        if required - keys.keys():
            raise ValueError(f"Missing Indicators columns: {', '.join(sorted(required-keys.keys()))}.")
        if any(keys[k] > 1 for k in wanted):
            raise ValueError("The Indicators sheet has duplicate required or selection columns.")
        fields = [(i, k) for i, k in enumerate(header) if k in wanted]
        rows = []
        for i, row in enumerate(it, start=2):
            if i > MAX_INDICATOR_ROWS + 1:
                raise ValueError("Too many Indicators rows. Upload an export for this Scenathon.")
            r = {k: row[j] if j < len(row) else None for j, k in fields}
            if any(x is not None for x in r.values()):
                rows.append(r)
        if not rows:
            raise ValueError("The Indicators sheet is empty.")
        return Report(sheet, frozenset(k for i,k in fields), tuple(rows), sha256(blob).hexdigest())
    finally:
        workbook.close()

def find_rows(report, event=None, pathway=None, stage=None, snapshot=None):
    rows = list(report.rows)
    if event is not None and "scenathon_id" in report.fields:
        rows = [r for r in rows if event_value(r["scenathon_id"]) == event]
    if pathway is not None:
        rows = [r for r in rows if typed_label(r["pathway_name_default"]) == pathway]
    if stage is not None and "trade_adj" in report.fields:
        rows = [r for r in rows if trade_flag(r["trade_adj"]) is stage]
    if snapshot is not None and "snapshot_id" in report.fields:
        rows = [r for r in rows if typed_label(r["snapshot_id"]) == snapshot]
    return rows

def options(report, event, pathway=None, stage=None):
    matching = find_rows(report, event=event, pathway=pathway, stage=stage)
    paths = sorted({typed_label(r["pathway_name_default"]) for r in matching} - {""})
    trades = sorted({trade_flag(r["trade_adj"]) for r in matching}) if "trade_adj" in report.fields else [False, True]
    snapshots = sorted({typed_label(r["snapshot_id"]) for r in matching} - {""}) if "snapshot_id" in report.fields else []
    return paths, trades, snapshots

def prepare_result(report, release, event, pathway, stage, snapshot=None, confirmed=False):
    if release.benchmark.scenathon_id != event:
        raise ValueError("The registered benchmark belongs to another Scenathon.")
    if type(stage) is not bool or not typed_label(pathway):
        raise ValueError("Choose a pathway and trade stage.")
    needs_confirmation = {"trade_adj", "scenathon_id"} - report.fields
    if needs_confirmation and not confirmed:
        raise ValueError("Confirm the missing event or trade-stage metadata before calculation.")
    rows = find_rows(report, event, pathway, stage)
    if not rows:
        raise ValueError("No Indicators rows match this event, pathway and trade stage.")
    if "snapshot_id" in report.fields:
        observed = {typed_label(r["snapshot_id"]) for r in rows}
        if "" in observed:
            raise ValueError("The selected export rows contain a blank snapshot_id.")
        if snapshot is None and len(observed) == 1:
            snapshot = next(iter(observed))
        if snapshot not in observed:
            raise ValueError("Choose an exact snapshot from the workbook.")
        rows = find_rows(report, event, pathway, stage, snapshot)
    else:
        snapshot = f"excel-{report.digest[:16]}"
    selected = []
    for row in rows:
        try:
            year = float(row["year"])
            if not year.is_integer():
                raise ValueError
        except (TypeError, ValueError, OverflowError) as err:
            raise ValueError(f"Invalid reporting year {row['year']!r}.") from err
        if year in YEARS:
            selected.append({**row, "year": int(year), "country": typed_label(row["country"]).upper(),
                             "pathway_name_default": pathway, "trade_adj": stage, "scenathon_id": event,
                             "snapshot_id": snapshot})
    raw = engine.calculate(selected, release.weights, benchmark=release.benchmark,
                           pathway=pathway, trade_adj=stage, scenathon_id=event, snapshot_id=snapshot)
    if len(selected) != len(release.weights.participant_ids) * len(YEARS):
        raise ValueError(f"Unexpected number of reporting rows: {len(selected)}.")
    # Store only the fields required by public views. Keep the original workbook private.
    summary = {r["participant_id"]: r["net_afolu_mtco2e"] for r in raw["summary"]}
    table = {}
    for d in raw["detail"]:
        p = d["participant_id"]
        table.setdefault(d["approach"], {})[p] = {
            v: dict(allowance=d[v]["allowance_mtco2e"], gap=d[v]["gap_mtco2e"],
                    relative_gap=d[v]["relative_gap_pct"]) for v in ("base", "adjusted")
        }
    if any(not math.isclose(r["allowance_total_mtco2e"], release.weights.budget_mtco2e,
                           abs_tol=1e-6, rel_tol=0) for r in raw["reconciliation"]):
        raise ValueError("At least one set of allowances does not match the approved budget.")
    annual = {y: math.fsum(float(r[k]) for r in selected if r["year"] == y for k in RATES) for y in YEARS}
    return {"schema": 1, "event": event, "pathway": pathway, "after_trade": stage, "snapshot": snapshot,
            "snapshot_from_report": "snapshot_id" in report.fields,
            "reference_fingerprint": release.fingerprint, "benchmark_version": release.benchmark.version,
            "benchmark_snapshot": release.benchmark.snapshot_id, "budget": release.weights.budget_mtco2e,
            "period": list(engine.PERIOD), "report_hash": report.digest, "source_sheet": report.sheet,
            "reporting_rows": len(selected), "summary": summary, "table": table,
            "annual": annual, "cumulative": raw["metadata"]["selected_cohort_emissions_mtco2e"]}

def status(gap):
    if gap < 0:
        return "Above allowance"
    if gap > 0:
        return "Within allowance"
    return "At allowance"

def rows_for(run, approach, variant):
    return [{"participant": p, "emissions": e, **run["table"][approach][p][variant]}
            for p, e in run["summary"].items()]

def difference(left, right, approach, variant):
    if left["event"] != right["event"] or left["reference_fingerprint"] != right["reference_fingerprint"]:
        raise ValueError("Compare runs within the same Scenathon and approved benchmark.")
    a = {r["participant"]: r for r in rows_for(left, approach, variant)}
    b = {r["participant"]: r for r in rows_for(right, approach, variant)}
    if a.keys() != b.keys():
        raise ValueError("The participant rosters differ.")
    result = []
    for p,x in a.items():
        y = b[p]
        if x["allowance"] != y["allowance"]:
            raise ValueError(f"The fixed allowance changed for {p}. The runs cannot be compared.")
        result.append(dict(participant=p, left_emissions=x["emissions"], right_emissions=y["emissions"],
                           delta_emissions=y["emissions"]-x["emissions"], allowance=x["allowance"],
                           left_gap=x["gap"], right_gap=y["gap"], delta_gap=y["gap"]-x["gap"],
                           status_left=status(x["gap"]), status_right=status(y["gap"])))
    return result
