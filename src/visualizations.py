"""All plotting routines.

Each function takes the data it needs and a target path, draws the chart,
saves it, and returns the matplotlib Figure for inline notebook display.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from sklearn.decomposition import PCA

from . import config

CALL_TYPE_COLORS = {"external": "#2196F3", "internal": "#4CAF50", "support": "#FF9800"}


def _setup() -> None:
    sns.set_theme(style="whitegrid")
    plt.rcParams["figure.dpi"] = 100


def _save(fig: plt.Figure, name: str) -> Path:
    out = config.OUTPUT_DIR / name
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    return out


def plot_distribution(meetings: pd.DataFrame) -> plt.Figure:
    _setup()
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    counts = meetings["call_type"].value_counts()
    counts.plot(kind="bar", ax=axes[0],
                color=[CALL_TYPE_COLORS[c] for c in counts.index])
    axes[0].set_title("Meetings by Call Type", fontweight="bold")
    axes[0].set_xlabel(""); axes[0].set_ylabel("Count")
    axes[0].tick_params(axis="x", rotation=0)

    meetings["meeting_purpose"].value_counts().plot(kind="barh", ax=axes[1], color="#2196F3")
    axes[1].set_title("Meetings by Purpose", fontweight="bold")
    axes[1].set_xlabel("Count"); axes[1].invert_yaxis()

    plt.tight_layout()
    _save(fig, "01_distribution.png")
    return fig


def plot_sentiment_breakdown(meetings: pd.DataFrame, product_df: pd.DataFrame) -> plt.Figure:
    _setup()
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    sns.boxplot(data=meetings, x="call_type", y="sentiment_score",
                hue="call_type", palette=CALL_TYPE_COLORS, ax=axes[0], legend=False)
    axes[0].set_title("Sentiment by Call Type", fontweight="bold")
    axes[0].axhline(y=3, color="red", ls="--", alpha=0.4)
    axes[0].set_xlabel(""); axes[0].set_ylabel("Sentiment Score (1-5)")

    sns.boxplot(data=product_df, x="product_area", y="sentiment_score",
                hue="product_area", palette="Set2", ax=axes[1], legend=False)
    axes[1].set_title("Sentiment by Product Area", fontweight="bold")
    axes[1].axhline(y=3, color="red", ls="--", alpha=0.4)
    axes[1].set_xlabel(""); axes[1].set_ylabel("Sentiment Score (1-5)")

    plt.tight_layout()
    _save(fig, "02_sentiment.png")
    return fig


def plot_sentiment_trend(weekly: pd.DataFrame, outage_weeks: tuple[int, int] = (10, 12)) -> plt.Figure:
    _setup()
    fig, ax = plt.subplots(figsize=(12, 5))
    for ctype in ["internal", "external", "support"]:
        sub = weekly[weekly["call_type"] == ctype]
        ax.plot(sub["week"], sub["sentiment_score"], marker="o",
                linewidth=2, label=ctype, color=CALL_TYPE_COLORS[ctype])
    ax.axvspan(*outage_weeks, alpha=0.12, color="red", label="Detect Outage")
    ax.axhline(y=3, color="gray", ls="--", alpha=0.3)
    ax.set_xlabel("ISO Week (2026)")
    ax.set_ylabel("Mean Sentiment Score")
    ax.set_title("Sentiment Trend by Call Type", fontweight="bold")
    ax.set_ylim(1.5, 5)
    ax.legend()
    plt.tight_layout()
    _save(fig, "03_sentiment_trend.png")
    return fig


def plot_customer_health(health_df: pd.DataFrame) -> plt.Figure:
    _setup()
    fig, ax = plt.subplots(figsize=(11, 7))
    plot_df = health_df.sort_values("risk_score")
    color_map = {"🔴 high": "#f44336", "🟡 medium": "#FF9800", "🟢 low": "#4CAF50"}
    colors = [color_map[t] for t in plot_df["risk_tier"]]
    ax.barh(plot_df["customer"], plot_df["risk_score"], color=colors)
    ax.set_xlabel("Composite Risk Score (0–1)")
    ax.set_title("Customer Churn Risk Ranking", fontweight="bold")
    for thr_val in config.CHURN_RISK_THRESHOLDS.values():
        ax.axvline(x=thr_val, color="gray", ls="--", alpha=0.4)
    plt.tight_layout()
    _save(fig, "04_customer_health.png")
    return fig


def plot_clusters(tfidf_matrix, labels: np.ndarray) -> plt.Figure:
    _setup()
    pca = PCA(n_components=2, random_state=42)
    coords = pca.fit_transform(tfidf_matrix.toarray())
    fig, ax = plt.subplots(figsize=(10, 7))
    scatter = ax.scatter(coords[:, 0], coords[:, 1], c=labels,
                         cmap="tab10", alpha=0.75, s=80,
                         edgecolors="white", linewidth=0.5)
    ax.set_title("Meeting Content Clusters (TF-IDF + KMeans, PCA projection)", fontweight="bold")
    ax.set_xlabel(f"PC1 ({pca.explained_variance_ratio_[0]:.1%} var)")
    ax.set_ylabel(f"PC2 ({pca.explained_variance_ratio_[1]:.1%} var)")
    plt.colorbar(scatter, ax=ax, label="Cluster")
    plt.tight_layout()
    _save(fig, "05_clusters.png")
    return fig


def plot_action_items(meetings: pd.DataFrame, top_owners: pd.DataFrame) -> plt.Figure:
    _setup()
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    avg = meetings.groupby("call_type")["num_action_items"].mean()
    avg.plot(kind="bar", ax=axes[0],
             color=[CALL_TYPE_COLORS[c] for c in avg.index])
    axes[0].set_title("Avg Action Items per Meeting", fontweight="bold")
    axes[0].set_xlabel(""); axes[0].set_ylabel("Action Items")
    axes[0].tick_params(axis="x", rotation=0)

    axes[1].barh(top_owners["owner"], top_owners["total"], color="#2196F3")
    axes[1].set_title("Top Action Item Owners", fontweight="bold")
    axes[1].set_xlabel("Total Assigned")
    axes[1].invert_yaxis()

    plt.tight_layout()
    _save(fig, "06_action_items.png")
    return fig


def plot_incident_blast_radius(affected: pd.DataFrame, direct: pd.DataFrame) -> plt.Figure:
    _setup()
    fig, ax = plt.subplots(figsize=(13, 5))
    for ctype, color in CALL_TYPE_COLORS.items():
        sub = affected[affected["call_type"] == ctype]
        ax.scatter(sub["start_time"], sub["sentiment_score"],
                   c=color, s=70, alpha=0.7, label=ctype, zorder=4)
    ax.scatter(direct["start_time"], direct["sentiment_score"],
               marker="X", c="red", s=160, edgecolors="black",
               linewidth=1, zorder=6, label="Incident Response")
    ax.axhline(y=3, color="gray", ls="--", alpha=0.3)
    ax.set_xlabel("Date"); ax.set_ylabel("Sentiment Score")
    ax.set_title("Outage Blast Radius — Meetings Mentioning the Incident", fontweight="bold")
    ax.set_ylim(1, 5.2)
    ax.legend()
    plt.tight_layout()
    _save(fig, "07_incident_impact.png")
    return fig


def plot_sentiment_by_purpose(meetings: pd.DataFrame) -> plt.Figure:
    _setup()
    summary = (meetings.groupby("meeting_purpose")["sentiment_score"]
               .agg(["mean", "count"]).sort_values("mean"))
    fig, ax = plt.subplots(figsize=(10, 6))
    colors = ["#f44336" if m < 3 else "#FF9800" if m < 3.5 else "#4CAF50"
              for m in summary["mean"]]
    summary["mean"].plot(kind="barh", ax=ax, color=colors)
    ax.set_xlabel("Mean Sentiment Score")
    ax.set_title("Sentiment by Meeting Purpose", fontweight="bold")
    ax.axvline(x=3, color="gray", ls="--", alpha=0.5)
    ax.set_xlim(1, 5)
    for i, (_, row) in enumerate(summary.iterrows()):
        ax.text(row["mean"] + 0.05, i, f"n={int(row['count'])}",
                va="center", fontsize=9)
    plt.tight_layout()
    _save(fig, "08_sentiment_by_purpose.png")
    return fig


def plot_speaker_dynamics(dominance: pd.DataFrame) -> plt.Figure:
    _setup()
    fig, ax = plt.subplots(figsize=(10, 5))
    for ctype, color in CALL_TYPE_COLORS.items():
        sub = dominance[dominance["call_type"] == ctype]
        ax.scatter(sub["max_speaker_share"], sub["sentiment_score"],
                   c=color, alpha=0.6, s=70, label=ctype)
    ax.set_xlabel("Max Single-Speaker Share")
    ax.set_ylabel("Sentiment Score")
    ax.set_title("Speaker Dominance vs Sentiment", fontweight="bold")
    ax.legend()
    plt.tight_layout()
    _save(fig, "09_speaker_dynamics.png")
    return fig


def plot_sentiment_trajectories(meetings: pd.DataFrame, n: int = 6) -> plt.Figure:
    """Show a few example within-meeting sentiment trajectories — the new view."""
    _setup()
    selection = meetings.dropna(subset=["trajectory"]).sort_values("max_drop").head(n)
    fig, ax = plt.subplots(figsize=(11, 5))
    for _, row in selection.iterrows():
        ax.plot(row["trajectory"], marker="o",
                label=row["title"][:50] + ("…" if len(row["title"]) > 50 else ""))
    ax.axhline(y=0, color="gray", ls="--", alpha=0.4)
    ax.set_xlabel("Meeting Progression (bucket)")
    ax.set_ylabel("Mean Sentence Sentiment (-1 to +1)")
    ax.set_title("Within-Meeting Sentiment Trajectories — Sharpest Negative Pivots", fontweight="bold")
    ax.legend(loc="lower left", fontsize=8)
    plt.tight_layout()
    _save(fig, "10_sentiment_trajectories.png")
    return fig
