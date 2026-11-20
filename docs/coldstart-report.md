# The first cold start on a second machine

RIPPLE's claim is not "it runs on the machine it was built on". It is that
someone who has never seen the project gets from nothing to a proxy that answers
a real completion.

So the claim was tested where it is uncomfortable: on a second machine, with an
empty home directory, the repository arriving as a `git bundle` (no network
between the two machines), and nothing else on disk — no clone, no virtualenv, no
`config.yaml`. macOS 27.2.1, the system Python 3.9.6 that ships with it, no
Homebrew, no `uv`, no `pipx`. The model node the proxy talks to runs on that same
machine and is reached over loopback.

What follows is what broke, in the order it broke, and what each break became.
Every line quoted here comes from the transcript of a real run; the code
references are to the state after the fixes.

---

## The completion came back empty

**What happened.** The first attempt stopped at the seventh step, the one that
sends a request through the new proxy and expects a word back. Everything before
it was green: clone, install, `init`, `doctor`, start. The answer was a `200` with
`finish_reason: "length"`, an empty `content`, and
`completion_tokens_details: {reasoning_tokens: 16}`.

**Why.** The harness asked for `max_tokens: 16`. The model is a thinking model:
it spent all sixteen tokens inside `reasoning_content` and never got to the
answer. Nothing was broken on the machine — on the machine this was developed
on, the same model answered inside the same budget because it happened to think
shorter. A too-small budget in the script, and a script that reads only `content`
cannot tell "the model thought too long" from "the model is broken".

**What changed.** Two things, both in `tools/coldstart.sh`: the budget is 128
instead of 16, and the extraction accepts `content` *or* `reasoning_content` and
says in the success line which of the two it was. A thinking model that answers
only in `reasoning_content` gave an answer; calling that a failure was wrong.

```
7. one real completion through the proxy
   ok    model offered through the proxy: <model>
   ok    completion in 1 s: "coldstart"
```

## A run without a key failed on a node that was working

**What happened.** The same protocol was then run the way a stranger without an
oMLX key would run it — a documented path, where `doctor` warns instead of
failing. It went red at the same step:

```
FAIL  the proxy offers no model after 15 s — the node answered nothing (key missing?)
```

**Why.** Measured, not guessed: the node answers `401` on `/v1/models` without a
key. Without one, a node is not only unwritable, it is unreadable — the proxy
offers no model, and there is nothing to complete against. The script already had
a branch for this case; it was simply unreachable, because an empty model
catalogue aborted the step before the branch that knew what an empty catalogue
means.

**What changed.** The order of the branches decides the question now: first
whether the run had a key, then whether a model appeared. Without a key, an empty
catalogue is a note, not a red step:

```
7. one real completion through the proxy
   ok    client key read from the generated keys.yaml (not printed)
   note  no oMLX key: /v1/models answers 401, no model offered, completion skipped (read-only path)
```

With a key, an empty catalogue stays an error, because there it is one.

What the run without a key then proved is the part that matters for a stranger:
`doctor` ends with two warnings and exit code zero, the proxy starts, the
dashboard answers, `/healthz`, `/stats` and `/config` return 200 — and the
eviction guard, which cannot work without residency signals, switches itself off
*visibly* instead of pretending:

```
eviction_guard: false — … is switched off because at least one host delivers no residency signals
```

## The diagnosis printed before the heading it belonged to

**What happened.** In the run without a key, `models_unreadable …` and
`eviction_guard: false …` appeared *above* the `ripple init` header. They go to
`stderr`, the header goes to `stdout`; in a transcript with both channels merged
into one file, the diagnosis lands in front of the title. The content is right,
the picture is restless.

**Not fixed, deliberately.** Reordering would mean reordering the output of a
command that `--yes` and the interactive dialog share. In a terminal the two
streams interleave readably; it only looks wrong when both are redirected into
the same file, which is what this protocol does.

## The location was written, but it could never be checked

**What happened.** `ripple init` asked for a site name and the networks it
contains, took the defaults, and wrote a `sites:` block. `ripple doctor` then
reported, twice, `SKIP site — no site declared`, and `init`'s own configuration
check printed `site=-` for the node. The configuration `init` had just written
carried a location that no check could see.

**Why.** `_step_site` attaches `site:` to a node only when the node's address
lies inside one of the site's networks, and `_interface_networks` drops loopback
on purpose. The default node of a fresh local install is on loopback, so it lies
in none of the derived networks. The case the site check exists for — "the node is
not where the configuration says" — could never come up on the reference path.

**What changed.** The finding was decided rather than patched: a node reached
over loopback *is* this machine, and loopback is in no site's networks — putting
it there would make every site contain every machine, and an `OK` that compared
nothing is worse than a `SKIP`. So the node keeps no `site:`, `ripple init` says
so in one line instead of leaving a silent `site=-`, and `doctor` reports `SKIP`
with the loopback as the reason. A hand-written `site:` on a loopback node is
reported as *unconfirmed*, not as contradicted: the claim is not wrong, there is
just nothing to check it against.

