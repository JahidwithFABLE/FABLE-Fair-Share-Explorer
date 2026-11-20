"""Per-snapshot publishing options and the allowance transition chart."""
import json

EXPLORE = ("Map", "Participants", "Comparison plot", "Annual trend")
CROSSING = "Who crossed the allowance?"
COMPARE = ("Change table", "Change plot", "Side-by-side maps", "Annual trend", CROSSING)


def public_views(run):
    saved = run.get("public_views")
    if not isinstance(saved, dict):
        return {"explore": list(EXPLORE), "compare": list(COMPARE[:-1]), "linked_maps": False}
    return {
        "explore": [v for v in EXPLORE if v in saved.get("explore", [])],
        "compare": [v for v in COMPARE if v in saved.get("compare", [])],
        "linked_maps": saved.get("linked_maps") is True,
    }


def checked_public_views(views):
    if not isinstance(views, dict):
        raise ValueError("Invalid public view settings.")
    for field, options in (("explore", EXPLORE), ("compare", COMPARE)):
        entries = views.get(field)
        if not isinstance(entries, (tuple, list)) or any(v not in options for v in entries):
            raise ValueError(f"Invalid {field} views.")
    result = public_views({"public_views": views})
    if not result["explore"]:
        raise ValueError("Select at least one Explore view.")
    return result


def shared_compare(left, right):
    a, b = public_views(left)["compare"], public_views(right)["compare"]
    return [item for item in COMPARE if item in a and item in b]


MOVED_IN = "Moved within allowance"
MOVED_OUT = "Moved above allowance"
STAYED_IN = "Stayed within allowance"
STAYED_OUT = "Stayed above allowance"
TRANSITIONS = (MOVED_IN, MOVED_OUT, STAYED_IN, STAYED_OUT)
COLORS = {MOVED_IN: "#087468", MOVED_OUT: "#AA453A",
          STAYED_IN: "#8999A8", STAYED_OUT: "#8999A8"}


def change_status(a, b):
    if a < 0:
        return MOVED_IN if b >= 0 else STAYED_OUT
    return MOVED_OUT if b < 0 else STAYED_IN


def crossing_rows(rows):
    return sorted([dict(row, transition=change_status(row["left_gap"], row["right_gap"])) for row in rows],
                  key=lambda row: (-abs(row["delta_gap"]), row["participant"]))


def crossing_chart(rows):
    import plotly.graph_objects as go
    from views import canvas

    chart = go.Figure()
    for status in TRANSITIONS:
        same = [r for r in rows if r["transition"] == status]
        if same:
            xs, ys = [], []
            for r in same:
                xs.extend((r["left_gap"], r["right_gap"], None))
                ys.extend((r["participant"], r["participant"], None))
            chart.add_scatter(x=xs, y=ys, mode="lines", name=status, showlegend=False,
                              line=dict(color=COLORS[status], width=3.2), hoverinfo="skip")
    for field, label, symbol, color in (
        ("left_gap", "A · reference", "circle", "#4C7397"),
        ("right_gap", "B · comparison", "diamond", "#162C3D"),
    ):
        chart.add_scatter(
            x=[r[field] for r in rows], y=[r["participant"] for r in rows], mode="markers", name=label,
            marker=dict(size=9, symbol=symbol, color=color, line=dict(color="#fff", width=.8)),
            customdata=[[r["left_gap"], r["right_gap"], r["delta_gap"], r["transition"]] for r in rows],
            hovertemplate=("<b>%{y}</b><br>A gap %{customdata[0]:+,.1f} MtCO₂e"
                           "<br>B gap %{customdata[1]:+,.1f} MtCO₂e"
                           "<br>Change %{customdata[2]:+,.1f} MtCO₂e"
                           "<br>%{customdata[3]}<extra></extra>"),
        )
    values = [r[k] for r in rows for k in ("left_gap", "right_gap")] + [0]
    lo, hi = min(values), max(values)
    pad = max((hi-lo)*.07, 1)
    chart.add_vline(x=0, line_dash="dot", line_color="#394F60", line_width=1.4)
    canvas(chart, max(340, min(1000, 25*len(rows)+115)))
    chart.update_layout(legend=dict(orientation="h", x=0, y=1.04))
    chart.update_xaxes(title_text="Signed allowance gap (MtCO₂e) · negative = above allowance",
                       range=[lo-pad, hi+pad])
    chart.update_yaxes(categoryorder="array", categoryarray=[r["participant"] for r in rows],
                       autorange="reversed")
    return chart


def map_link_data(run, variant, enabled):
    if not enabled:
        return ""
    payload = {p: {a: run["table"][a][p][variant]["gap"]
                   for a in ("AP", "HR", "EQ")} for p in run["summary"]}
    data = json.dumps(payload, allow_nan=False, separators=(",", ":")).replace("<", "\\u003c")
    return f'<script type="application/json" id="map-linked-gaps">{data}</script>'
