"""Open Fair Share viewer with a gated upload and publishing workspace."""
from hashlib import sha256
from pathlib import Path
import hmac
import logging
import os
import time

import pandas as pd
import streamlit as st

from model import (APPROACHES, MAX_UPLOAD_MB, difference, options, prepare_result,
                   read_report, release_for_app, rows_for, status)
from repository import Repository
from publication import (EXPLORE, COMPARE, CROSSING, MOVED_IN, MOVED_OUT, public_views, shared_compare, crossing_rows, crossing_chart, map_link_data)
from views import annual_chart, csv_bytes, delta_chart, index_chart, world_map, gap_svg, map_definitions

st.set_page_config(page_title="FABLE Fair Share", page_icon="🌐", layout="wide")
st.markdown("""
<style>
.stMainBlockContainer {max-width: 1320px; padding-top: 2.45rem}
.fs-head {border:1px solid #dce8ed;border-radius:14px;padding:1.35rem 1.65rem 1.55rem;
          background:linear-gradient(115deg,#eff6f5,#f1f5f9);margin:0 0 1.05rem;color:#162c3d}
.fs-head .eyebrow {font-size:.71rem;letter-spacing:.13em;text-transform:uppercase;color:#276757;font-weight:750}
.fs-head h1 {font-size:2.5rem;letter-spacing:-.038em;margin:.1rem 0 .25rem;padding:0;color:#162c3d}
.fs-head p {margin:0;color:#40596c}
</style>
<div class="fs-head"><div class="eyebrow">FABLE / Scenathon</div>
<h1>Fair Share Explorer</h1><p>Explore pathway emissions against the fixed Current Trends allowances.</p></div>
""", unsafe_allow_html=True)

def setting(name, default=""):
    val = os.environ.get(name)
    if val is not None:
        return val
    try:
        return str(st.secrets.get(name, default))
    except FileNotFoundError:
        return default

@st.cache_resource(show_spinner=False)
def init_repository(data, dsn, version="final-v1"):
    return Repository(Path(data) / "fair_share.sqlite3", dsn)

@st.cache_resource(show_spinner=False)
def releases():
    return release_for_app()

def stage_label(after):
    return "After trade" if after else "Before trade"

def record_label(r, short=False):
    stamp = r["published_at"].replace("T"," ")[:16] + " UTC"
    suffix = f" · {stamp} · {r['id'][:6]}"
    return (r["pathway"] if short else f"{r['pathway']} · {stage_label(r['after_trade'])}") + suffix

def selection_line(run):
    a,b=run["period"]
    return f"{run['pathway']} · {stage_label(run['after_trade'])} · {a}–{b}"

def go_plot(fig, key):
    st.plotly_chart(fig, width="stretch", theme=None, key=key,
                    config={"displaylogo":False,"toImageButtonOptions":{"scale":2}})

def number_col(label, pattern="%.1f"):
    return st.column_config.NumberColumn(label, format=pattern)

def method_select(key):
    x,y=st.columns(2)
    approach=x.selectbox("Approach",tuple(APPROACHES),format_func=lambda a:f"{a} · {APPROACHES[a]}",key=key+"approach")
    variant=y.radio("Allowance variant",("adjusted","base"),format_func=str.title,horizontal=True,key=key+"variant")
    return approach,variant

def table_for(rows, key, allow_download=True):
    table = [dict(**r,status=status(r["gap"])) for r in rows]
    frame=pd.DataFrame(table)
    order = ["participant","emissions","allowance","gap","relative_gap","status"]
    config = {"participant":"Participant", "emissions":number_col("Selected emissions (MtCO₂e)"),
              "allowance":number_col("Fixed allowance (MtCO₂e)"),"gap":number_col("Signed gap (MtCO₂e)"),
              "relative_gap":number_col("Relative gap (%)"),"status":"Status"}
    st.dataframe(frame, hide_index=True, width="stretch", column_order=order, column_config=config, height=460)
    if allow_download:
        st.download_button("Download table CSV",csv_bytes(table),file_name="fair_share_participants.csv",mime="text/csv",key=key)
    st.caption("Signed gap is fixed allowance minus selected emissions. A negative gap means emissions exceeded the allowance. Blank relative gaps are undefined at near-zero net emissions.")

def report_metrics(run):
    a,b,c,d=st.columns(4)
    year=run["period"][1]
    year_value=run["annual"].get(str(year),run["annual"].get(year))
    a.metric("Cumulative net AFOLU",f"{run['cumulative']/1000:,.2f} GtCO₂e")
    b.metric(f"Annual net AFOLU in {year}",f"{year_value/1000:,.2f} GtCO₂e/yr")
    c.metric("Fixed cumulative budget",f"{run['budget']/1000:,.2f} GtCO₂e")
    d.metric("Participants",len(run["summary"]))

def map_controls(key):
    left,right=st.columns(2)
    metric=left.selectbox("Map colour",("relative_gap","gap"),format_func=lambda x:"Relative gap (%)" if x=="relative_gap" else "Signed gap (MtCO₂e)",key=key+"metric")
    if metric=="relative_gap":
        maximum=right.number_input("Colour limit (±%)",min_value=1.,value=200.,step=25.,key=key+"percent")
    else:
        maximum=right.number_input("Colour limit (± MtCO₂e)",min_value=1.,value=10000.,step=1000.,key=key+"absolute")
    return metric,maximum