## The protocol asked for whichever model came first

**What happened.** Step seven took `ids[0]` from `/v1/models`, and the order
depends on the node. On this node the first entry was a 453 GB model. It happened
to be loaded, so nothing happened. Had it been cold, a cold-start protocol would
have triggered a 453 GB load on a node that serves real traffic.

**Why.** A protocol that borrows the production node's state is not a protocol.
The bug was the assumption that the first model in the list is a cheap one.

**What changed.** `tools/coldstart.sh` takes `--model NAME`. With it, step seven
completes against exactly that model and turns red if the proxy does not offer it.
Without it, the old behaviour remains. The recorded run named the smallest loaded
model on the node (6.5 GB), so the completion caused no load at all — and the
node's model status was identical before and after the run, down to the process.

## `doctor` had never run against a running proxy

**What happened.** The protocol ran `ripple doctor` before starting the proxy,
which is where `dashboard` and `completion` are `SKIP` by construction. Nobody had
ever seen `doctor` finish against a proxy that was actually serving. The promise
"doctor ends green, including a real completion" was asserted, not shown.

**Why.** The step was missing from the protocol, so the promise had no evidence
behind it. A check that is always skipped is not a check.

**What changed.** A new step runs `doctor` against the running proxy and ends the
run if `doctor` goes red:

```
OK   completion <model> answered in 1057 ms via local
```

That is the step that makes the last line of the protocol mean something.

---

## What this says about the installation

The install path itself never broke. Clone from the bundle, `python3 -m venv` on
the system Python 3.9.6, `pip install .` with build isolation (which pulls
`setuptools` down on its own), sixteen i18n catalogues in `site-packages`, a
`ripple` console script, `init --yes`, both written files at mode 0600, proxy up,
dashboard answering on `/healthz`, `/stats` and `/config`, port free again after
the stop, no listener, no key file left on disk, the real home directory
untouched. A stranger needs `git`, `python3` and `curl` — nothing else, and no
package manager.

Every defect was in the instrument, or in what a check was allowed to claim. That
is the pattern worth keeping: a cold start fails first in its own protocol. The
first red step was a token budget in the harness, the second was the harness
treating "no key" as a failure, the third was the harness asking for a 453 GB
model because that one happened to be first, and the fourth was the harness never
having run `doctor` against a running proxy. Four of the six findings were about
the script, not the product.

The two that were about the product are the ones a stranger feels. Missing rights
must be a warning, not a failure — a read-only node is a documented way to run,
and the proxy has to start, serve the dashboard and route what it can while
saying plainly that it cannot see the model list. And a check that cannot compare
anything has to say so rather than report a comparison it did not make. Both were
decided, not worked around.

What a cold start cannot tell you: that the installation is easy. It can tell you
that it worked once, on one machine, with one node on loopback.

## What catches these today

| Finding | Caught today by |
|---|---|
| empty completion, `content` vs `reasoning_content` | `doctor`'s completion check (repaired in #82: it asks for a word, reads the text, and treats `finish_reason: length` with empty `content` as a finding — `ripple/doctor.py:703`) and the script's own extraction, `tools/coldstart.sh:308`. The probe's `max_tokens` is measured: five tokens went entirely into the reasoning channel, 64 never failed in 43 runs |
| no key is not a failure | `tests/test_coldstart.py:107` (read-limited node, `doctor` exits 0), `tests/test_doctor.py:175` (missing admin rights is a `WARN`), `tests/test_eviction.py:127` (guard off, node still serves). The script's own branch order, `tools/coldstart.sh:283`, has no test |
| diagnosis on `stderr` before the header | nothing, by decision |
| site written but not checkable | `tests/test_doctor.py:422` (real `init`, real `doctor`, `SKIP` with the loopback reason) and `tests/test_doctor.py:467` (a hand-written `site:` is unconfirmed, not contradicted) |
| first model in the list | nothing but the flag: `tools/coldstart.sh:288`. No test |
| `doctor` against a running proxy | `tests/test_doctor.py:108` (`dashboard` and `completion` green against a live proxy) and `tests/test_coldstart.py:61` (fresh home, `init` against a stub node, the real proxy, the real `doctor`, one completion with the issued key) |

`tests/test_coldstart.py` is the suite's version of the same walk — deliberately
the same code path (`init.main`, the real config loader, the real proxy, the real
`doctor`) and not a second implementation that could agree with itself while the
script fails. What it does not do is run the script. So two of the six findings
rest on `tools/coldstart.sh` alone, two are covered on the product side but not
in the form the script exercises, one is covered by tests, and one has no check at
all. That gap is named here rather than papered over: a regression in the
protocol would not turn the suite red.
