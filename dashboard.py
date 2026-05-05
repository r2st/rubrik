"""Streamlit dashboard for Transcript Intelligence.

Reuses the same `src/` modules as the notebook and CLI pipeline — single source
of truth for categorization, sentiment, clustering, and insights. The dashboard
is just a thin presentation layer over those.

Run:  streamlit run dashboard.py
"""
from __future__ import annotations

from collections import Counter

import pandas as pd
import plotly.express as px
import streamlit as st

from src import categorizer, clustering, config, data_loader, insights, sentiment

# ---------------------------------------------------------------------------
# Page setup
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Transcript Intelligence",
    page_icon="📞",
    layout="wide",
)

CALL_TYPE_COLORS = {"external": "#2196F3", "internal": "#4CAF50", "support": "#FF9800"}


# ---------------------------------------------------------------------------
# Data loading (cached)
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner="Loading transcripts and running pipeline…")
def load_pipeline() -> dict:
    """Run the full pipeline once, cache the results."""
    raw = data_loader.load_all_meetings()
    df = data_loader.meetings_to_dataframe(raw)
    sentences_df = data_loader.sentences_dataframe(raw)
    speakers_df = data_loader.speakers_dataframe(raw)

    df = categorizer.annotate(df)
    df["num_action_items"] = df["action_items"].apply(len)
    df = sentiment.add_trajectories(df, sentences_df)

    cluster_result = clustering.cluster_transcripts(df["full_transcript"])
    df["content_cluster"] = cluster_result.labels

    health = insights.customer_health(df)
    incident = insights.incident_impact(df)
    ai_load = insights.action_item_load(df, top_n=15)
    comp = insights.competitive_signals(df)
    dominance = insights.speaker_dominance(speakers_df, df)
    pivots = insights.negative_pivots(df)

    return {
        "df": df, "sentences_df": sentences_df, "speakers_df": speakers_df,
        "cluster_result": cluster_result,
        "health": health, "incident": incident, "ai_load": ai_load,
        "comp": comp, "dominance": dominance, "pivots": pivots,
    }


data = load_pipeline()
df = data["df"]


# ---------------------------------------------------------------------------
# Sidebar filters
# ---------------------------------------------------------------------------
st.sidebar.header("Filters")

call_types = st.sidebar.multiselect(
    "Call type", sorted(df["call_type"].unique()),
    default=sorted(df["call_type"].unique()),
)

products = sorted({p for areas in df["product_areas"] for p in areas})
selected_products = st.sidebar.multiselect("Product area", products, default=products)

date_range = st.sidebar.date_input(
    "Date range",
    value=(df["start_time"].min().date(), df["start_time"].max().date()),
    min_value=df["start_time"].min().date(),
    max_value=df["start_time"].max().date(),
)

# Apply filters
mask = df["call_type"].isin(call_types)
mask &= df["product_areas"].apply(lambda areas: bool(set(areas) & set(selected_products)))
if isinstance(date_range, tuple) and len(date_range) == 2:
    start, end = date_range
    mask &= (df["start_time"].dt.date >= start) & (df["start_time"].dt.date <= end)

filtered = df[mask].reset_index(drop=True)
st.sidebar.metric("Meetings (filtered)", len(filtered))


# ---------------------------------------------------------------------------
# Header KPIs
# ---------------------------------------------------------------------------
st.title("📞 Transcript Intelligence")
st.caption("AegisCloud meeting transcripts · Feb–Apr 2026 · 100 meetings")

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Meetings", len(filtered))
k2.metric("Avg sentiment", f"{filtered['sentiment_score'].mean():.2f}" if len(filtered) else "—")
k3.metric("Customers", filtered["customer"].nunique())
k4.metric("Action items", int(filtered["num_action_items"].sum()))
k5.metric("Negative pivots", int((filtered["max_drop"] <= -0.5).sum()))

st.divider()


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab_overview, tab_customers, tab_incident, tab_meeting, tab_clusters = st.tabs([
    "Overview", "Customers (at risk)", "Incident impact",
    "Meeting drill-down", "Topics & clusters",
])