def show_map(run,a,v,field,limit):
    st.iframe(world_map(run,a,v,field,limit),height=588,alt="Interactive world map of participant allowance gaps")

# --- Fair Share parallel results ---
def parallel_index_chart(run, approaches, regional, scale):
    from plotly.subplots import make_subplots
    from views import canvas

    charts = [index_chart(run, a, regional, scale) for a in approaches]
    if len(charts) == 1:
        return charts[0]

    fig = make_subplots(rows=1, cols=len(charts), shared_yaxes=True,
                        subplot_titles=[f"{a} · {APPROACHES[a]}" for a in approaches],
                        horizontal_spacing=0.055)
    lo = min(c.layout.xaxis.range[0] for c in charts)
    hi = max(c.layout.xaxis.range[1] for c in charts)
    ticks = {x: text for c in charts
             for x, text in zip(c.layout.xaxis.tickvals, c.layout.xaxis.ticktext)}
    positions = [0.0]
    for x in sorted(ticks, key=abs):
        if all(abs(x - p) >= 0.16 * (hi - lo) for p in positions):
            positions.append(x)
    positions.sort()
    axis = charts[0].layout.yaxis
    for i, chart in enumerate(charts, 1):
        for trace in chart.data:
            trace.showlegend = i == 1 and trace.showlegend is not False
            fig.add_trace(trace, row=1, col=i)
        if lo < 0:
            fig.add_vrect(x0=lo, x1=0, fillcolor="#ECF5F3", opacity=.85,
                          line_width=0, layer="below", row=1, col=i)
        fig.add_vline(x=0, line_dash="dot", line_color="#4C6679", row=1, col=i)
        fig.update_xaxes(range=[lo, hi], tickmode="array", tickvals=positions,
                         ticktext=[ticks.get(p, "1") for p in positions], row=1, col=i)
        fig.update_yaxes(tickmode="array", tickvals=axis.tickvals, ticktext=axis.ticktext,
                         range=axis.range, showticklabels=(i == 1), row=1, col=i)
    canvas(fig, charts[0].layout.height)
    fig.update_annotations(font_size=13)
    fig.update_layout(margin=dict(l=65, r=22, t=72, b=48),
                      legend=dict(y=1.08, groupclick="togglegroup"))
    return fig


