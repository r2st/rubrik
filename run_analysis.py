"""End-to-end pipeline runner.

Loads the dataset, applies categorization, clustering, sentiment trajectories,
generates all insights and figures, and exports CSVs/JSON to `output/`.

Usage:
    python3 run_analysis.py
"""
from __future__ import annotations

import json
from collections import Counter

import pandas as pd

from src import categorizer, clustering, config, data_loader, insights, sentiment, visualizations


def _print_header(title: str) -> None:
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def main() -> None:
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    _print_header("TRANSCRIPT INTELLIGENCE — PIPELINE")

    print("\n[1/6] Loading dataset...")
    meetings_raw = data_loader.load_all_meetings()
    df = data_loader.meetings_to_dataframe(meetings_raw)
    speakers_df = data_loader.speakers_dataframe(meetings_raw)
    sentences_df = data_loader.sentences_dataframe(meetings_raw)
    print(f"  meetings={len(df)}, sentences={len(sentences_df)}, "
          f"speaker_segments={len(speakers_df)}")

    print("\n[2/6] Categorizing...")
    df = categorizer.annotate(df)
    df["num_action_items"] = df["action_items"].apply(len)
    print(f"  call_types: {df['call_type'].value_counts().to_dict()}")
    print(f"  purposes: {df['meeting_purpose'].nunique()} unique")
    product_counts = Counter(p for areas in df["product_areas"] for p in areas)
    print(f"  product mentions: {dict(product_counts.most_common())}")

    print("\n[3/6] Sentence-level sentiment trajectories...")
    df = sentiment.add_trajectories(df, sentences_df)
    print(f"  meetings with sharp negative pivots (max_drop ≤ -0.5): "
          f"{(df['max_drop'] <= -0.5).sum()}")

    print("\n[4/6] Content clustering (silhouette-selected k)...")
    cluster_result = clustering.cluster_transcripts(df["full_transcript"])
    df["content_cluster"] = cluster_result.labels
    print(f"  optimal k={cluster_result.n_clusters} "
          f"(silhouette={cluster_result.silhouette:.3f})")
    for cid, terms in cluster_result.cluster_terms.items():
        n = (df["content_cluster"] == cid).sum()
        print(f"    cluster {cid} (n={n}): {', '.join(terms)}")

    print("\n[5/6] Insights...")
    health = insights.customer_health(df)
    incident = insights.incident_impact(df)
    ai_load = insights.action_item_load(df)
    comp = insights.competitive_signals(df)
    dominance = insights.speaker_dominance(speakers_df, df)
    pivots = insights.negative_pivots(df)

    print(f"  customer_health: {len(health)} customers, "
          f"top risk: {health.iloc[0]['customer'] if len(health) else 'n/a'}")
    print(f"  incident_impact: {incident['n_affected']}/{incident['n_total']} "
          f"meetings ({incident['affected_pct']}%) mention outage; "
          f"sentiment Δ = {incident['sentiment_unaffected'] - incident['sentiment_affected']:.2f}")
    print(f"  action_items: top owner = {ai_load.iloc[0]['owner']} "
          f"({ai_load.iloc[0]['total']})")
    print(f"  competitive_language: {comp['n_flagged']} meetings flagged "
          f"(sentiment Δ = {comp['sentiment_other'] - comp['sentiment_flagged']:.2f})")
    print(f"  speaker_dominance: max-share avg = "
          f"{dominance['max_speaker_share'].mean():.1%}")
    print(f"  negative_pivots: {len(pivots)} meetings with sharp drops")

    print("\n[6/6] Visualizations + exports...")

    # Long-form product sentiment (one row per (meeting, product))
    product_sent = (df[["sentiment_score", "product_areas"]]
                    .explode("product_areas")
                    .rename(columns={"product_areas": "product_area"}))

    weekly = sentiment.weekly_trend(df)

    visualizations.plot_distribution(df)
    visualizations.plot_sentiment_breakdown(df, product_sent)
    visualizations.plot_sentiment_trend(weekly)
    visualizations.plot_customer_health(health)
    visualizations.plot_clusters(cluster_result.tfidf_matrix, cluster_result.labels)
    visualizations.plot_action_items(df, ai_load)
    visualizations.plot_incident_blast_radius(
        incident["affected_meetings"], incident["direct_incident_meetings"])
    visualizations.plot_sentiment_by_purpose(df)
    visualizations.plot_speaker_dynamics(dominance)
    visualizations.plot_sentiment_trajectories(df)

    # CSV exports
    df_export_cols = ["meeting_id", "title", "call_type", "meeting_purpose",
                      "primary_product", "customer", "start_time",
                      "duration_min", "num_participants", "sentiment_score",
                      "overall_sentiment", "share_negative", "max_drop",
                      "num_action_items", "content_cluster"]
    df[df_export_cols].to_csv(config.OUTPUT_DIR / "meetings_processed.csv", index=False)
    health.to_csv(config.OUTPUT_DIR / "customer_health.csv", index=False)
    pivots.to_csv(config.OUTPUT_DIR / "negative_pivots.csv", index=False)
    ai_load.to_csv(config.OUTPUT_DIR / "action_item_owners.csv", index=False)

    summary = {
        "n_meetings": len(df),
        "call_types": df["call_type"].value_counts().to_dict(),
        "date_range": [str(df["start_time"].min().date()), str(df["start_time"].max().date())],
        "clustering": {
            "k": cluster_result.n_clusters,
            "silhouette": round(cluster_result.silhouette, 3),
            "terms": {str(k): v for k, v in cluster_result.cluster_terms.items()},
        },
        "sentiment_by_call_type": df.groupby("call_type")["sentiment_score"].mean().round(2).to_dict(),
        "incident_impact": {k: v for k, v in incident.items()
                            if k not in ("direct_incident_meetings", "affected_meetings")},
        "competitive_signals": {k: v for k, v in comp.items() if k != "flagged_meetings"},
        "top_at_risk_customers": health.head(5)[["customer", "risk_score", "risk_tier"]].to_dict("records"),
    }
    with (config.OUTPUT_DIR / "analysis_results.json").open("w") as f:
        json.dump(summary, f, indent=2, default=str)

    _print_header("DONE")
    print(f"Outputs at: {config.OUTPUT_DIR}")


if __name__ == "__main__":
    main()