# ---------- OVERVIEW ----------
with tab_overview:
    c1, c2 = st.columns(2)

    with c1:
        st.subheader("Sentiment by call type")
        if len(filtered):
            fig = px.box(
                filtered, x="call_type", y="sentiment_score",
                color="call_type", color_discrete_map=CALL_TYPE_COLORS,
                points="all", height=400,
            )
            fig.add_hline(y=3, line_dash="dash", line_color="red", opacity=0.5)
            fig.update_layout(showlegend=False, yaxis_title="Score (1–5)")
            st.plotly_chart(fig, use_container_width=True)

    with c2:
        st.subheader("Sentiment by meeting purpose")
        if len(filtered):
            purpose_summary = (filtered.groupby("meeting_purpose")["sentiment_score"]
                               .agg(["mean", "count"]).reset_index()
                               .sort_values("mean"))
            fig = px.bar(
                purpose_summary, x="mean", y="meeting_purpose",
                orientation="h", color="mean",
                color_continuous_scale=["#f44336", "#FF9800", "#4CAF50"],
                range_color=[1, 5], height=400,
                hover_data={"count": True},
            )
            fig.add_vline(x=3, line_dash="dash", line_color="gray", opacity=0.5)
            fig.update_layout(yaxis_title="", xaxis_title="Mean sentiment", coloraxis_showscale=False)
            st.plotly_chart(fig, use_container_width=True)

    st.subheader("Sentiment trend over time")
    weekly = sentiment.weekly_trend(filtered) if len(filtered) else pd.DataFrame()
    if len(weekly):
        fig = px.line(
            weekly, x="week", y="sentiment_score", color="call_type",
            markers=True, color_discrete_map=CALL_TYPE_COLORS, height=350,
        )
        fig.add_vrect(x0=10, x1=12, fillcolor="red", opacity=0.1,
                      annotation_text="Detect Outage", annotation_position="top left")
        fig.add_hline(y=3, line_dash="dash", line_color="gray", opacity=0.4)
        fig.update_layout(xaxis_title="ISO Week (2026)", yaxis_title="Mean sentiment")
        st.plotly_chart(fig, use_container_width=True)

    st.subheader("Distribution")
    c1, c2 = st.columns(2)
    with c1:
        ct = filtered["call_type"].value_counts().reset_index()
        ct.columns = ["call_type", "count"]
        fig = px.bar(ct, x="call_type", y="count", color="call_type",
                     color_discrete_map=CALL_TYPE_COLORS, height=300)
        fig.update_layout(showlegend=False)
        st.plotly_chart(fig, use_container_width=True)
    with c2:
        product_mentions = Counter(p for areas in filtered["product_areas"] for p in areas)
        if product_mentions:
            pm = pd.DataFrame(product_mentions.most_common(), columns=["product", "count"])
            fig = px.bar(pm, x="product", y="count", color="product", height=300)
            fig.update_layout(showlegend=False)
            st.plotly_chart(fig, use_container_width=True)


# ---------- CUSTOMERS ----------
with tab_customers:
    st.subheader("Customer churn risk ranking")
    st.caption(
        "Composite score in [0, 1]. Combines (a) avg sentiment below neutral, "
        "(b) explicit churn signals, (c) within-meeting negative pivots. "
        "Weights live in `config.RISK_WEIGHTS`."
    )

    health = data["health"]
    if len(health):
        st.dataframe(
            health[["customer", "risk_tier", "risk_score", "avg_sentiment",
                    "min_sentiment", "num_meetings", "churn_signals"]],
            use_container_width=True, hide_index=True,
        )

        # Drill-down for a single customer
        st.divider()
        st.subheader("Customer drill-down")
        chosen = st.selectbox("Pick a customer", health["customer"].tolist())
        cust_meetings = df[df["customer"] == chosen].sort_values("start_time")

        cc1, cc2, cc3 = st.columns(3)
        cc1.metric("Meetings", len(cust_meetings))
        cc2.metric("Avg sentiment", f"{cust_meetings['sentiment_score'].mean():.2f}")
        cc3.metric("Risk", health.loc[health["customer"] == chosen, "risk_tier"].iloc[0])

        st.dataframe(
            cust_meetings[["start_time", "title", "meeting_purpose",
                           "sentiment_score", "max_drop", "share_negative",
                           "num_action_items"]],
            use_container_width=True, hide_index=True,
        )