# --- Map presentation ---
def map_presentation(run, approaches, variant, field, limit, linked=False):
    from html import escape
    from views import SCALE

    tiles = []
    for a, name in APPROACHES.items():
        svg, _ = gap_svg(run, a, variant, field, limit, shared=True)
        tiles.append(
            f'<section data-approach="{a}" data-inline="{int(a in approaches)}"'
            f'{" hidden" if a not in approaches else ""}>'
            f'<h3>{a} · {escape(name)}</h3>{svg}</section>'
        )
    units = "%" if field == "relative_gap" else " MtCO₂e"
    key = "".join((
        '<div class="key" style="background:linear-gradient(90deg,',
        ",".join(f"{color} {pos*100:g}%" for pos, color in SCALE),
        ')"></div><div class="labels">',
        f"<span>−{limit:g}{units} · Above</span><span>0 · At allowance</span>",
        f"<span>+{limit:g}{units} · Within</span></div>",
    ))
    
    link_payload = map_link_data(run, variant, linked)
    definitions = ('<svg width="0" height="0" aria-hidden="true" class="geometry">'
                   '<defs>' + map_definitions() + '</defs></svg>')
    return """<!doctype html><html lang="en"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<style>
*{box-sizing:border-box}
body{margin:0;color:#162c3d;background:#fff;font:14px system-ui,sans-serif}
#board{background:#fff;padding:4px}
#board:fullscreen,.standalone #board{padding:clamp(12px,2vw,34px);overflow:auto;width:100%;height:100%}
.bar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:9px}
.actions{display:flex;align-items:center;gap:8px}
h2,h3{margin:0}h2{font-size:17px}h3{font-size:14px;margin-bottom:7px}
button,select{background:#fff;color:#162c3d;border:1px solid #a9bbc7;border-radius:6px;padding:7px 10px;font:inherit}
button{cursor:pointer}
#cards{display:grid;gap:12px;grid-template-columns:repeat(var(--count,3),minmax(0,1fr))}
section{min-width:0}section[hidden],a[hidden],select[hidden]{display:none!important}
svg{display:block;width:100%;height:auto;max-height:350px;background:#f6f9fb;border:1px solid #e2eaef;border-radius:8px}
#board:fullscreen svg,.standalone svg{max-height:65vh}
svg.geometry{position:absolute;width:0;height:0;border:0;overflow:hidden;pointer-events:none}
use{stroke:#718398;stroke-width:.003;cursor:pointer}
use:focus,use:hover,use.active{stroke:#1d4156;stroke-width:.012;outline:none}
.key,.labels{width:min(480px,95%);margin:10px auto 0}
.key{height:9px;border:1px solid #9baab7;border-radius:4px}
.labels{margin-top:4px;display:flex;justify-content:space-between;gap:8px;font-size:12px}
small{display:block;color:#526b7c;margin:8px 0}
#readout{white-space:pre-line;background:#f2f6f8;border-left:3px solid #087468;border-radius:4px;padding:10px 12px}


use.linked{stroke:#312764;stroke-width:.012;paint-order:stroke}
</style>
<main id="board">""" + definitions + """
  <div class="bar"><h2>Fair Share maps</h2><div class="actions">
    <select id="slide" aria-label="Map slide" hidden>
      <option value="all">All three</option><option>AP</option><option>HR</option><option>EQ</option>
    </select>
    <button type="button" id="expand">⛶ Full screen</button>
    <a id="fallback" target="_blank" rel="noopener" hidden>Open maps in a new tab</a>
  </div></div>
  <div id="cards" style="--count:""" + str(len(approaches)) + ';">' + "".join(tiles) + "</div>" + key + """
  <small>Shared scale. Grey means N/A or not mapped. Colours are capped at the legend limits.</small>
  <div id="readout" role="status" aria-live="polite">Click or focus a country on any map to see its Fair Share result.</div>
</main>
""" + link_payload + """
<script>
const board = document.querySelector("#board");
const cards = document.querySelector("#cards");
const select = document.querySelector("#slide");
const expand = document.querySelector("#expand");
const fallback = document.querySelector("#fallback");
const standalone = window.self === window.top;
if (standalone) document.body.classList.add("standalone");

function draw() {
    const active = document.fullscreenElement === board;
    const present = active || standalone;
    let count = 0;
    for (const card of cards.children) {
        const show = present
            ? (select.value === "all" || card.dataset.approach === select.value)
            : card.dataset.inline === "1";
        card.hidden = !show;
        if (show) count++;
    }
    cards.style.setProperty("--count", count);
    select.hidden = !present;
    expand.textContent = active ? "Exit full screen" : "⛶ Full screen";
}
const readout = document.querySelector("#readout");
const linkOptions = document.getElementById("map-linked-gaps");
const linkedGaps = linkOptions ? JSON.parse(linkOptions.textContent) : null;
const linkedNodes = new Map();
if (linkedGaps) {
    for (const el of cards.querySelectorAll("use[data-participant]")) {
        const pid = el.dataset.participant;
        if (!pid) continue;
        if (!linkedNodes.has(pid)) linkedNodes.set(pid, []);
        linkedNodes.get(pid).push(el);
    }
}
let highlighted = null, previousLinked = [];
function showCountry(path) {
    if (highlighted) highlighted.classList.remove("active");
    highlighted = path;
    path.classList.add("active");
    previousLinked.forEach(el => el.classList.remove("linked"));
    const pid = path.dataset.participant;
    const gaps = linkedGaps?.[pid];
    previousLinked = linkedNodes.get(pid) || [];
    if (gaps) {
        previousLinked.forEach(el => el.classList.add("linked"));
        const fmt = new Intl.NumberFormat("en-US", {maximumFractionDigits:1, signDisplay:"exceptZero"});
        const metrics = Object.entries(gaps).map(([a,g]) => a + "  " + fmt.format(g) + " MtCO₂e");
        readout.textContent = path.dataset.info.split("\\n")[0] + " · " + pid
                            + "\\nSigned allowance gap   " + metrics.join("   |   ");
        return;
    }
    const approach = path.closest("section").dataset.approach;
    readout.textContent = approach + " · " + path.dataset.info;
}
for (const name of ["click", "focusin", "keydown"]) {
    cards.addEventListener(name, event => {
        const path = event.target.closest("use[data-info]");
        if (!path || (name === "keydown" && !["Enter", " "].includes(event.key))) return;
        if (name === "keydown") event.preventDefault();
        showCountry(path);
    });
}
select.addEventListener("change", draw);
document.addEventListener("fullscreenchange", draw);
expand.addEventListener("click", async () => {
    if (document.fullscreenElement === board) {
        await document.exitFullscreen();
        return;
    }
    select.value = "all";
    try {
        await board.requestFullscreen();
    } catch {
        if (!fallback.href) {
            const page = "<!doctype html>" + document.documentElement.outerHTML;
            fallback.href = URL.createObjectURL(new Blob([page], {type:"text/html"}));
        }
        fallback.hidden = false;
        fallback.focus();
    }
    draw();
});
draw();
</script></html>"""


# --- Grouped Base–Adjusted range table ---
def cached_map_presentation(run, approaches, variant, field, limit, linked):
    parts = (run["event"], run["pathway"], run["after_trade"], run["snapshot"],
             run["reference_fingerprint"], run["report_hash"])
    key = parts + (tuple(approaches), variant, field, limit, linked)
    cache = st.session_state.setdefault("map_document_cache", {})
    if key not in cache:
        if len(cache) >= 3:
            cache.pop(next(iter(cache)))
        cache[key] = map_presentation(run, approaches, variant, field, limit, linked)
    return cache[key]


