"""Logging setup: one record per line, even under a launchd redirect.

`deploy/ai.ripple.plist` used to send StandardOutPath and StandardErrorPath to
`~/Library/Logs/ripple.log`, which is also `log_file`. A StreamHandler on
sys.stderr then wrote every record a second time into the same file: 1476
`ripple.request:` lines for 740 requests. The plist no longer carries either
key — the file descriptor launchd owns cannot rotate, so after the first
rotation it kept appending to the renamed file and grew past the 10 MB cap.
The rotating handler is the only sink now. `setup_logging()` still recognises
the case for anyone who puts the redirect back: same device and inode, then
only the rotating handler is registered.
"""

from __future__ import annotations

import io
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Iterator

import pytest

from ripple.__main__ import setup_logging
from ripple.config import Config

MESSAGE = "one record per line please"


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    """setup_logging() rewires the root logger — put it back afterwards."""
    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    yield
    for h in list(root.handlers):
        if h not in saved:
            h.close()
        root.removeHandler(h)
    for h in saved:
        root.addHandler(h)
    root.setLevel(saved_level)


def _write(cfg: Config, stream: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stderr", stream)
    monkeypatch.setattr(sys, "stdout", stream)
    setup_logging(cfg)
    logging.getLogger("ripple.test").info(MESSAGE)
    for h in logging.getLogger().handlers:
        h.flush()


def test_single_handler_when_stderr_is_the_log_file(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    log_file = tmp_path / "ripple.log"
    log_file.touch()
    with open(log_file, "a", encoding="utf-8") as fake_stderr:
        _write(Config(log_file=str(log_file)), fake_stderr, monkeypatch)

    text = log_file.read_text(encoding="utf-8")
    assert text.count(MESSAGE) == 1, "the record was written twice"
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.handlers.RotatingFileHandler)


def test_redirect_notice_is_logged_once(tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    log_file = tmp_path / "ripple.log"
    log_file.touch()
    with open(log_file, "a", encoding="utf-8") as fake_stderr:
        _write(Config(log_file=str(log_file)), fake_stderr, monkeypatch)

    text = log_file.read_text(encoding="utf-8")
    assert text.count("rotating file handler only") == 1


def test_stream_handler_kept_when_the_paths_differ(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """Interactive run: stderr is the terminal, the file is a different file."""
    log_file = tmp_path / "ripple.log"
    terminal = tmp_path / "terminal.txt"
    with open(terminal, "w", encoding="utf-8") as fake_stderr:
        _write(Config(log_file=str(log_file)), fake_stderr, monkeypatch)

    handlers = logging.getLogger().handlers
    assert len(handlers) == 2
    assert any(type(h) is logging.StreamHandler for h in handlers)
    assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in handlers)
    assert log_file.read_text(encoding="utf-8").count(MESSAGE) == 1
    assert terminal.read_text(encoding="utf-8").count(MESSAGE) == 1


def test_no_log_file_keeps_the_stream_handler(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    terminal = tmp_path / "terminal.txt"
    with open(terminal, "w", encoding="utf-8") as fake_stderr:
        _write(Config(log_file=None), fake_stderr, monkeypatch)

    assert len(logging.getLogger().handlers) == 1
    assert terminal.read_text(encoding="utf-8").count(MESSAGE) == 1


def test_stream_without_fileno_is_not_a_file(monkeypatch: pytest.MonkeyPatch,
                                             tmp_path: Path) -> None:
    """A captured stream (pytest, a pipe wrapper) has no inode — never equal."""
    log_file = tmp_path / "ripple.log"
    log_file.touch()
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    setup_logging(Config(log_file=str(log_file)))

    handlers = logging.getLogger().handlers
    assert len(handlers) == 2, "a fileno-less stream must keep both handlers"
    assert any(type(h) is logging.StreamHandler for h in handlers)
