"""Compare all published Scenathon pathways and trade stages against one fixed benchmark."""

from collections import defaultdict

import plotly.graph_objects as go
import pandas as pd
import streamlit as st

from model import APPROACHES, release_for_app
from publication import public_views
from views import canvas, csv_bytes


def stage_label(after):
    return "After trade" if after else "Before trade"


def run_label(run):
    return f"{run['pathway']} · {stage_label(run['after_trade'])}"


def annual_overlay(runs):
    fig = go.Figure()
    for run in runs:
        years = sorted(int(year) for year in run["annual"])
        values = [run["annual"].get(str(year), run["annual"].get(year)) / 1000 for year in years]
        fig.add_scatter(x=years, y=values, mode="lines+markers", name=run_label(run),
                        hovertemplate="%{x}: %{y:,.3f} GtCO₂e/year<extra>%{fullData.name}</extra>")
    canvas(fig, 450)
    fig.update_xaxes(title_text="Reporting year", tickmode="array", tickvals=years)
    fig.update_yaxes(title_text="Annual net AFOLU · GtCO₂e/year")
    return fig


def cumulative_comparison(runs):
    fig = go.Figure()
    fig.add_bar(x=[run_label(run) for run in runs],
                y=[run["cumulative"] / 1000 for run in runs],
                customdata=[[run["snapshot"], run["report_hash"][:10]] for run in runs],
                hovertemplate="%{x}<br>%{y:,.3f} GtCO₂e (2026–2050)"
                              "<br>Snapshot %{customdata[0]}<extra></extra>")
    fig.add_hline(y=runs[0]["budget"] / 1000, line_color="#087468", line_dash="dash",
                  annotation_text="Fixed budget", annotation_position="top left")
    canvas(fig, 490)
    fig.update_xaxes(title_text="Pathway and trade stage", tickangle=-25)
    fig.update_yaxes(title_text="Cumulative net AFOLU · GtCO₂e")
    return fig


def gap_comparison(runs, approach, variant, participant):
    fig = go.Figure()
    if participant == "All participants (heatmap)":
        participants = sorted(runs[0]["summary"])
        gaps = [[run["table"][approach][pid][variant]["gap"] for pid in participants]
                for run in runs]
        maximum = max(abs(x) for row in gaps for x in row) or 1
        fig.add_trace(go.Heatmap(
            x=participants, y=[run_label(run) for run in runs], z=gaps,
            zmin=-maximum, zmax=maximum, zmid=0, colorscale="RdBu",
            colorbar=dict(title="MtCO₂e"),
            hovertemplate="%{y}<br>%{x}<br>Signed gap %{z:+,.2f} MtCO₂e<extra></extra>"))
        canvas(fig, max(375, 65 * len(runs) + 180))
        fig.update_xaxes(title_text="Participant", tickangle=-45)
    else:
        values = [run["table"][approach][participant][variant]["gap"] for run in runs]
        labels = [run_label(run) for run in runs]
        fig.add_bar(y=labels, x=values, orientation="h",
                    marker_color=["#AA453A" if value < 0 else "#087468" for value in values],
                    hovertemplate="%{y}<br>Signed gap %{x:+,.2f} MtCO₂e<extra></extra>")
        fig.add_vline(x=0, line_dash="dash", line_color="#4C6679")
        canvas(fig, max(360, 40 * len(runs) + 170))
        fig.update_xaxes(title_text="Signed gap · MtCO₂e (negative = above allowance)")
        fig.update_yaxes(autorange="reversed")
    return fig


def comparison_rows(runs, approach, variant):
    release = release_for_app().get(runs[0]["event"])
    weights = (release.weights.percentages
               if release and release.fingerprint == runs[0]["reference_fingerprint"] else {})
    result = []
    for run in runs:
        for pid in sorted(run["summary"]):
            d = run["table"][approach][pid][variant]
            share = weights.get((pid, approach), (None, None))
            result.append({
                "pathway": run["pathway"], "trade_stage": stage_label(run["after_trade"]),
                "snapshot": run["snapshot"], "participant": pid,
                "approach": approach, "allowance_variant": variant,
                "fair_share_pct": share[0] if variant == "base" else share[1],
                "emissions_mtco2e": run["summary"][pid],
                "fixed_allowance_mtco2e": d["allowance"],
                "signed_gap_mtco2e": d["gap"],
                "relative_gap_pct": d["relative_gap"],
                "benchmark_version": run["benchmark_version"],
            })
    return result


