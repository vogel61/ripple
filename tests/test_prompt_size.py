"""The size of a request is the size of the *prompt*, not of the user text.

`prompt_text()` excludes the system prompt on purpose: it is the text the
prefix match runs on, and a system prompt carries timestamps and a tool list and
would change every turn. Feeding that same text into `estimate_tokens()` was the
bug (issue #14): a real Hermes turn of 83 192 prompt tokens with a 101-character
user message was sized as **27 tokens**, so every agent request was `ephemeral`
— it held no slot, never bound, never rebound — and the prefill bias saw a
prompt far below `min_prompt_tokens` and returned 0.0.

`prompt_size_text()` is the second, different text: everything oMLX actually
prefills. `prompt_text()` is untouched.
"""

from __future__ import annotations

import pytest

from ripple.registry import (estimate_tokens, fingerprint, prompt_size_text,
                             prompt_text)

from tests.support import chat_body, host_config, make_config, prompt_for, \
    stub_model_loaded

SYSTEM = 300_000          # characters, the measured Hermes case in issue #14
USER = "wo steht die anlage in site-b?"                # 100 characters


def agent_body(*, system: int = SYSTEM, user: str = USER, model: str = "m1",
               tools: list | None = None) -> dict:
    """A real agent turn: a huge system prompt and a one-line user message."""
    body: dict = {"model": model, "messages": [
        {"role": "system", "content": "S" * system},
        {"role": "user", "content": user},
    ]}
    if tools is not None:
        body["tools"] = tools
    return body


def _two_hosts(stub_factory) -> list:
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    return [host_config("alpha", a.url, node_id=a.state.node_id, weight=3.0),
            host_config("beta", b.url, node_id=b.state.node_id, weight=2.0)]


# ---------------------------------------------------------------------------
# The two texts are different on purpose
# ---------------------------------------------------------------------------
def test_prompt_text_stays_the_user_text_only() -> None:
    """The prefix-match text must not start carrying the system prompt."""
    body = agent_body()

    assert prompt_text(body) == USER
    assert "SSSS" not in prompt_text(body)


def test_the_measured_case_is_sized_as_the_prompt_not_as_the_user_text() -> None:
    """83 k-token turn: 27 tokens before, ~75 k now (issue #14)."""
    body = agent_body()

    user_only = estimate_tokens(prompt_text(body))
    whole = estimate_tokens(prompt_size_text(body))

    assert user_only < 32                     # the "27 tokens" of the report
    assert whole > 70_000
    assert whole > user_only * 1000


def test_prompt_size_counts_every_role() -> None:
    """system, user, assistant and tool content all end up in the prompt."""
    body = {"model": "m1", "messages": [
        {"role": "system", "content": "sys " + "s" * 400},
        {"role": "user", "content": "u" * 400},
        {"role": "assistant", "content": "a" * 400},
        {"role": "tool", "content": "t" * 400},
    ]}

    size = len(prompt_size_text(body))

    assert size >= 1600, "one role was left out of the size"
    assert prompt_text(body) == "u" * 400


def test_prompt_size_counts_tool_calls_and_their_arguments() -> None:
    """A tool call is re-rendered into the next prompt by the chat template."""
    body = {"model": "m1", "messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read_file", "arguments": "x" * 4000}}]},
        {"role": "tool", "content": "ok"},
    ]}

    assert len(prompt_size_text(body)) > 4000


def test_prompt_size_counts_the_tool_schema() -> None:
    """On an agent turn the schema is not small — and it is prefilled."""
    tools = [{"type": "function",
              "function": {"name": "search", "description": "d" * 9000,
                           "parameters": {"type": "object", "properties": {}}}}]
    body = agent_body(system=100, tools=tools)

    assert estimate_tokens(prompt_size_text(body)) > 2000
    assert prompt_text(body) == USER


def test_prompt_size_handles_the_completion_style_body() -> None:
    assert "hello" in prompt_size_text({"prompt": "hello"})
    assert prompt_size_text({}) == ""


def test_a_list_content_is_flattened_like_the_prefix_text_does() -> None:
    body = {"model": "m1", "messages": [
        {"role": "system", "content": [{"type": "text", "text": "sys" * 100}]},
        {"role": "user", "content": [{"type": "text", "text": "user part"}]},
    ]}

    assert "sys" * 100 in prompt_size_text(body)
    assert "user part" in prompt_size_text(body)


def test_the_thread_identity_still_comes_from_the_user_text() -> None:
    """Two turns with different system prompts are still one conversation."""
    a = agent_body(system=1000)
    b = agent_body(system=2000)

    assert fingerprint("m1", a) == fingerprint("m1", b)
    assert prompt_text(a) == prompt_text(b)


# ---------------------------------------------------------------------------
# What the size decides: "ephemeral" (FA-7)
# ---------------------------------------------------------------------------
async def test_a_large_system_prompt_is_not_ephemeral(stub_factory, ripple_factory):
    """Acceptance (issue #14): a 300 kB system prompt holds a slot."""
    harness = await ripple_factory(make_config(_two_hosts(stub_factory)))

    resp = await harness.ok(agent_body())

    assert resp.headers["x-ripple-reason"] == "new_thread"
    assert resp.headers["x-ripple-thread"]
    assert (await harness.healthz())["threads"]["active"] == 1
    assert sum((await harness.slots()).values()) == 1


async def test_a_short_single_message_request_is_still_ephemeral(
        stub_factory, ripple_factory):
    """FA-7 is untouched: a genuinely short request reserves nothing."""
    harness = await ripple_factory(make_config(_two_hosts(stub_factory)))

    resp = await harness.ok(chat_body("m1", prompt_for(0), turns=1))

    assert resp.headers["x-ripple-reason"] == "ephemeral"
    assert "x-ripple-thread" not in resp.headers
    assert sum((await harness.slots()).values()) == 0


async def test_a_tool_schema_alone_can_keep_a_request_out_of_ephemeral(
        stub_factory, ripple_factory):
    """The schema is prompt: 40 kB of it is 10 k tokens, above min_prompt_tokens."""
    harness = await ripple_factory(make_config(_two_hosts(stub_factory)))
    tools = [{"type": "function",
              "function": {"name": "search", "description": "d" * 40_000}}]

    resp = await harness.ok(chat_body("m1", prompt_for(0), turns=1, tools=tools))

    assert resp.headers["x-ripple-reason"] == "new_thread"
    assert sum((await harness.slots()).values()) == 1


@pytest.mark.parametrize("turns", [1, 2])
async def test_the_size_decides_and_not_the_message_count(turns, stub_factory,
                                                         ripple_factory):
    """A long single-message request is not short, a short thread is not long."""
    harness = await ripple_factory(make_config(_two_hosts(stub_factory)))

    big = await harness.ok(agent_body())
    small = await harness.ok(chat_body("m1", prompt_for(3), turns=turns))

    assert big.headers["x-ripple-reason"] != "ephemeral"
    assert small.headers["x-ripple-reason"] == (
        "ephemeral" if turns == 1 else "new_thread")
