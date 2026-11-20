"""Audience-ready charts, CSV and an offline world map."""
from functools import lru_cache
from html import escape
from io import StringIO
from pathlib import Path
import csv
import gzip
import json
import math

import plotly.graph_objects as go
from model import APPROACHES, rows_for, status, engine

INK, MUTED, GRID = "#162C3D", "#526B7C", "#E2EAEF"
BASE, ADJUSTED, FIRST, SECOND = "#A3432F", "#087468", "#547596", "#087468"
SCALE = ((0.0,"#9D2526"),(.29,"#E17365"),(.50,"#FFFFFF"),(.65,"#85C6A8"),(.8,"#279B94"),(1.0,"#184E82"))
NA = "#C7CED7"
GROUPS = {
    "AFRME": "ETH R_EAF R_MEA R_SAF R_WAF SAF", "ASEAN": "R_ASE",
    "CSAM": "ARG BRA CHL DOM GUA MEX R_CSA",
    "EURUS": "CZE DEN FIN GRE IRE NOR POL POR RUS R_EEU R_NEU SWE TUR UK",
    "NAM": "CAN USA", "RASP": "CHN IND NEP PAK R_ASP R_CAS R_SIS UZB",
}

def csv_bytes(rows):
    if not rows:
        return b""
    text = StringIO(newline="")
    w = csv.DictWriter(text, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
    return text.getvalue().encode("utf-8-sig")

def canvas(fig, height=520):
    fig.update_layout(template="plotly_white", height=height, margin=dict(l=70,r=26,t=32,b=54),
                      font=dict(color=INK, size=13), hoverlabel=dict(bgcolor="#F5FAFC", font_color=INK),
                      paper_bgcolor="white", plot_bgcolor="white",
                      legend=dict(orientation="h", x=0, y=1.025, xanchor="left", yanchor="bottom"))
    fig.update_xaxes(gridcolor=GRID, zerolinecolor="#8BA0B1")
    fig.update_yaxes(gridcolor=GRID, zerolinecolor="#8BA0B1")
    return fig

def safe_number(x, digits=1, sign=False):
    if x is None:
        return "N/A"
    x = 0 if round(x,digits) == 0 else x
    return f"{x:+,.{digits}f}" if sign and x else f"{x:,.{digits}f}"

def grouped_rows(run, approach, variant):
    covered = {p for members in GROUPS.values() for p in members.split()}
    if covered != run["summary"].keys():
        raise ValueError("The provisional display grouping does not cover this roster.")
    records = []
    for name,members in GROUPS.items():
        members = members.split()
        e = math.fsum(run["summary"][p] for p in members)
        a = math.fsum(run["table"][approach][p][variant]["allowance"] for p in members)
        g = a-e
        records.append(dict(participant=name, emissions=e, allowance=a, gap=g,
                            relative_gap=100*g/abs(e) if abs(e)>engine.GAP_ZERO else None))
    return records

def comparison_index(row):
    a = row["allowance"]
    return 1 + (row["emissions"]-a)/abs(a) if abs(a)>engine.GAP_ZERO else None

def index_chart(run, approach, regional=False, scale="Focused"):
    extractor = grouped_rows if regional else rows_for
    pairs = {v: extractor(run, approach, v) for v in ("base","adjusted")}
    names = [r["participant"] for r in pairs["base"]]
    fig = go.Figure()
    trans = (lambda z: math.asinh((z-1)/.35)) if scale == "Focused" else (lambda z:z-1)
    values = [comparison_index(row) for members in pairs.values() for row in members]
    values = [v for v in values if v is not None] + [1.]
    lo, hi = min(map(trans,values)),max(map(trans,values))
    pad=.065*max(1,hi-lo)
    lo,hi=lo-pad,hi+pad
    candidates = {1,0,.5,.8,1.2,1.5,2,3}
    for power in range(0,min(100,math.ceil(math.log10(max(abs(v)+1 for v in values)))+1)):
        candidates.update(x*10.**power for x in (-5,-2,-1,1,2,5))
    ticks = [1.]
    for x in sorted(candidates-{1}, key=lambda t: abs(trans(t))):
        if lo<=trans(x)<=hi and all(abs(trans(x)-trans(t))>.105*(hi-lo) for t in ticks):
            ticks.append(x)
    ticks.sort()
    if lo<0:
        fig.add_vrect(x0=lo,x1=0,fillcolor="#ECF5F3",opacity=.85,line_width=0,layer="below")
    for variant,colour,marker,shift in (("base",BASE,"circle",-.17),("adjusted",ADJUSTED,"diamond",.17)):
        sx,sy,xx,yy,data = [],[],[],[],[]
        for i,row in enumerate(pairs[variant]):
            idx = comparison_index(row)
            if idx is None:
                continue
            y=i+shift
            x=trans(idx)
            sx += [0,x,None]
            sy += [y,y,None]
            xx.append(x); yy.append(y)
            data.append([row["participant"],idx,row["emissions"],row["allowance"],row["gap"]])
        fig.add_trace(go.Scatter(x=sx,y=sy,mode="lines",line=dict(color=colour,width=3),
                                 showlegend=False,hoverinfo="skip",legendgroup=variant))
        fig.add_trace(go.Scatter(x=xx,y=yy,mode="markers",customdata=data,
                                 marker=dict(color=colour,size=8,symbol=marker,line=dict(color="white",width=.7)),
                                 name=variant.title(),legendgroup=variant,hovertemplate=(
                                 "<b>%{customdata[0]}</b><br>Index %{customdata[1]:.3f}"
                                 "<br>Emissions %{customdata[2]:,.1f} MtCO₂e<br>Fixed allowance %{customdata[3]:,.1f} MtCO₂e"
                                 "<br>Gap %{customdata[4]:+,.1f} MtCO₂e<extra>"+variant.title()+"</extra>")))
    fig.add_vline(x=0,line_dash="dot",line_color="#4C6679",line_width=1.4)
    canvas(fig, max(370, len(names)*22+98))
    fig.update_xaxes(title_text="Emissions-to-allowance index"+(" · focused scale" if scale=="Focused" else ""),
                     tickmode="array",tickvals=[trans(x) for x in ticks],ticktext=[f"{x:,.4g}" for x in ticks],range=[lo,hi])
    fig.update_yaxes(tickmode="array",tickvals=list(range(len(names))),ticktext=names,range=[len(names)-.52,-.48])
    return fig

def annual_chart(a, b=None):
    fig=go.Figure()
    for r, label, colour in ((a, "Selected" if b is None else "A · "+a["pathway"],FIRST),
                              (b, "B · "+(b["pathway"] if b else ""),SECOND)):
        if r is None: continue
        years=sorted(map(int,r["annual"]))
        y=[r["annual"].get(str(year),r["annual"].get(year))/1000 for year in years]
        fig.add_scatter(x=years,y=y,name=label,mode="lines+markers",line=dict(width=3,color=colour),
                        marker=dict(size=7),hovertemplate="%{x}<br>%{y:,.2f} GtCO₂e/year<extra>%{fullData.name}</extra>")
    canvas(fig,390)
    fig.update_xaxes(title_text="Reporting year",tickmode="array",tickvals=years)
    fig.update_yaxes(title_text="Annual net AFOLU · GtCO₂e/year")
    return fig

def delta_chart(rows, metric):
    field = "delta_gap" if metric=="Gap" else "delta_emissions"
    rows = sorted(rows, key=lambda r:r[field])
    values=[r[field] for r in rows]
    fig=go.Figure(go.Bar(y=[r["participant"] for r in rows], x=values, orientation="h",
                         marker_color=[FIRST if x<0 else SECOND for x in values],
                         hovertemplate="%{y}<br>%{x:+,.1f} MtCO₂e<extra></extra>"))
    canvas(fig,max(390,len(rows)*21+94))
    fig.update_xaxes(title_text=f"Change in {metric.lower()} · B − A (MtCO₂e)")
    fig.update_yaxes(autorange="reversed")
    return fig

@lru_cache(maxsize=1)
def map_shapes():
    # Offline paths are preprojected and simplified for display. Geometry
    # never feeds the scientific calculations or changes participant codes.
    asset=Path(__file__).resolve().parent/"assets"/"world_map_paths.json.gz"
    with gzip.open(asset,"rt",encoding="utf-8") as stream:
        data=json.load(stream)
    return data["crosswalk"],tuple(data["paths"])


@lru_cache(maxsize=1)
def map_definitions():
    _, shapes=map_shapes()
    return "".join(f'<path id="fable-geo-{i}" d="{d}" fill-rule="evenodd"/>'
                   for i,(_,_,d) in enumerate(shapes))


def colour(value, limit):
    if value is None:
        return NA
    t = (max(-limit,min(limit,value))/limit+1)/2
    for (a,left),(b,right) in zip(SCALE,SCALE[1:]):
        if t<=b:
            blend=(t-a)/(b-a)
            return "#"+"".join(f"{round(int(left[i:i+2],16)*(1-blend)+int(right[i:i+2],16)*blend):02x}" for i in (1,3,5))
    return SCALE[-1][1]

def gap_svg(run, approach, variant, field="relative_gap", limit=200, shared=False):
    crosswalk, shapes=map_shapes()
    rows={r["participant"]:r for r in rows_for(run,approach,variant)}
    paths, clipped = [], False
    for i,(iso,name,d) in enumerate(shapes):
        row=rows.get(crosswalk.get(iso))
        value=row[field] if row is not None else None
        clipped |= value is not None and abs(value)>limit
        info=[f"{name} ({iso})"]
        if row:
            pid=row["participant"]
            info += [pid + (" · regional result" if pid.startswith("R_") else ""),
                     f"Selected pathway emissions  {safe_number(row['emissions'])} MtCO₂e",
                     f"Fixed Current Trends allowance  {safe_number(row['allowance'])} MtCO₂e",
                     f"Signed gap  {safe_number(row['gap'],sign=True)} MtCO₂e",
                     "Relative gap  "+("N/A" if row["relative_gap"] is None else safe_number(row["relative_gap"],sign=True)+"%"),
                     status(row["gap"])]
        else:
            info.append("No mapped Fair Share result.")
        info=escape("\n".join(info),quote=True)
        owner = escape(row["participant"], quote=True) if row else ""
        tag = "use" if shared else "path"
        geometry = f'href="#fable-geo-{i}"' if shared else f'd="{d}"'
        keyboard = 'tabindex="0"' if row else ""
        paths.append(f'<{tag} {geometry} data-participant="{owner}" fill="{colour(value,limit)}" '
                     f'fill-rule="evenodd" {keyboard} data-info="{info}"><title>{escape(name)}</title></{tag}>')
    svg = '<svg viewBox="-2.72 -1.39 5.44 2.49" aria-label="Fair Share gap map">' + "".join(paths) + '</svg>'
    return svg, clipped


def world_map(run, approach, variant, field="relative_gap", limit=200):
    svg,clipped=gap_svg(run,approach,variant,field,limit)
    units="%" if field=="relative_gap" else " MtCO₂e"
    clipped = " Colours are capped at the legend limits. Click a country for its full value." if clipped else ""
    stops=",".join(f"{c} {i*100:g}%" for i,c in SCALE)
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>Fair Share world map</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;font:14px system-ui,sans-serif;color:{INK};background:#fff}}
svg{{width:100%;max-height:385px;display:block;background:#f6f9fb;border:1px solid #e2eaef;border-radius:9px}}
path{{stroke:#718398;stroke-width:.003;cursor:pointer}}path:focus,path:hover,path.active{{stroke:#1d4156;stroke-width:.012;outline:none}}
.key{{margin:13px auto 0;width:min(420px,88%);height:9px;border:1px solid #9baab7;border-radius:4px;background:linear-gradient(90deg,{stops})}}
.labels{{margin:3px auto 0;width:min(420px,88%);display:flex;justify-content:space-between;gap:5px;font-size:12px}}
small{{display:block;color:{MUTED};margin:7px 0}}
#readout{{white-space:pre-line;line-height:1.4;background:#f2f6f8;border-left:3px solid #087468;border-radius:4px;padding:10px 13px;min-height:52px}}
</style>
{svg}
<div class="key"></div><div class="labels"><span>−{limit:g}{units} · Above</span><span>0 · At</span><span>+{limit:g}{units} · Within</span></div>
<small>Grey means N/A or not mapped. Regional results are repeated on member countries.{clipped}</small>
<div id="readout" role="status" aria-live="polite">Click or focus a country to see emissions, its fixed allowance and gap.</div>
<script>
let current;
function show(el) {{
 if (current) current.classList.remove("active");
 current=el; current.classList.add("active");
 document.querySelector("#readout").textContent=el.dataset.info;
}}
document.addEventListener("click", e=>{{const el=e.target.closest("[data-info]");if(el) show(el);}});
document.addEventListener("focusin", e=>{{const el=e.target.closest("[data-info]");if(el) show(el);}});
document.addEventListener("keydown", e=>{{const el=e.target.closest("[data-info]");if(el && (e.key==="Enter"||e.key===" ")){{e.preventDefault();show(el);}}}});
</script></html>"""