# ---------- INCIDENT ----------
with tab_incident:
    st.subheader("Detect Outage — blast radius")
    inc = data["incident"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Meetings affected", f"{inc['n_affected']}/{inc['n_total']}")
    c2.metric("Affected %", f"{inc['affected_pct']}%")
    c3.metric("Direct incident-response", inc["n_direct"])
    c4.metric(
        "Sentiment drag",
        f"{inc['sentiment_unaffected'] - inc['sentiment_affected']:.2f}",
        delta=f"affected: {inc['sentiment_affected']}",
        delta_color="inverse",
    )

    affected = inc["affected_meetings"]
    direct = inc["direct_incident_meetings"]

    affected_plot = affected.copy()
    affected_plot["is_direct"] = affected_plot["meeting_id"].isin(direct["meeting_id"])
    fig = px.scatter(
        affected_plot, x="start_time", y="sentiment_score",
        color="call_type", symbol="is_direct",
        symbol_map={True: "x", False: "circle"},
        color_discrete_map=CALL_TYPE_COLORS,
        hover_data=["title", "meeting_purpose"], height=450,
    )
    fig.add_hline(y=3, line_dash="dash", line_color="gray", opacity=0.4)
    fig.update_traces(marker=dict(size=11))
    fig.update_layout(yaxis_title="Sentiment score", xaxis_title="Date")
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Direct incident-response timeline")
    st.dataframe(
        direct[["start_time", "title", "sentiment_score", "num_action_items"]],
        use_container_width=True, hide_index=True,
    )


# ---------- MEETING DRILL-DOWN ----------
with tab_meeting:
    st.subheader("Meeting drill-down")
    st.caption("Pick any meeting to inspect its sentiment trajectory and transcript.")

    options = filtered.sort_values("start_time", ascending=False)
    if not len(options):
        st.warning("No meetings match the current filters.")
    else:
        labels = options.apply(
            lambda r: f"{r['start_time'].strftime('%m/%d')} · "
                      f"{r['call_type'][:3]} · sent {r['sentiment_score']:.1f} · {r['title']}",
            axis=1,
        ).tolist()
        choice_idx = st.selectbox(
            "Meeting",
            range(len(options)), format_func=lambda i: labels[i],
        )
        chosen = options.iloc[choice_idx]

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Meeting score", f"{chosen['sentiment_score']:.1f}")
        c2.metric("Max drop (within-call)", f"{chosen['max_drop']:.2f}"
                  if pd.notna(chosen["max_drop"]) else "—")
        c3.metric("% negative sentences", f"{chosen['share_negative']:.0%}"
                  if pd.notna(chosen["share_negative"]) else "—")
        c4.metric("Action items", int(chosen["num_action_items"]))

        st.markdown(f"**Summary:** {chosen['summary_text']}")

        if pd.notna(chosen["max_drop"]) and isinstance(chosen.get("trajectory"), list):
            traj_df = pd.DataFrame({
                "bucket": list(range(len(chosen["trajectory"]))),
                "sentiment": chosen["trajectory"],
            })
            fig = px.line(traj_df, x="bucket", y="sentiment", markers=True, height=300)
            fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.4)
            fig.update_yaxes(range=[-1.1, 1.1])
            fig.update_layout(
                title="Within-meeting sentiment trajectory",
                xaxis_title="Meeting progression (bucket)",
                yaxis_title="Mean sentence sentiment",
            )
            st.plotly_chart(fig, use_container_width=True)

        # Sentence-level table
        st.subheader("Transcript with per-sentence sentiment")
        sentences = data["sentences_df"][
            data["sentences_df"]["meeting_id"] == chosen["meeting_id"]
        ].sort_values("index")
        sent_color = {"positive": "🟢", "neutral": "⚪", "negative": "🔴"}
        sentences = sentences.copy()
        sentences["s"] = sentences["sentiment"].map(sent_color)
        st.dataframe(
            sentences[["s", "speaker", "sentence"]].rename(columns={"s": ""}),
            use_container_width=True, hide_index=True, height=400,
        )


# ---------- CLUSTERS ----------
with tab_clusters:
    cr = data["cluster_result"]
    st.subheader(f"Content clusters · k={cr.n_clusters} · silhouette={cr.silhouette:.3f}")
    st.caption("k chosen by maximizing silhouette score over k ∈ [4, 10].")

    cluster_summary = []
    for cid, terms in cr.cluster_terms.items():
        members = df[df["content_cluster"] == cid]
        cluster_summary.append({
            "cluster": cid,
            "size": len(members),
            "top_terms": ", ".join(terms),
            "dominant_purpose": members["meeting_purpose"].value_counts().index[0]
                if len(members) else "—",
            "avg_sentiment": round(members["sentiment_score"].mean(), 2)
                if len(members) else 0,
        })
    st.dataframe(pd.DataFrame(cluster_summary), use_container_width=True, hide_index=True)

    selected = st.selectbox("Inspect cluster", sorted(cr.cluster_terms.keys()))
    members = df[df["content_cluster"] == selected].sort_values("sentiment_score")
    st.dataframe(
        members[["title", "call_type", "meeting_purpose", "sentiment_score"]],
        use_container_width=True, hide_index=True,
    )