def grouped_range_table(run, approaches, mode, key, full, allow_download):
    from html import escape
    from math import isfinite

    fields = (
        ("weight_pct", "Fair Share %", 2),
        ("allowance", "Fixed allowance", 1),
        ("gap", "Signed gap", 1),
        ("relative_gap", "Relative gap %", 2),
    )
    variants = ("base", "adjusted") if mode == "range" else (mode,)
    search, filters = st.columns([2, 3])
    term = search.text_input("Find participant", placeholder="e.g. R_CAS",
                             key=key+"find").strip().upper()
    selection = filters.selectbox("Allowance filter", ("All participants",
                        "Above in any displayed variant", "Within in all displayed variants"),
                        key=key+"allowance_filter")

    # Older published runs don't store the weights. Only show them if the
    # registered release matches the exact fingerprint saved with the result.
    try:
        release = releases().get(run["event"])
    except (OSError, ValueError, KeyError):
        release = None
    weights = (release.weights.percentages
               if release and release.fingerprint == run["reference_fingerprint"] else {})

    def finite(x):
        return None if x is None or not isfinite(x) else x

    def fmt(x, digits):
        return "N/A" if x is None else f"{0 if round(x, digits) == 0 else x:,.{digits}f}"

    head = "".join(
        f'<th scope="colgroup" colspan="{len(fields)}" class="edge">{a} · {escape(APPROACHES[a])}</th>'
        for a in approaches
    )
    labels = "".join(
        f'<th scope="col" class="{"edge" if i == 0 else ""}">{label}</th>'
        for a in approaches for i, (_, label, _) in enumerate(fields)
    )
    body, exported = [], []
    for pid, emissions in run["summary"].items():
        if term not in pid:
            continue
        lookup = {a: run["table"][a][pid] for a in approaches}
        above = any(lookup[a][v]["gap"] < 0 for a in approaches for v in variants)
        if (selection.startswith("Above") and not above) or (selection.startswith("Within") and above):
            continue
        cells = [f'<th scope="row">{escape(pid)}</th>', f'<td>{fmt(emissions, 1)}</td>']
        out = {"participant": pid, "emissions_mtco2e": emissions}
        for a in approaches:
            weight = weights.get((pid, a), (None, None))
            for i, (field, _, digits) in enumerate(fields):
                b, j = weight if field == "weight_pct" else (
                    lookup[a]["base"][field], lookup[a]["adjusted"][field]
                )
                b, j = finite(b), finite(j)
                available = [x for x in (b, j) if x is not None]
                lo, hi = (min(available), max(available)) if available else (None, None)
                prefix = f"{a}_{field}"
                if mode == "range":
                    out.update({prefix+"_min": lo, prefix+"_max": hi})
                    left, right = fmt(lo, digits), fmt(hi, digits)
                    text = left if left == right else f"{left} to {right}"
                    if len(available) == 1:
                        text += " †"
                    visible = available
                else:
                    value = b if mode == "base" else j
                    out[prefix+"_"+mode] = value
                    text = fmt(value, digits)
                    visible = [] if value is None else [value]

                cls = ["edge"] if i == 0 else []
                if field in {"gap", "relative_gap"} and visible:
                    if max(visible) < 0:
                        cls.append("over")
                    elif min(visible) > 0:
                        cls.append("within")
                tip = escape(f"Base: {fmt(b, digits)} | Adjusted: {fmt(j, digits)}", quote=True)
                cells.append(f'<td class="{" ".join(cls)}" title="{tip}">{text}</td>')
        body.append("<tr>"+"".join(cells)+"</tr>")
        exported.append(out)

    if not exported:
        st.info("No participants match these filters.")
        return
    st.caption(f"{len(exported)} of {len(run['summary'])} participants · Cumulative MtCO₂e unless marked %.")
    height = 620 if full else 460
    st.html("""
<style>
.fs-range{border:1px solid #d9e4eb;border-radius:8px;overflow:auto}
.fs-range table{width:100%;border-spacing:0;border-collapse:separate;font:12px system-ui;color:#162c3d}
.fs-range th,.fs-range td{padding:7px 8px;border-bottom:1px solid #e6edf1;text-align:right}
.fs-range thead th{position:sticky;background:#eaf2f5;text-align:center;z-index:2}
.fs-range thead tr:first-child th{top:0;height:34px}
.fs-range thead tr:nth-child(2) th{top:34px}
.fs-range thead th:first-child[rowspan]{left:0;z-index:4}
.fs-range tbody th{position:sticky;left:0;background:#f5f8fa;text-align:left;z-index:1}
.fs-range tbody tr:nth-child(even) td{background:#f7f9fb}
.fs-range .edge{border-left:2px solid #cbdde5}
.fs-range .over{color:#9d2e31}.fs-range .within{color:#097063}
.fs-range td[title]{cursor:help;font-variant-numeric:tabular-nums}
</style>
""" + f'<div class="fs-range" style="max-height:{height}px" tabindex="0">'
      f'<table style="min-width:{150 + 305 * len(approaches)}px" aria-label="Fair Share by approach">'
      f'<thead><tr><th rowspan="2" scope="col">Participant</th>'
      f'<th rowspan="2" scope="col">Selected emissions</th>{head}</tr>'
      f'<tr>{labels}</tr></thead><tbody>{"".join(body)}</tbody></table></div>')
    st.caption("Each range spans Base and Adjusted within the same approach. Hover to see each value. "
               "Negative gaps mean emissions exceed the allowance. † Only one value is defined.")
    if not weights:
        st.caption("Fair Share percentages are unavailable for this saved release. Its stored results are unchanged.")
    if allow_download:
        st.download_button("Download filtered CSV", csv_bytes(exported),
                           "fair_share_participant_ranges.csv", "text/csv", key=key+"parallel_csv")


