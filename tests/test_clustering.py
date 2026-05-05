"""Tests for the clustering module."""
from __future__ import annotations

from src import clustering


def test_cluster_transcripts_picks_k_in_range(meetings_df) -> None:
    result = clustering.cluster_transcripts(meetings_df["full_transcript"],
                                            k_range=range(4, 8))
    assert 4 <= result.n_clusters <= 7
    assert -1 <= result.silhouette <= 1
    assert len(result.labels) == len(meetings_df)


def test_cluster_terms_present_for_every_cluster(meetings_df) -> None:
    result = clustering.cluster_transcripts(meetings_df["full_transcript"],
                                            k_range=range(5, 8))
    assert set(result.cluster_terms.keys()) == set(range(result.n_clusters))
    for terms in result.cluster_terms.values():
        assert len(terms) >= 3
        assert all(isinstance(t, str) and t for t in terms)


def test_cluster_filler_words_filtered_on_real_data(meetings_df) -> None:
    """Top cluster terms should never include conversational fillers."""
    result = clustering.cluster_transcripts(meetings_df["full_transcript"],
                                            k_range=range(5, 8))
    fillers = {"yeah", "okay", "like", "just", "want"}
    for cid, terms in result.cluster_terms.items():
        leaked = fillers & set(terms)
        assert not leaked, f"cluster {cid} contains fillers: {leaked}"
