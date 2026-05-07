"""End-to-end pipeline runner.

Loads the dataset, applies categorization, clustering, sentiment trajectories,
generates all insights and figures, and exports CSVs/JSON to `output/`.

Two modes:

  Default (in-memory)         python run_analysis.py
    Loads the entire dataset into pandas. Comfortable up to ~100k records.
    Required for the visualizations + clustering steps which expect a
    materialized DataFrame.

  Streaming                   python run_analysis.py --streaming --batch-size 1000
    Folds the analytical aggregates incrementally. Memory is O(batch_size)
    regardless of dataset size — required at production volume (1M+).
    Skips the clustering + visualizations stages (those need the full set);
    the streaming summary still includes call-type / purpose / sentiment
    breakdowns and the customer-health rollup.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter

from src import categorizer, clustering, config, data_loader, insights, sentiment, visualizations
from src.logging_config import configure_logging, get_logger

log = get_logger(__name__)


def streaming_mode(batch_size: int) -> None:
    """Run the analytical pipeline in streaming mode (production-volume safe)."""
    from src.repository import default_repository
    from src.streaming import streaming_analyze

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    repo = default_repository()
    log.info("Streaming mode — total=%d, batch_size=%d", repo.count(), batch_size)

    result = streaming_analyze(repo, batch_size=batch_size)
    result.write_csv(config.OUTPUT_DIR)

    summary_path = config.OUTPUT_DIR / "streaming_summary.json"
    summary_path.write_text(json.dumps(result.summary, indent=2, default=str))
    log.info("Streaming summary saved to %s", summary_path)
    log.info("  %d meetings · %d batches · %.1fs · %.0f rows/s",
             result.aggregate.n_meetings, result.n_batches,
             result.elapsed_s, result.aggregate.n_meetings / max(result.elapsed_s, 0.001))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--streaming", action="store_true",
                   help="Run in streaming mode (required at production volume).")
    p.add_argument("--batch-size", type=int, default=1000,
                   help="Streaming batch size (only with --streaming).")
    args = p.parse_args()

    configure_logging()
    if args.streaming:
        streaming_mode(args.batch_size)
        return

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    log.info("Loading dataset…")
    meetings_raw = data_loader.load_all_meetings()
    df = data_loader.meetings_to_dataframe(meetings_raw)
    speakers_df = data_loader.speakers_dataframe(meetings_raw)
    sentences_df = data_loader.sentences_dataframe(meetings_raw)
    log.info("Loaded %d meetings, %d sentences, %d speaker segments",
             len(df), len(sentences_df), len(speakers_df))

    log.info("Categorizing…")
    df = categorizer.annotate(df)
    df["num_action_items"] = df["action_items"].apply(len)
    log.info("Call types: %s", df["call_type"].value_counts().to_dict())
    log.info("Purposes: %d unique", df["meeting_purpose"].nunique())
    product_counts = Counter(p for areas in df["product_areas"] for p in areas)
    log.info("Product mentions: %s", dict(product_counts.most_common()))

    log.info("Computing sentence-level sentiment trajectories…")
    df = sentiment.add_trajectories(df, sentences_df)
    log.info("Meetings with sharp negative pivots (max_drop ≤ -0.5): %d",
             (df["max_drop"] <= -0.5).sum())

    log.info("Content clustering (silhouette-selected k)…")
    cluster_result = clustering.cluster_transcripts(df["full_transcript"])
    df["content_cluster"] = cluster_result.labels
    log.info("Optimal k=%d (silhouette=%.3f)",
             cluster_result.n_clusters, cluster_result.silhouette)
    for cid, terms in cluster_result.cluster_terms.items():
        n = (df["content_cluster"] == cid).sum()
        log.info("  cluster %d (n=%d): %s", cid, n, ", ".join(terms))

    log.info("Generating insights…")
    health = insights.customer_health(df)
    incident = insights.incident_impact(df)
    ai_load = insights.action_item_load(df)
    comp = insights.competitive_signals(df)
    dominance = insights.speaker_dominance(speakers_df, df)
    pivots = insights.negative_pivots(df)

    log.info("customer_health: %d customers, top risk: %s",
             len(health), health.iloc[0]["customer"] if len(health) else "n/a")
    log.info("incident_impact: %d/%d (%.1f%%) mention outage; sentiment Δ=%.2f",
             incident["n_affected"], incident["n_total"], incident["affected_pct"],
             incident["sentiment_unaffected"] - incident["sentiment_affected"])
    log.info("action_items: top owner = %s (%d)",
             ai_load.iloc[0]["owner"], ai_load.iloc[0]["total"])
    log.info("competitive_language: %d flagged (sentiment Δ=%.2f)",
             comp["n_flagged"], comp["sentiment_other"] - comp["sentiment_flagged"])
    log.info("speaker_dominance: avg max share = %.1f%%",
             dominance["max_speaker_share"].mean() * 100)
    log.info("negative_pivots: %d meetings with sharp drops", len(pivots))

    log.info("Generating visualizations and exports…")
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

    log.info("Done. Outputs at: %s", config.OUTPUT_DIR)


if __name__ == "__main__":
    main()