def detail_view(run, key, allow_download=True):
    st.caption(selection_line(run))
    report_metrics(run)

    panel_key = key + "parallel_panel"
    settings = public_views(run)
    allowed = settings["explore"]
    if not allowed:
        st.info("No public Explore views are enabled for this snapshot.")
        return
    tab_key = key + "tab"
    if st.session_state.get(tab_key) not in allowed:
        st.session_state.pop(tab_key, None)
    with st.container(key=panel_key):
        nav, control = st.columns([5, 1])
        tab = nav.radio("Result view", allowed,
                        horizontal=True, key=key + "tab", label_visibility="collapsed")
        full = (control.toggle("Full screen", key=key + "full")
                if tab != "Map" else False)

        if full:
            st.html(f"""<style>
                .st-key-{panel_key} {{
                    position:fixed!important; inset:0!important; z-index:999999!important;
                    width:100vw!important; max-width:none!important; height:100vh!important;
                    height:100dvh!important; box-sizing:border-box!important; margin:0!important;
                    padding:16px clamp(14px, 2vw, 38px)!important; background:white!important;
                    overflow:auto!important;
                }}
                header[data-testid="stHeader"] {{ visibility:hidden!important; }}
            </style>""")
            st.caption(selection_line(run))

        if tab == "Annual trend":
            go_plot(annual_chart(run), key + "trend")
            return

        selector, variant_box = st.columns([3, 2])
        chosen = selector.multiselect("Approaches", list(APPROACHES), default=list(APPROACHES),
                                      format_func=lambda a: f"{a} · {APPROACHES[a]}",
                                      key=key + "parallel_approaches")
        approaches = [a for a in APPROACHES if a in chosen]
        variant = "adjusted"
        if tab == "Participants":
            variant = variant_box.radio(
                "Table values", ("range", "base", "adjusted"), horizontal=True,
                format_func=lambda x: "Min–max" if x == "range" else x.title(),
                key=key + "table_values",
            )
        elif tab != "Comparison plot":
            variant = variant_box.radio(
                "Allowance variant", ("adjusted", "base"), format_func=str.title,
                horizontal=True, key=key + "variant",
            )
        if not approaches:
            st.info("Select at least one approach.")
            return

        if tab == "Map":
            field, limit = map_controls(key + "map")
            st.iframe(cached_map_presentation(run, approaches, variant, field, limit, settings["linked_maps"]),
                      height=450, alt="AP, HR and EQ Fair Share maps with map-only full screen")

        elif tab == "Participants":
            grouped_range_table(run, approaches, variant, key, full, allow_download)

        else:
            left, right = st.columns(2)
            region = left.radio("View", ("Participants", "Regional overview"),
                                horizontal=True, key=key + "group")
            scale = right.radio("Index scale", ("Focused", "Linear"),
                                horizontal=True, key=key + "scale")
            st.caption("Base and Adjusted are shown together. Index 1 means emissions match the allowance.")
            try:
                go_plot(parallel_index_chart(run, approaches, region == "Regional overview", scale),
                        key + "parallel_index")
            except ValueError as exc:
                st.warning(str(exc))
            if region == "Regional overview":
                st.caption("Provisional display groups. Index uses summed emissions and allowances.")

        with st.expander("Benchmark and report information"):
            st.caption("Fixed allowances use the approved initial Current Trends submissions "
                       "before trade adjustment. New uploads do not recalculate them.")
            st.text(f"Benchmark {run['benchmark_version']}\nSnapshot {run['snapshot']}\n"
                    f"Validated reporting rows {run['reporting_rows']}")


def publication_controls(current, key):
    explore = st.multiselect("Public Explore views", EXPLORE, default=current["explore"], key=key+"explore")
    compare_views = st.multiselect("Public Compare views", COMPARE, default=current["compare"], key=key+"compare",
                                   help="Both snapshots must allow a view before it appears in public Compare.")
    linked = st.checkbox("Link country selections across AP, HR and EQ maps",
                         value=current["linked_maps"], key=key+"linked", disabled="Map" not in explore)
    return {"explore": explore, "compare": compare_views, "linked_maps": linked}


