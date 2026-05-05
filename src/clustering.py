"""Content-based clustering with TF-IDF + KMeans.

Picks the cluster count via silhouette score over a small candidate range
rather than hard-coding `k`.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer
from sklearn.metrics import silhouette_score

from . import config


@dataclass
class ClusterResult:
    labels: np.ndarray
    n_clusters: int
    silhouette: float
    cluster_terms: dict[int, list[str]]
    tfidf_matrix: any  # scipy sparse
    vectorizer: TfidfVectorizer


def _build_vectorizer() -> TfidfVectorizer:
    stop_words = list(ENGLISH_STOP_WORDS) + config.CONVERSATIONAL_FILLERS
    return TfidfVectorizer(
        max_features=300,
        stop_words=stop_words,
        ngram_range=(1, 2),
        min_df=3,
        max_df=0.7,
    )


def cluster_transcripts(
    texts: pd.Series,
    k_range: range = range(4, 11),
    random_state: int = 42,
) -> ClusterResult:
    """Fit TF-IDF + KMeans over `k_range`, return the best by silhouette."""
    vectorizer = _build_vectorizer()
    matrix = vectorizer.fit_transform(texts)

    best: tuple[float, KMeans, int] = (-1.0, None, 0)  # type: ignore
    for k in k_range:
        km = KMeans(n_clusters=k, random_state=random_state, n_init=10)
        labels = km.fit_predict(matrix)
        score = silhouette_score(matrix, labels)
        if score > best[0]:
            best = (score, km, k)

    score, model, k = best
    feature_names = vectorizer.get_feature_names_out()
    cluster_terms: dict[int, list[str]] = {}
    for i in range(k):
        top_idx = model.cluster_centers_[i].argsort()[-6:][::-1]
        cluster_terms[i] = [feature_names[idx] for idx in top_idx]

    return ClusterResult(
        labels=model.labels_,
        n_clusters=k,
        silhouette=float(score),
        cluster_terms=cluster_terms,
        tfidf_matrix=matrix,
        vectorizer=vectorizer,
    )
