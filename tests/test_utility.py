"""The one vocabulary for utility requests (docs/utility-models-plan.md, step 2).

These tests pin the module's public surface: the model types and paths that
mean utility work, and the text a log line reads out of a body. They do not
test routing or placement — later steps do that against this vocabulary.
"""

from __future__ import annotations

from ripple.utility import (UTILITY_MODEL_TYPES, UTILITY_PATHS, is_utility_path,
                            is_utility_type, utility_text)


def test_embedding_and_reranker_are_utility_types():
    assert is_utility_type("embedding")
    assert is_utility_type("reranker")
    assert UTILITY_MODEL_TYPES == {"embedding", "reranker"}


def test_a_chat_type_is_not_utility():
    assert not is_utility_type("llm")
    assert not is_utility_type("chat")


def test_the_type_is_compared_the_way_the_fleet_compares_engine_types():
    """The node's word is stored raw; the comparison forgives case and space."""
    assert is_utility_type("EMBEDDING")
    assert is_utility_type(" Reranker ")


def test_an_empty_type_is_not_utility():
    assert not is_utility_type("")


def test_both_utility_paths_are_recognised():
    assert is_utility_path("v1/embeddings")
    assert is_utility_path("v1/rerank")
    assert UTILITY_PATHS == {"v1/embeddings", "v1/rerank"}
    assert not is_utility_path("v1/chat/completions")


def test_the_log_text_of_an_embeddings_body_is_its_input():
    assert utility_text({"input": "hello"}) == "hello"
    assert utility_text({"input": ["one", "two"]}) == "one two"
    assert utility_text({}) == ""


def test_the_log_text_of_a_rerank_body_is_its_query():
    assert utility_text({"query": "what is RIPPLE?", "documents": ["a", "b"]}) == (
        "what is RIPPLE?")
    assert utility_text({"documents": ["a", "b"]}) == ""