def crossing_view(rows, approach, variant):
    data = crossing_rows(rows)
    count_in = sum(r["transition"] == MOVED_IN for r in data)
    count_out = sum(r["transition"] == MOVED_OUT for r in data)
    left, right, steady = st.columns(3)
    left.metric("Moved within allowance", count_in)
    right.metric("Moved above allowance", count_out)
    steady.metric("No boundary change", len(data)-count_in-count_out)
    selected = st.radio("Participants to show", ("Crossed only", "All participants", MOVED_IN, MOVED_OUT),
                        horizontal=True, key="crossing_filter")
    if selected == "Crossed only":
        shown = [r for r in data if r["transition"] in (MOVED_IN, MOVED_OUT)]
    elif selected == "All participants":
        shown = data
    else:
        shown = [r for r in data if r["transition"] == selected]
    st.caption(f"{approach} · {variant.title()} · shared fixed allowance. Zero means at allowance. "
               "Crossers move between above allowance and at or within allowance.")
    if shown:
        go_plot(crossing_chart(shown), "compare_crossing")
    else:
        st.info("No participants match this transition filter. Select All participants to see how gaps moved.")


def choose_event(catalogue,key):
    titles={r["event_id"]:r["title"] for r in catalogue}
    event=st.selectbox("Scenathon",tuple(titles),format_func=titles.get,key=key)
    return [r for r in catalogue if r["event_id"]==event]

def overview(repo,catalogue):
    if not catalogue:
        st.info("No results have been published yet. Davide can upload and publish a validated report in Admin.")
        return
    query=st.query_params.get("run")
    query_row=next((r for r in catalogue if r["id"]==query),None)
    titles={r["event_id"]:r["title"] for r in catalogue}
    ids=list(titles)
    filters=st.columns(4)
    event=filters[0].selectbox("Scenathon",ids,format_func=titles.get,
        index=ids.index(query_row["event_id"]) if query_row else 0, key="explore_event")
    current=[r for r in catalogue if r["event_id"]==event]
    pathways=list(dict.fromkeys(r["pathway"] for r in current))
    pidx=pathways.index(query_row["pathway"]) if query_row and query_row["pathway"] in pathways else 0
    pathway=filters[1].selectbox("Pathway",pathways,index=pidx,key=f"explore_path_{event}")
    current=[r for r in current if r["pathway"]==pathway]
    stages=sorted({r["after_trade"] for r in current})
    sidx=stages.index(query_row["after_trade"]) if query_row and query_row["after_trade"] in stages else 0
    stage=filters[2].selectbox("Trade stage",stages,format_func=stage_label,index=sidx,key=f"explore_stage_{event}_{pathway}")
    by_id={r["id"]:r for r in current if r["after_trade"]==stage}
    ids=list(by_id)
    run_id=filters[3].selectbox("Published snapshot",ids,index=ids.index(query) if query in ids else 0,
        format_func=lambda k:record_label(by_id[k],True),key=f"explore_snap_{event}_{pathway}_{stage}")
    if run_id!=query:
        st.query_params["run"]=run_id
    run=repo.get(run_id)
    if run:
        detail_view(run,"overview_")
    else:
        st.info("This result is no longer public.")

def compatible(a,b,mode):
    if a["id"]==b["id"] or a["reference"]!=b["reference"]:
        return False
    same_path=a["pathway"]==b["pathway"]
    same_stage=a["after_trade"]==b["after_trade"]
    if mode=="Pathways":
        return not same_path and same_stage
    if mode=="Trade stages":
        return same_path and not same_stage
    return same_path and same_stage

def compare(repo,catalogue):
    if not catalogue:
        st.info("Publish at least two matched results to compare them.")
        return
    pool=choose_event(catalogue,"compare_event")
    mode=st.radio("Compare",("Pathways","Trade stages","Snapshots"),horizontal=True,key="compare_mode")
    if mode=="Trade stages":
        pool_a=[a for a in pool if not a["after_trade"]]
    else:
        pool_a=pool
    valid={r["id"]:r for r in pool_a if any(compatible(r,b,mode) for b in pool)}
    if not valid:
        st.info({"Pathways":"Publish two distinct pathways at the same trade stage with the same benchmark.",
                 "Trade stages":"Publish before- and after-trade results for the same pathway and benchmark.",
                 "Snapshots":"Publish two distinct exports for the same pathway, trade stage and benchmark."}[mode])
        return
    c1,c2=st.columns(2)
    labels=lambda r:record_label(r)
    default_a=next((k for k,r in valid.items() if r["pathway"].casefold()=="current trends"),next(iter(valid)))
    a_id=c1.selectbox("A · Reference run",list(valid),index=list(valid).index(default_a),format_func=lambda k:labels(valid[k]),key="compare_a_"+mode)
    choices={b["id"]:b for b in pool if compatible(valid[a_id],b,mode)}
    b_id=c2.selectbox("B · Comparison run",list(choices),format_func=lambda k:labels(choices[k]),key="compare_b_"+a_id)
    a,b=repo.get_pair(a_id,b_id)
    if a is None or b is None:
        st.info("A selected result is no longer public.")
        return
    available = shared_compare(a,b)
    if not available:
        st.info("These snapshots have no public Compare views in common. Choose another pair.")
        return
    approach,variant=method_select("compare_")
    try:
        rows=difference(a,b,approach,variant)
    except ValueError as e:
        st.warning(str(e))
        return
    metric=st.columns(3)
    metric[0].metric("Change in cumulative net AFOLU",f"{(b['cumulative']-a['cumulative'])/1000:+,.2f} GtCO₂e")
    y=str(a["period"][1])
    metric[1].metric(f"Change in {y} annual net AFOLU",f"{(b['annual'][y]-a['annual'][y])/1000:+,.2f} GtCO₂e/yr")
    metric[2].metric("Participants changing status",sum(r["status_left"]!=r["status_right"] for r in rows))
    st.caption("Changes are B minus A. Both runs use the same fixed allowances. A positive gap change means emissions moved down relative to the allowance.")
    if st.session_state.get("compare_view") not in available:
        st.session_state.pop("compare_view", None)
    page=st.radio("Comparison view",available,
                  horizontal=True,key="compare_view",label_visibility="collapsed")
    if page=="Change table":
        names={"participant":"Participant","left_emissions":"A emissions","right_emissions":"B emissions",
               "delta_emissions":"Δ emissions","allowance":"Fixed allowance","left_gap":"A gap",
               "right_gap":"B gap","delta_gap":"Δ gap","status_left":"A status","status_right":"B status"}
        config={k:(number_col(label) if k not in {"participant","status_left","status_right"} else label) for k,label in names.items()}
        st.caption("All amounts in this table are cumulative MtCO₂e.")
        st.dataframe(pd.DataFrame(rows),hide_index=True,width="stretch",column_config=config,height=460)
        st.download_button("Download comparison CSV",csv_bytes(rows),"fair_share_comparison.csv","text/csv")
    elif page=="Change plot":
        measure=st.radio("Plot change in",("Gap","Emissions"),horizontal=True)
        go_plot(delta_chart(rows,measure),"compare_delta")
    elif page=="Side-by-side maps":
        field,limit=map_controls("compare_map_")
        cols=st.columns(2)
        for col,label,run in zip(cols,("A","B"),(a,b)):
            with col:
                st.caption(f"{label} · {selection_line(run)}")
                show_map(run,approach,variant,field,limit)
    elif page==CROSSING:
        crossing_view(rows,approach,variant)
    else:
        go_plot(annual_chart(a,b),"compare_trend")


