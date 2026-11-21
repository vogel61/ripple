"""Is a newer RIPPLE release published? (issue #123)

One module owns the question, so the answer has exactly one shape and the rest
of the program never touches HTTP for it. `check()` returns an `UpdateState`
whose `state` is one of four words:

``newer``
    A release with a higher version than the one running exists.
``current``
    The running version is the newest release — **or newer than it**. That
    second half is the important one: a checkout of the internal forge
    repository is often ahead of the last public release, and a dashboard that
    then offered an "update" would point backwards. Equal and older both read
    `current`, and the header draws nothing for either.
``unknown``
    No usable answer: no network, a timeout, a 404 (the repository has no
    release yet), a rate limit, a payload that is not JSON. This is a silent
    state — no warning in the log, no element in the header. A dashboard that
    complains about a failed *hint* is worse than one that says nothing.
``disabled``
    `update.check: false` in the config. The fetch is not attempted at all, so
    a machine without internet does not spend a DNS lookup and a timeout on
    every start — and does not log a failure every start either.

The comparison is semantic and deliberately forgiving (`parse_version`): the
numbers are read component by component, so ``0.10.0`` is newer than ``0.9.0``
where a string compare would say the opposite. A leading ``v`` is dropped, build
metadata (``+abc123``) and a pre-release suffix (``-rc1``) are ignored for the
order — which means a pre-release of the same number (``1.2.0-rc1`` against
``1.2.0``) is *not* newer, the honest answer for an offer that would install the
release. A component that does not start with a digit reads as 0, so a tag like
``nightly`` can never look newer than a real version.

The source is the public GitHub releases API, unauthenticated (handover §5): a
private source would need a token on every installation just to show a hint.
Nothing here downloads or installs anything — `ripple update` (issue #121) is
the command the header names, and it is the operator's call.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from . import __version__

#: The public releases endpoint. One release, not the list: the newest published
#: release is the only thing the question needs.
DEFAULT_URL = "https://api.github.com/repos/vogel61/ripple/releases/latest"

#: Short on purpose: the check runs in the background, so a slow answer must not
#: hold a task open for long. Three seconds is enough for a healthy connection
#: and short enough that a blackholed one is over before the operator looks.
DEFAULT_TIMEOUT = 3.0

#: `fetch(url, timeout) -> payload` — the one injection point. The tests pass a
#: stub; production passes `fetch_latest`.
Fetcher = Callable[[str, float], Awaitable[Any]]

_LEADING_V = re.compile(r"^[vV]\s*")
_LEADING_DIGITS = re.compile(r"^\d+")


def parse_version(text: Any) -> tuple[int, ...]:
    """A version string as comparable numbers: ``0.10.0`` → ``(0, 10, 0)``.

    Tolerant by design — this parses a tag somebody else wrote, and a tag that
    does not fit the pattern must not crash a start-up. See the module docstring
    for what is dropped and why.
    """
    raw = str(text or "").strip()
    raw = _LEADING_V.sub("", raw)
    # Build metadata (`+sha`) and the pre-release suffix (`-rc1`) do not order
    # releases for our purpose: the offer names a release, not a candidate.
    core = raw.split("+", 1)[0].split("-", 1)[0]
    parts: list[int] = []
    for piece in core.split("."):
        found = _LEADING_DIGITS.match(piece.strip())
        parts.append(int(found.group(0)) if found else 0)
    # Trailing zeros carry no information, and dropping them makes the tuple
    # compare work for different lengths: `1.2` == `1.2.0`, `1.0.1` > `1`.
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(latest: Any, current: Any) -> bool:
    """True only when `latest` is strictly newer than `current`."""
    return parse_version(latest) > parse_version(current)


@dataclass(frozen=True)
class UpdateState:
    """What the header knows about a newer release.

    `latest` is the version *without* the tag's leading `v` (`v0.2.0` → `0.2.0`),
    because the header puts its own `v` in front of it. `url` is the release page
    on GitHub — the one thing a click may open; nothing here installs anything.
    `checked_at` is the epoch second the answer arrived (None while `disabled`),
    so the page can say how old the hint is without a second endpoint.
    """

    state: str = "unknown"
    latest: str | None = None
    url: str | None = None
    checked_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "latest": self.latest,
                "url": self.url, "checked_at": self.checked_at}


def _display_version(tag: str) -> str:
    return _LEADING_V.sub("", str(tag).strip())


async def fetch_latest(url: str, timeout: float) -> Any:
    """GET the release JSON. Any HTTP error is an exception, by design.

    `raise_for_status` is what turns a 404 — GitHub's answer for "this
    repository has no release yet" — into the `unknown` state instead of a
    crash or a bogus version. A User-Agent is not optional there: the API
    refuses a request without one.
    """
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(
            url, headers={"Accept": "application/vnd.github+json",
                          "User-Agent": f"ripple/{__version__}"})
        response.raise_for_status()
        return response.json()


async def check(url: str = DEFAULT_URL, current: str = __version__,
                timeout: float = DEFAULT_TIMEOUT, *, enabled: bool = True,
                fetch: Fetcher | None = None) -> UpdateState:
    """Ask once, answer in one of the four states. Never raises.

    `enabled=False` is the config switch: nothing is fetched, and the state is
    `disabled`. Every failure path returns `unknown` — the caller has no error
    branch to write, and no start-up ever fails because of this.
    """
    if not enabled:
        return UpdateState(state="disabled")
    call = fetch or fetch_latest
    try:
        payload = await call(url, timeout)
    except Exception:                     # noqa: BLE001 — a hint must not raise
        return UpdateState(state="unknown", checked_at=time.time())

    checked = time.time()
    tag = payload.get("tag_name") if isinstance(payload, dict) else None
    # A draft is not published; a payload without a tag is not an answer.
    if not tag or payload.get("draft"):
        return UpdateState(state="unknown", checked_at=checked)
    page = payload.get("html_url")
    latest = _display_version(tag)
    state = "newer" if is_newer(tag, current) else "current"
    return UpdateState(state=state, latest=latest,
                       url=str(page) if page else None, checked_at=checked)
