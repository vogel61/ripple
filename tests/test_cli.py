"""The CLI surface: one program, three first-run words, the old flags unchanged.

`ripple init`, `ripple discover`, `ripple doctor` are dispatched by hand in
`__main__.main()` so that `ripple --config x`, `ripple --check` and
`ripple --version` keep behaving exactly as they did. These tests pin both
halves of that: the dispatch, and the fact that a flag is never mistaken for a
subcommand.
"""

from __future__ import annotations

import pytest

from ripple import __main__ as cli


def test_the_three_first_run_words_are_wired():
    assert cli.SUBCOMMANDS == ("init", "discover", "doctor")


@pytest.mark.parametrize("word", ["init", "discover", "doctor"])
def test_each_word_reaches_its_module_with_the_rest_of_argv(monkeypatch, word):
    module = __import__(f"ripple.{word}", fromlist=["main"])
    seen: dict = {}

    def fake_main(argv=None):
        seen["argv"] = argv
        return 7

    monkeypatch.setattr(module, "main", fake_main)
    assert cli.main([word, "--flag", "value"]) == 7
    assert seen["argv"] == ["--flag", "value"]


def test_a_flag_is_not_a_subcommand():
    # `--check` on a missing config must take the ordinary path: ConfigError →
    # exit 2. If a flag were ever routed to a subcommand this would not happen.
    assert cli.main(["--config", "/nonexistent/ripple.yaml", "--check"]) == 2


def test_the_module_entry_points_exist():
    for word in cli.SUBCOMMANDS:
        module = __import__(f"ripple.{word}", fromlist=["main"])
        assert callable(module.main)


def test_help_of_a_subcommand_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["discover", "--help"])
    assert exc.value.code == 0
    assert "--scan" in capsys.readouterr().out