def authenticated():
    token=setting("FABLE_ADMIN_TOKEN")
    if len(token)<24:
        st.info("Admin is disabled. Set a strong FABLE_ADMIN_TOKEN in your server settings.")
        return False
    current=sha256(token.encode()).hexdigest()
    session=st.session_state.get("admin_session")
    if session and session[0]==current and time.time()-session[1]<8*3600:
        if st.button("Sign out",key="admin_logout"):
            for key in ("admin_session","upload_cache","checked_preview","admin_book","map_document_cache"):
                st.session_state.pop(key,None)
            st.rerun()
        return True
    st.session_state.pop("admin_session",None)
    with st.form("admin_sign_in",clear_on_submit=True):
        password=st.text_input("Admin access token",type="password")
        if st.form_submit_button("Sign in"):
            if hmac.compare_digest(password.encode(),token.encode()):
                st.session_state["admin_session"]=(current,time.time())
                st.rerun()
            st.error("The access token is incorrect.")
    return False

def upload_panel(repo, catalog):
    titles = repo.titles()
    events=list(catalog)
    event=st.selectbox("Scenathon release",events,
                       format_func=lambda e:f"{titles.get(e,catalog[e].label)} · ID {e}",key="admin_event")
    release=catalog[event]
    title=st.text_input("Scenathon display name",value=titles.get(event,release.label),
                        max_chars=110,key="admin_title_"+event)
    st.caption(f"Fixed benchmark {release.benchmark.version} · {len(release.weights.participant_ids)} participants")
    file=st.file_uploader("Scenathon report (.xlsx)",type=["xlsx"],max_upload_size=MAX_UPLOAD_MB,key="admin_book",
                          help="The app reads Indicators or Indicators Report. It does not use the Trade sheet.")
    if file is None:
        st.session_state.pop("upload_cache",None)
        st.session_state.pop("checked_preview",None)
        st.info("Upload the exported workbook to select the pathway and trade stage.")
        return
    # Streamlit keeps a stable upload ID across widget reruns. Read the workbook
    # bytes only on first selection and when publishing the original.
    upload_id = getattr(file, "file_id", None)
    if upload_id is None:
        upload_id = sha256(file.getvalue()).hexdigest()
    cached=st.session_state.get("upload_cache")
    if not cached or cached[0]!=upload_id:
        st.session_state.pop("checked_preview",None)
        with st.spinner("Reading the Indicators sheet…"):
            try:
                report=read_report(file.getvalue())
            except ValueError as exc:
                st.error(str(exc))
                return
            except Exception:
                logging.getLogger(__name__).exception("Workbook read failed")
                st.error("The Indicators sheet could not be read. Export a fresh .xlsx workbook and try again.")
                return
        st.session_state["upload_cache"]=(upload_id,report)
    else:
        report=cached[1]
    digest=report.digest
    try:
        paths,_,_=options(report,event)
        if not paths:
            st.error("No pathway in this report matches the selected Scenathon release.")
            return
        key=event+"_"+digest[:12]
        path=st.selectbox("Pathway",paths,key="admin_path_"+key,
                          help="Pathway names are read from the report to avoid mislabelling results.")
        _,stages,_=options(report,event,pathway=path)
        stage=st.radio("Trade stage",stages,format_func=stage_label,horizontal=True,key="admin_stage_"+key+path)
        _,_,snapshots=options(report,event,path,stage)
        snap=st.selectbox("Workbook snapshot",snapshots,key="admin_snapshot_"+key+path+str(stage)) if snapshots else None
        missing={"scenathon_id","trade_adj"}-report.fields
        confirmed=False
        if missing:
            st.warning("Workbook metadata missing: "+", ".join(sorted(missing))+". Verify the selected export against Scenathon.")
            confirmed=st.checkbox("I verified that this report matches the selected Scenathon and trade stage.",
                                  key="admin_confirm_"+key+path+str(stage))
        selection=(digest,event,path,stage,snap,confirmed,release.fingerprint)
        if st.button("Validate and preview",type="primary",key="admin_validate",disabled=bool(missing and not confirmed)):
            st.session_state.pop("checked_preview",None)
            try:
                checked=prepare_result(report,release,event,path,stage,snap,confirmed)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.session_state["checked_preview"]=(selection,checked)
        preview=st.session_state.get("checked_preview")
        if preview and preview[0]==selection:
            run=preview[1]
            st.success(f"Private preview · {run['reporting_rows']} validated rows. The result is not public until you publish it.")
            with st.expander("Choose what to publish", expanded=True):
                visible = publication_controls(public_views(run), "draft_" + digest[:10] + "_")
                if not visible["explore"]:
                    st.info("Select at least one Explore view before publishing.")
            if st.button("Publish to public dashboard",type="primary",key="admin_publish",
                         disabled=not title.strip() or not visible["explore"]):
                try:
                    with st.spinner("Saving the results and private workbook…"):
                        run_id,created=repo.publish(title,run,file.name,file.getvalue(),visibility=visible)
                except ValueError as exc:
                    st.error(str(exc))
                except Exception:
                    logging.getLogger(__name__).exception("Publish transaction failed")
                    st.error("Publishing failed. The preview is still available. Check the app database and try again.")
                else:
                    st.success("Published successfully." if created else "This exact run is already recorded and is now public.")
                    st.link_button("Open public result",f"?run={run_id}")
            preview_run = {**run, "public_views": {**visible, "explore": list(EXPLORE)}}
            detail_view(preview_run,"preview_",allow_download=False)
    except ValueError as exc:
        st.error(str(exc))