def render_all_pathways(repo, catalogue):
    """Published-only, one-Scenathon, one-frozen-reference comparison."""
    if not catalogue:
        st.info("There are no published results for this Scenathon.")
        return
    st.caption("Compare published pathways against the same fixed Current Trends benchmark. "
               "No allowances or weights are recalculated when browsing.")
    stage_choice = st.selectbox("Trade stage", ("Before trade", "After trade", "Both stages"),
                                key="all_pathway_stage")
    scoped = [r for r in catalogue if (stage_choice == "Both stages"
              or bool(r["after_trade"]) == (stage_choice == "After trade"))]
    if not scoped:
        st.info("No published results exist for this trade stage.")
        return

    references = list(dict.fromkeys(row["reference"] for row in scoped))
    reference = (st.selectbox("Fixed benchmark release", references,
                             format_func=lambda value: value[:12] + "…", key="all_reference")
                 if len(references) > 1 else references[0])
    scoped = [r for r in scoped if r["reference"] == reference]
    pathways = sorted({r["pathway"] for r in scoped}, key=str.casefold)
    selected = st.multiselect("Pathways to compare", pathways, default=pathways,
                              key="all_pathway_names")
    if not selected:
        st.info("Select at least one pathway.")
        return

    by_combination = defaultdict(list)
    for row in scoped:
        if row["pathway"] in selected:
            by_combination[(row["pathway"], bool(row["after_trade"]))].append(row)
    chosen = []
    if any(len(items) > 1 for items in by_combination.values()):
        st.caption("The newest published export is selected by default. "
                   "Use Snapshot versions to select an older publication.")
    with st.expander("Snapshot versions", expanded=False):
        for (pathway, stage), items in sorted(by_combination.items(), key=lambda entry: (entry[0][0].casefold(), entry[0][1])):
            items.sort(key=lambda r: (r["published_at"], r["id"]), reverse=True)
            if len(items) == 1:
                chosen.append(items[0])
                st.caption(f"{pathway} · {stage_label(stage)} · latest {items[0]['published_at'][:10]}")
            else:
                options = {r["id"]: r for r in items}
                selected_id = st.selectbox(
                    f"{pathway} · {stage_label(stage)}", list(options),
                    format_func=lambda k: f"{options[k]['published_at'][:16]} · {options[k]['snapshot']}",
                    key="all_version_" + items[0]["event_id"] + "_" + pathway + "_" + str(stage))
                chosen.append(options[selected_id])
    if len(chosen) < 2:
        st.info("Publish another pathway or trade stage to compare multiple results. "
                "You can still explore the currently published selection.")
        return

    runs = [repo.get(item["id"]) for item in chosen]
    if any(run is None for run in runs):
        st.warning("A selected snapshot is no longer public. Refresh this view.")
        return
    if len({r["reference_fingerprint"] for r in runs}) != 1:
        st.error("Selected runs have different fixed benchmarks and cannot be compared.")
        return
    if len({tuple(r["period"]) for r in runs}) != 1:
        st.error("Selected runs have different reporting periods and cannot be compared.")
        return
    options = [
        ("Cumulative emissions", "Change plot"),
        ("Annual trends", "Annual trend"),
        ("Participant Fair Share gaps", "Change plot"),
        ("Full-precision comparison table", "Change table"),
    ]
    allowed = [name for name, permission in options
               if all(permission in public_views(run)["compare"] for run in runs)]
    if not allowed:
        st.info("The selected publications do not share a permitted Compare view. "
                "Admin can adjust public Compare views in Snapshot history.")
        return
    st.caption(f"Comparing {len(runs)} published pathway/stage combinations · "
               f"{len({r['pathway'] for r in runs})} pathway(s) · "
               f"fixed budget {runs[0]['budget']/1000:,.2f} GtCO₂e")
    view = st.radio("Comparison plot", allowed, horizontal=True, key="all_pathway_view")
    if view == "Cumulative emissions":
        st.plotly_chart(cumulative_comparison(runs), width="stretch", theme=None,
                        config={"displaylogo": False})
    elif view == "Annual trends":
        st.plotly_chart(annual_overlay(runs), width="stretch", theme=None,
                        config={"displaylogo": False})
    else:
        a, b = st.columns(2)
        approach = a.selectbox("Fair Share approach", list(APPROACHES),
                               format_func=lambda x: f"{x} · {APPROACHES[x]}", key="all_approach")
        variant = b.selectbox("Allowance variant", ("adjusted", "base"),
                              format_func=str.title, key="all_variant")
        if view == "Participant Fair Share gaps":
            participant = st.selectbox("Participant", ["All participants (heatmap)"] + sorted(runs[0]["summary"]),
                                       key="all_participant")
            st.plotly_chart(gap_comparison(runs, approach, variant, participant),
                            width="stretch", theme=None, config={"displaylogo": False})
        else:
            rows = comparison_rows(runs, approach, variant)
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=500,
                         column_config={
                             "fair_share_pct": st.column_config.NumberColumn("Fair Share (%)", format="%.2f"),
                             "emissions_mtco2e": st.column_config.NumberColumn("Emissions (MtCO₂e)", format="%.0f"),
                             "fixed_allowance_mtco2e": st.column_config.NumberColumn("Fixed allowance (MtCO₂e)", format="%.0f"),
                             "signed_gap_mtco2e": st.column_config.NumberColumn("Signed gap (MtCO₂e)", format="%.0f"),
                             "relative_gap_pct": st.column_config.NumberColumn("Relative gap (%)", format="%.2f"),
                         })
            st.download_button("Download comparison CSV (full precision)", csv_bytes(rows),
                               file_name="fair_share_all_pathways.csv", mime="text/csv", key="all_pathway_csv")
    st.caption("Trade stage labels describe emissions. Base/Adjusted describe Fair Share allowances. "
               "All plotted gaps use the same frozen benchmark, not a recalibrated pathway-specific one.")
