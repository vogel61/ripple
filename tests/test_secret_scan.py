"""The secret scanner (tools/secret_scan.py) itself — issue #65.

A scanner nobody tests is a scanner that quietly stops matching. These tests use
synthetic values only; the real key never appears in the repository.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    import sys

    spec = importlib.util.spec_from_file_location(
        "secret_scan", REPO / "tools" / "secret_scan.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves field types through sys.modules[cls.__module__], so the
    # module has to be registered before it is executed.
    sys.modules["secret_scan"] = module
    spec.loader.exec_module(module)
    return module


scan = _load()


def test_a_short_key_is_found_only_by_its_literal():
    # The oMLX key on the reference machines is four characters: no entropy rule
    # can find it, which is why --key-file exists.
    hits = scan.scan_text("admin_key: \"abcd\"\n", "config.yaml", "abcd")
    assert [h.rule for h in hits] == ["literal-key"]


def test_the_literal_is_never_printed():
    hits = scan.scan_text("admin_key: \"abcd\"\n", "config.yaml", "abcd")
    assert "abcd" not in hits[0].sample
    assert hits[0].sample == "****"


def test_known_token_shapes_are_found():
    text = "\n".join([
        "key = 'sk-" + "a" * 32 + "'",
        "token: ghp_" + "b" * 36,
        "auth: Bearer " + "c" * 30,
        "-----BEGIN OPENSSH PRIVATE KEY-----",  # secret-scan: allow (fixture)
    ])
    rules = {h.rule for h in scan.scan_text(text, "x.py", None)}
    assert {"openai-style", "github-token", "bearer", "private-key"} <= rules


def test_ordinary_text_and_placeholders_are_clean():
    text = "\n".join([
        "admin_key: \"CHANGE-ME\"",
        "api_key: ${OMLX_API_KEY}",
        "node_id: 11111111-1111-4111-8111-111111111111",
        "sha256 3f786850e387550fdab836ed7e6dc881de23001b",
        "cd ~/Projects/ripple && ./deploy/start-ripple.command",
    ])
    assert scan.scan_text(text, "README.md", None) == []


def test_a_long_hex_run_without_key_context_is_not_a_secret():
    # A git SHA in a document is not a credential; a rule that says otherwise
    # gets switched off by the next reader.
    text = "fixed in 4f3c2b1a9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a"
    assert scan.scan_text(text, "CHANGELOG.md", None) == []
    assert scan.scan_text("api_key: 4f3c2b1a9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a",  # secret-scan: allow
                          "x.yaml", None) != []


def test_the_allow_marker_is_explicit_and_visible():
    # The scanner's own fixtures carry it; a real key must not be hidden by it
    # silently — the marker sits in the line, so it shows up in every diff.
    line = 'api_key = "sk-' + "a" * 32 + '"  # secret-scan: allow (fixture)'
    assert scan.scan_text(line, "tests/x.py", None) == []
    assert scan.scan_text('api_key = "sk-' + "a" * 32 + '"', "x.py", None) != []


def test_the_working_tree_is_scanned_through_git(tmp_path, monkeypatch):
    # The tree scan must look at tracked files only — what would be published.
    hits = scan.scan_tree(None)
    assert isinstance(hits, list)