def manage_panel(repo):
    cat=repo.catalogue(private=True)
    if not cat:
        st.caption("There are no stored snapshots.")
        return
    choices={x["id"]:x for x in cat}
    selected=st.selectbox("Stored snapshot",list(choices),format_func=lambda k:record_label(choices[k]),key="stored_run")
    r=choices[selected]
    st.caption(f"{r['title']} · {'Public' if r['published'] else 'Unpublished'}")
    if r["published"] and st.button("Unpublish this snapshot",key="admin_unpublish"):
        repo.hide(selected)
        st.rerun()
    saved = repo.get(selected, private=True)
    if saved:
        with st.expander("Edit public views", expanded=True):
            settings = publication_controls(public_views(saved), "stored_" + selected + "_")
            if st.button("Save public views", key="admin_save_views", disabled=not settings["explore"]):
                try:
                    repo.set_public_views(selected, settings)
                except ValueError as exc:
                    st.error(str(exc))
                except Exception:
                    logging.getLogger(__name__).exception("Could not save public views")
                    st.error("Could not save public views. The existing publication was unchanged.")
                else:
                    st.success("Public views saved. No re-upload or republish needed.")
    st.caption("The source workbook remains in the private database archive.")

def admin(repo):
    st.subheader("Admin workspace")
    if not authenticated():
        return
    try:
        catalog=releases()
    except Exception:
        logging.getLogger(__name__).exception("Approved release did not validate")
        st.error("Could not load the approved Fair Share releases. Check releases.json and its referenced files.")
        return
    mode=st.radio("Manage",("Upload and publish","Snapshot history"),horizontal=True,key="admin_mode")
    if mode=="Upload and publish":
        upload_panel(repo,catalog)
    else:
        manage_panel(repo)

def main():
    data=setting("FABLE_APP_DATA_DIR",str(Path(__file__).resolve().parent/"data"))
    dsn=setting("FABLE_DATABASE_URL").strip()
    hosted=setting("FABLE_HOSTED").strip().lower() in {"1","true","yes"}
    if hosted and not dsn:
        st.error("Hosted mode needs FABLE_DATABASE_URL for durable report and result storage.")
        return
    try:
        repo=init_repository(data,dsn)
        page=st.radio("Workspace",("Explore","Compare","Admin"),horizontal=True,
                      label_visibility="collapsed",key="workspace")
        cat=repo.catalogue() if page != "Admin" else ()
    except Exception:
        logging.getLogger(__name__).exception("Repository connection failed")
        st.error("The Fair Share catalogue could not be opened. Check the application database settings.")
        return
    if page=="Explore":
        overview(repo,cat)
    elif page=="Compare":
        compare(repo,cat)
    else:
        admin(repo)
    st.divider()
    st.caption("FABLE Fair Share Explorer · fixed Current Trends allowances · cumulative and annual indicators shown separately")

main()
