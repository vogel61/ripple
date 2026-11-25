"""One vocabulary for the third request class: utility work, not chat.

RIPPLE routes two kinds of request that are not a conversation — an embedding
and a rerank — and each of them is asked about in a different place: the proxy
decides whether to hold a thread for it, placement decides whether it may evict
for it, stats counts it, the dashboard shows it. If each of those keeps its own
set of model types and paths, they drift: a path gets routed but not classified,
or a model is called utility in one view and chat in the next.

So the class is named here, once, and the other modules import it. This module
is deliberately pure: it imports nothing from `ripple.proxy`, `ripple.hosts` or
`ripple.placement`, because those are the modules that must agree with it, and a
dependency in either direction would make the agreement circular.
"""

from __future__ import annotations

from typing import Any

#: The model types a node reports for work that is not a chat thread. oMLX
#: names an embedding model `embedding` and a reranker `reranker`; a chat model
#: is `llm`. Anything else the node says is not this class — see
#: `is_utility_type()`.
UTILITY_MODEL_TYPES: set[str] = {"embedding", "reranker"}

#: The request paths that carry utility work. Written without a leading slash,
#: the same shape as `PROXY_PATHS` in `ripple/proxy.py`, so the two can be
#: compared as sets without normalising either one.
UTILITY_PATHS: set[str] = {"v1/embeddings", "v1/rerank"}


def is_utility_type(model_type: str) -> bool:
    """Whether a node's model type means utility work rather than chat.

    An empty string is **not** utility. "The node did not say" is a missing
    answer, not a classification: treating it as utility would let a node that
    reports nothing slip past the thread accounting, and treating it as chat
    keeps the old behaviour for the nodes that never labelled their models.

    Compared the way the fleet compares `engine_type` elsewhere
    (`Host.model_builtin`): the node's word is stored as it arrived, and the
    comparison forgives case and surrounding space.
    """
    return model_type.strip().lower() in UTILITY_MODEL_TYPES


def is_utility_path(path: str) -> bool:
    """Whether a request path is a utility call.

    The comparison is exact: no trailing-slash or prefix folding, so a path that
    RIPPLE does not route cannot be classified as utility by accident.
    """
    return path in UTILITY_PATHS


def utility_text(body: dict[str, Any]) -> str:
    """The text of a utility request, for the log line only.

    An embeddings call logs the length of its `input`, a rerank call the length
    of its `query`; without this a rerank would log `prompt_chars=0` and look
    like an empty request. The text is not used for routing, placement or
    accounting — the node measures what it actually processed.

    `input` wins when a body carries both, because that is the key an embeddings
    call sends. A list of inputs is joined with a single space: the caller counts
    characters and truncates, so this returns the whole text rather than a
    capped copy. Token-id inputs (a list of integers) have no text to log and
    yield `""`. A malformed body — not a mapping, a missing key, a non-string
    value — also yields `""`; a log line must never fail a request.
    """
    if not isinstance(body, dict):
        return ""

    value = body.get("input")
    if value is None:
        value = body.get("query")

    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        parts = [item for item in value if isinstance(item, str)]
        return " ".join(parts)
    return ""
