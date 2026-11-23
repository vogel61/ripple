# Changelog

All notable changes to this project. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.3] - 2026-11-23

The install path, repaired. Sixteen fixes in one release, because 0.2.2 was cut
internally and never published: 0.2.3 is its first public appearance. Patch, not
minor: the configuration surface is unchanged, except that an empty model
allow-list now means the chat-capable models.

Why a patch despite the behaviour change in #157: the previous behaviour was a
defect, not a contract — an empty answer routed oMLX's utility engines as if they
were chat models.

The first ten fixes were found by reading `init` against a stranger's machine;
the last six by running it on one that already had a fleet — a stale node in the
overlay that answered 401, a configuration written into the checkout instead of
the home directory, a plist that carried the oMLX key, and the site networks
that were never searched although typing `192.0.2.0/24` reads as "look there".

### Fixed

- **A fresh install ended without an admin key** (issue #158). `init` generated
  one, wrote it to `auth.admin_key` and said "not shown here", so the first thing
  an operator did was copy a secret out of a mode-0600 file — or conclude there
  was none. The question is `RIPPLE admin key (empty = generate a 32-hex one):`
  now; an empty answer prints the key once, in the same shape as the client key,
  and `--yes` does the same so a scripted run does not end without one.
- **The oMLX key went to `defaults.api_key` only** (issue #159), so a fresh start
  logged `host_stats: no oMLX admin key (replication.admin_key)` while `ripple
  doctor` reported the admin rights as OK — two statements that contradict each
  other on the same file, and the per-host curves never sampled. `init` writes
  both names now; `replication.enabled` keeps its `false`.
- **The add-node flow never asked for the node's own key** (issue #161). A node
  inherits nothing from `defaults`, so it arrived with `models=0`, no residency
  and FA-18 off fleet-wide. 7.1 has a field for it, the dry run and the add both
  send it, and an empty answer is a valid read-only node that says so instead of
  reporting "0 models".
- **An empty allow-list made oMLX's utility engines routable** (issue #157):
  `MarkItDown` (`model_type: markitdown`, loaded) appeared as its own block in
  the distribution and was offered over `/v1/models`. The empty answer now means
  the chat-capable models, `init` names what it left out, and `all` keeps the
  documented way to route everything.
- **A port a closed listener left in `TIME_WAIT` was called "in use by an
  unidentified process"** (issue #151) — and under `--yes` `init` moved to 8011
  while launchd and every client still pointed at 8010. The probe sets
  `SO_REUSEADDR` like uvicorn does and checks both the wildcard and the loopback
  address; a moved port is announced in one line at the end of the run.
- **The comma-separated network line `init` proposed was refused** (issue #152):
  the whole line was handed to `ipaddress`, so the site was written with
  `networks: []` — a block `ripple doctor` can only report, never check. A string
  is split on commas and whitespace now, every token must parse as CIDR and must
  carry a prefix (`192.0.2.0` quietly became a `/32` before).
- **A `${VAR}` that expanded to empty was silent** (issue #153). The message
  names the field and the variable.
- **`init --force` replaced the config and the key store without a backup**
  (issue #154) — the destructive path ran at the worst moment. Both files are
  copied aside as `<file>.bak-<YYYYmmdd-HHMMSS>`, mode 0600, before either is
  written, and both paths are printed.
- **The node sweep probed every candidate without the key** (issue #155), so
  every candidate came back with `model_count: 0` and `admin: false` — the one
  an operator will not add. `POST /admin/discover` sends the key the fleet
  itself probes with, and a swept node that has no key keeps its reason.
- **A rotated admin key looked like a broken sweep** (issue #156): the refusal
  was a bare `admin key required`, which the page could not map to a step. It
  carries `step: "admin"` and `code: "admin_key_refused"` now, with its own
  sentence in all sixteen catalogs that says where the current key is.
- **`ripple doctor` checked the wrong port** (issue #162): `--url` had a fixed
  default of 8010, so an installation on any other port was told its port was
  free and had its dashboard and completion checks skipped. Without `--url` the
  URL now comes from `listen_port` in the configuration that was just read.
- **The port question accepted `80`** (issue #166). Everything from 1 to 65535
  passed, and a port below 1024 then failed at start-up with `EACCES`, minutes
  after the question. Now 1024–65535 is accepted, the documented range
  `8010–8099` is named in the message, and the four ways to answer wrong (not a
  number, `0`, needs root, too high) each get their own sentence. A typo asks
  again instead of ending the install, and `--port 80` is refused the same way
  instead of writing a configuration that cannot bind.
- **An address typed at the question was never probed** (issue #167), so a typo
  or a machine without oMLX became a node that never answers, silently. Typed
  addresses go through the probe the automatic path already used, and the run
  says what it found: oMLX answered with its `node_id`, something answered that
  reports no `node_id` but a key-protected model list (which every
  OpenAI-compatible server with auth produces — measured against this machine's
  own gateway), something answered that is not oMLX at all, or nothing answered
  at all. An address that answers nothing stays possible — a node that is
  switched off today is the one you configure today — but it is confirmed once
  instead of assumed.
- **The launchd plist carried the oMLX key** (issue #185). `install-service`
  copied `~/.omlx/api_key.txt` into `EnvironmentVariables.OMLX_API_KEY`, so the
  key sat in a second file — one that every backup of `~/Library` picks up —
  although a configuration written by `init` names it as `defaults.api_key_file`
  and needs nothing there. The plist now carries the key **only** when the
  configuration asks for the variable (`api_key: ${OMLX_API_KEY}`); the template
  marks the block with `__KEY_BEGIN__`/`__KEY_END__` and `render_plist` drops it.
  `ripple doctor` gained a `plist` line: it warns in both directions — a key
  nobody needs, or a missing one the configuration asks for.
- **The site networks were never searched** (issue #187). Entering
  `192.0.2.0/24` at the networks question reads as "look there", but `init`
  probes only the addresses it was given — so a second machine was never asked
  for its name, weight or key. After the networks, `init` now offers the sweep
  (default **no**: a sweep is traffic on someone else's network), and every node
  it finds is asked the same three questions as one on loopback. A node already
  in the fleet is skipped by `node_id`.
- **`ripple -perm` read the checkout's config, which the install no longer
  writes** (issue #186). `service.config_path()` defaulted to the working
  directory — and the installed wrapper `cd`s into the checkout — so after #183
  every service command died on `<repo>/config.yaml` with advice about copying
  `config.example.yaml`. Both the command path and the plist path now use
  `default_config_path()` (`~/.ripple/config.yaml`, else `./config.yaml`), the
  job path always absolute, and the missing-file hint names `ripple init`.
- **A node added at the page answered 401** (issue #182). The runtime overlay
  has no field for a key — and must not have one, it is written by the API — so
  the node had none, `/v1/models` answered 401 and the eviction guard switched
  off fleet-wide. An overlay node now uses the fleet key (`defaults.api_key` or
  its `api_key_file` reference); a key set on the block itself still wins, and a
  fleet without any key says so once.
- **`init` wrote the config into the directory it ran in** (issue #183).
  `./install.sh` runs inside the checkout, so a stranger's `config.yaml` landed
  in the clone — invisible to `ripple` started elsewhere, and gone after the
  next re-clone — while the key store went to `~/.ripple/`. `init` now writes
  `~/.ripple/config.yaml`, and `ripple`/`ripple doctor` read it without a flag
  (`./config.yaml` stays the fallback).
- **The last line of `init` named `auth.admin_key` in `config.yaml`** (issue
  #184) — the file that stopped carrying it in #179. It names
  `~/.ripple/secrets.yaml` now.
- **The legend explained the fleet before the fleet was visible** (issue #181).
  On the fleet tab the seven states and the shape note stood above the node
  cards, so the first screen at 1440×900 was the explanation of a state nobody
  had seen yet. The legend keeps its place and collapses: `▶ Legende anzeigen`
  by default, `Legende ausblenden` when open, `aria-expanded`/`hidden` as a real
  button. The choice is remembered in `localStorage`, and the shape note travels
  inside the collapsible part.
- **The node list said `model list not read (the key comes later)` and was read
  a step later** (issue #180). The key question moved in front of the discovery,
  so the line carries the count it measured — `local  http://127.0.0.1:8000  23
  model(s)` — and a key that does not fit is visible there instead of one step
  on. The sentence stays for the install without any key.
- **The configuration carried the keys, so every shared file was a leak**
  (issue #179). `init` wrote the oMLX key and the admin key into `config.yaml`
  in plain text — the file an operator pastes into a forum. A credential may now
  be a reference instead: `api_key_file: ~/.ripple/secrets.yaml#omlx.fleet`, with
  the values in a 0600 file that `init` writes. A literal still works and wins
  over the reference, so older files load unchanged. `ripple doctor` gained a
  `secrets` check — `OK` when the file as written carries no credential, `WARN`
  naming the fields when it does.
- **The key question sent a fresh install to a file oMLX never writes** (issue
  #177). `oMLX API key (usually in ~/.omlx/api_key.txt …)` and the docs said the
  key "usually sits" there — but oMLX keeps it in `settings.json` under
  `auth.api_key` (admin panel → Settings → API key) and writes no text copy; the
  string appears in none of its binaries. A stranger with two fresh Mac Studios
  was sent to a path that does not exist, twice. The question now names the
  panel, per node; a file that *is* there is offered and called what it is — a
  copy — and the same holds in `docs/install*.md` and in the launchd error.
- **`install.sh` for the prerequisite nobody installs** (issue #178). macOS has
  shipped no Python since 12.3, so `python3 -m venv` printed `command not found`
  and the documentation listed "Python >= 3.9" without saying how to get it.
  `./install.sh` finds an interpreter (3.9–3.13), refuses anything older, creates
  `.venv`, installs, and offers `ripple init`; without Python it prints the two
  lines that fix it (`xcode-select --install`, `brew install python@3.13`).
  `Install.command` runs it by double-click. `--check` reports without writing.
- **A fresh install opened with two warnings it could not act on** (issues #175,
  #176). A missing `host_stats.json` on the first start is normal and is a note
  now, not a warning; and `rebind` no longer warns that it "cannot fire" when
  there is only one node — a move needs a destination, so with one node there is
  nothing to price and `ripple doctor` says `SKIP` instead. A two-node fleet
  behaves exactly as before.
- **The German dashboard said `Calibrate`** (issue #174) in the node card and in
  two sentences — the only untranslated strings in `de.json`. Now
  `Kalibrieren`.
- **One oMLX key for the whole fleet made a second node read-only** (issue #171).
  `hosts.<name>.api_key` exists for the fleet whose machines each carry their own
  `~/.omlx/api_key.txt`, but `init` asked once and wrote that key everywhere — the
  second node answered 401, delivered no model list and no residency signal, and
  FA-18 went off for the whole fleet without saying which node refused which key.
  The key is asked per node now: the first answer is the fleet default, a node
  that refuses it is asked once more for its own, and a key that differs is
  written as `hosts.<name>.api_key`. `empty = read-only` stays an answer, and a
  scripted run (`--yes`, `--omlx-key`) reports the refused node instead of
  asking.
- **A port outside 8010-8099 said `written as asked`** (issue #172) — a sentence
  that admitted a deviation and then shrugged. It names the consequence now (the
  dashboard, `ripple doctor` and `ripple start` follow that number) and asks
  once, default no, so a typo like `8199` for `8099` no longer passes as a
  decision. `--port` and `--yes` keep writing it: a flag is a decision, and a
  scripted run has nobody to ask.
- **A node typed at the address question got no name** (issue #173), so an empty
  answer wrote `name: ''` and the run ended with `the written configuration does
  not load: hosts[0]: 'name' missing` after the files were written. The typed
  address gets the suggestion the automatic path gets (`local`, or a `node-…`
  stub from its `node_id`), and the name question offers it as the default.
- **An unchanged capability note was logged on every refresh** (issue #170), so
  one node with an unreadable model list printed the same two lines twice inside
  a single `init` check and would have kept printing them on every probe
  interval. A note is news when it appears and when it goes away — the rule
  `version` and `models_unreadable` already followed.
- **The weight question explained nothing and validated nothing** (issue #169).
  `weight` is a word from the configuration file: a stranger cannot tell that the
  number is a *relative* share, and a typo (`abc`, `0`, `21`) was silently
  replaced by the default `1`. The question now says what the number does
  (`relative: higher takes more new threads, 1-20`) and insists: every wrong
  answer gets its own sentence and asks again. The file stays as tolerant as it
  was — `hosts.*.weight` is a positive float, so a hand-edited `1.5` keeps
  working; the range is what the question asks for, not what the file allows.
- **The discovered list said `0 model(s)` before the key was asked** (issue
  #168). oMLX answers `401` on `/v1/models` without a key, so the number named a
  measurement nobody had made. The line says what was measured now — that the
  node answered, and that the model list needs the key — and keeps the real count
  when a key was available.

### Added

- **One smoke test walks the install path** (issue #160): stop, `init --force
  --yes` against a stub node, start, read the log. Six defects reached the
  operator on one morning and every one had green unit tests; each part was
  correct, the path between them was not, and no test walked it.
- **The model dot blinks blue while a node loads** (issue #149): a cold load was
  invisible as a load — the request that triggered it sits in `pp`, so the dot
  blinked orange and read like a prefill on resident weights, for the minutes a
  453 GB load runs. `load` wins over `pp`, and `/healthz` carries the fact as
  `models[].hosts[].loading`.

### Changed

- **The two tok/s cells say "current", not "15 min"** (issue #150). The caption
  named a period the number does not use and read as an average the page never
  computes; the window stays on the chart, where it describes the line.

## [0.2.1] - 2026-11-21

Six changes and three fixes. Patch, not minor: nothing new to configure, only
what the page shows and how it reaches the browser.

### Changed

- **Five equal metric cells, and the rate is split into what a thread gets and
  what the node gives** (issue #145). The card had four cells and one tok/s
  figure — the smoothed decode rate per request — with the cache as a full-width
  row underneath. The row is five cells of equal width now: `inflight`, `thread
  slots`, `RTT`, `tok/s per thread` and `tok/s total`, with `cache` still the
  full-width row. Both rate cells are read from **the same bucket**, the last one
  with a `total` sample: reading each row's last number separately showed `0.0`
  next to `23.9` from a twenty-second-old bucket whenever the node was idle, and
  the card contradicted itself. `per thread` is set in the maintenance tone at
  the operator's request; `total` keeps the foreground, so the two cells do not
  compete for the same attention. Five columns at 1440 px are 117 px wide, and
  the longest subline (`target 60 % · load 0.36`) measured 130 px: it wraps onto
  a second line instead of being shortened — #80 had shortened the same line
  once and paid for it with the unit.

- **The plot is 46 px high, and its zero is the card's separator line** (issue
  #145). 34 px was the height the tab had without growing; at a ratio of 1.5
  between the two rows they sat 8 px apart and the separation could not be read.
  The block is 14 px head plus 46 px area now, 60 px in all. The 6 px padding
  under the area is gone for a measured reason: the eye took the grey rule 9 px
  below the true zero for the zero, so the two lines looked like two scales.
  Now the rule the operator already sees is the zero — a value of 0 merges with
  it, which is the right picture for "no traffic".

- **The sum row is one lightness step brighter, not taller** (issue #142). #138
  had marked the sum by height: 14 → 21 px, 50 % taller than the rows it sums.
  The operator's check on 21.11. took that back, for two measurable reasons: the
  21-px bar is the only tall element in `#load`, so the eye lands on the sum
  instead of the nodes; and the axis sat 8 px above the bar but 12 px below the
  row above, so it stuck to the sum instead of cutting between the rows. The
  axis stands at `top:-10px` now — 9 px above the sum, 11 px below the row
  above — and the five segments are one CIE-Lab step (+9 L\*) brighter at the
  same hue (`--sum-*`). HSL lightness +8 % was the first attempt and is not
  even: measured +4.9 (prefill) to +9.0 (decode, mixed), which looked unevenly
  lit rather than one step brighter. The orange sits at the edge of sRGB and
  lands at +8.4 (`#ffbb55`, ΔE76 2.1). The legend keeps the base colours — it
  describes the parts, not the sum.

- **The thread list goes one column at 1100 px, not at 1200** (issue #143). At
  1100 px the two columns are 518 px each and the key keeps 205 px — measured,
  nothing truncates, no line overflows; at 1060 px three of four keys do. The
  old 1200 was above what an iPad 10.9"/11" reports in landscape (1180–1194 CSS
  px), so those screens got one column although two would have carried.

- **The parameter help is translated, with the schema sentence as the fallback**
  (issue #144). The 29 `params.help.*` sentences live in all sixteen catalogs
  now; the page asks the catalog first and takes `/config → help` only when the
  language does not know the sentence. The fallback is what makes a new setting
  shippable before fifteen translations exist, and `en.json` carries the same 29
  sentences word for word so the two sources cannot drift.

- **The page's own CSS and JS carry their mtime** (no issue). Both links read
  `/static/dashboard.css?v=<mtime>` and `/static/dashboard.js?v=<mtime>`.
  `StaticFiles` sends `ETag` and `Last-Modified` but no `Cache-Control`, and a
  plain reload then does not necessarily refetch: on 21.11. the old sum colour
  stayed on screen although the server had long served the new file (compared by
  md5), and the operator would have had to clear the cache by hand. With the
  mtime in the URL every change is a new URL — the one kind of cache a browser
  cannot ignore.

### Fixed

- **A comment terminator inside a comment swallowed `--sum-idle`** (issue #142).
  The Lab explanation wrote `a*/b*`; the `*/` closed the CSS comment, the rest of
  the line was read as declarations, and `--sum-idle` was never defined — the
  idle segment of the sum row was drawn with no fill at all. It reads `a* and
  b*` now, and a test reads the custom properties out of the stylesheet instead
  of trusting the render.

- **The oMLX version in the foot was as muted as the counters beside it** (issue
  #145). It is the only value in the foot that says something about the node's
  build, so it is set in `--fg`, the foreground tone, while the counters keep the
  dimmed grey.

- **The replication section called itself "the third and last exception"** (issue
  #145). The list had grown with every amendment, and the sentence was true only
  when #13 was written. Manual and module docstring now say what is true:
  replication is the one write path that touches another machine's filesystem;
  calibration, maintenance, model policy, parameters, restart and the node list
  all stay on this host. The sentence lives in all sixteen catalogs — `de` and
  `en` with the code, the other fourteen in a follow-up.

## [0.2.0] - 2026-11-21

Eight new capabilities, four changes and eleven fixes. Minor, not patch: the
capabilities change what RIPPLE does, the changes and the fixes ride along. No
intermediate patch release — that would only produce two exports of the same
tree.

### Added

- **Every tooltip opens on a tap, `not routable` became a symbol, and 6.5 explains
  its own parameters** (issue #139). Three gaps with one cause: the page showed
  values and abbreviations without saying what they mean, and the tooltip layer
  opened on hover and focus only — `pointerover` fires on a touch, and the global
  click handler closed the box in the next instant, so on a phone no tooltip was
  reachable at all. A click now shows the box instead of hiding it, except
  within 400 ms of a pointer-show (that is the end of a tap, not a second wish)
  and except a second tap on the same owner. The `not routable` chip was
  84 × 35 px inside a 100 px model cell and wrapped onto two lines, twenty-one of
  them in the table; it is now the tabler `route-off` at 14 px in the colour of
  the failure, and the tooltip carries the reason the chip never named —
  `dispatch` false (the node does not offer the model) or listed but not loaded
  (the first request pays a cold load) — with `role="img"` and an `aria-label`,
  so a screen reader gets the sentence and not a picture. Section 6.5's forty
  rows of bare field names carry a sentence each: `FIELD_HELP` in the config
  schema, one entry per path, served as `help` in `GET /config` and rendered as
  the parameter's tooltip. It lives in the schema and not in `dashboard.js`,
  where the text would go stale the first time a field is renamed; 29 paths,
  every one of `EDITABLE_PATHS` held by a test, in English like the `Source:`
  lines. Four of the forty rows stay without a tooltip because they are not
  settings: the config file's path, the `loading` aggregate of four settings, and
  the two measured prefill rates.

- **The lock is a property of the model, not of its routability** (issue #118).
  `POST /admin/models/{model}/lock` now succeeds for a cold model, for one the
  allow-list keeps out of routing, and for one no node lists yet — the operator
  locking ahead of a start. The `409 lock_not_dispatchable` refusal from #28 is
  gone; it asked whether the lock could refuse something *right now* about a
  thing that is an attribute. The page carries one padlock per **model** row, in
  the cell with the `rowspan` and never in a node cell, because the lock has no
  host in its key; the open state is the neutral `--fg-2`, not the success
  colour, because an unlocked model is the *unprotected* state and not a good
  one. `MarkItDown` is a virtual oMLX engine — derived from `engine_type` in
  `/v1/models/status`, never from a list of ids in this repository: always
  locked, never releasable (`409 lock_builtin`), resident without weights, and
  its node cells offer no policy control.

- **A locked model is preloaded when its host becomes healthy** (issue #119).
  At startup and at the end of a maintenance window, on every host whose
  allow-list contains it, so the first thread after an oMLX restart no longer
  pays a cold load the operator had explicitly ruled out. The trigger is the
  healthy transition that already sets `relearn_due` (#86) — no second poller —
  and nothing awaits a load inside the health path or the lifespan: startup stays
  under 3 s while a 400 GB load runs. A preload that displaces a model reports
  what went (`hosts[].displaced`, `models[].hosts[].evicted_by`, a `▼` in the
  row); one that does not fit ends in a visible `failed` state with the numbers
  ("400.0 GB needed, 40.8 GB free") instead of a retry loop. Locking a cold model
  does not load it on the spot — that is a separate decision, not this issue's.

- **`ripple start | status | shutdown | restart | update`** (issue #121). The
  five operations in one module, with label, port, plist and config taken from
  `RIPPLE_LABEL`/`RIPPLE_PORT`/`RIPPLE_PLIST`/`RIPPLE_CONFIG` and the production
  values as defaults — which is what makes them testable without touching a
  running service. `restart` returns only once the dashboard answers (0.3 s
  measured on a warm node) and waits for the launchd job to be *gone* as well as
  the port to be free, because `bootout` is asynchronous and assuming otherwise
  left the service down. A process on the port that is not RIPPLE is named and
  left alone. `update` refuses on a dirty tree, runs the suite, and restarts only
  if it is green. `deploy/install-cli.command` installs the wrapper into
  `/usr/local/bin` if writable, else `~/.local/bin`, with no sudo.

- **Autostart on and off from the command line** (issue #122). `ripple -perm`
  renders the plist from the checkout it runs in, writes the oMLX key with
  `plutil`, bootstraps the job and waits for `/healthz`, then prints
  `autostart: on (ai.ripple, /path/to/repo)`. `ripple shutdown -perm` stops
  first, then removes the job and the plist, then verifies both. A running manual
  instance is never killed: the job is installed and launchd takes over at the
  next login, which the command says in one line instead of leaving it to be
  discovered.

- **The dashboard header offers a newer release** (issue #123). Checked once at
  startup against the public GitHub releases API, unauthenticated, in a
  background task; `unknown` (no network, no release yet, a 404) is silent, and
  `update.check` turns the check off for a machine without internet. A local
  build newer than the newest release reads as `current`, never as an offer
  pointing backwards. The comparison is semantic (`0.10.0 > 0.9.0`). Nothing is
  downloaded or installed — the header names `ripple update`, which is #121.

- **A lock that cannot be kept says so at the click** (issue #126). The lock is
  model-wide, so the question is not "does it fit on this host" but "does it fit
  on at least one host that permits it" — and the answer now travels with the
  view: per model `feasible` with `feasible_reason` and `feasible_host`, per host
  `fits` and `fit_reason`. The arithmetic is `HostRuntime.preload_forecast()`
  through one helper, so the padlock and the preload cannot drift apart, and the
  forecast stays read-only: nothing is evicted, unlocked or pinned to make room.
  Measured on the live fleet: `GLM-5.2-oQ4` needs 448.9 GB where 37.3 GB are free
  after evicting MarkItDown, `POST` returns 200, and the model reads back
  `feasible=False` with `feasible_host=alpha` — the lock is stored, and it is
  visible that it can be resident nowhere right now. A warning state, not a
  refusal, because the operator may be mid-reconfiguration (unlock DeepSeek, then
  lock GLM) and the intent is legitimate, only not satisfiable yet; refusing
  would be a dead end. The padlock has a third state for it — amber outline and
  an `!` badge, `aria-label` from the catalog, so it is carried by shape and text
  and not by colour alone — with the forecast's own sentence plus the next step
  in its tooltip. `feasible: null` (no host offers the model) and `locked` but
  not routed here (the drain pattern of #120) stay silent, and a builtin never
  warns.

- **The admin key is entered where the operator is** (issue #130). After a
  restart, a new tab or a browser restart every operator control was inert and
  the only hint was a tooltip pointing at `/keys`, so the operator found out by
  clicking something and reading the refusal. The masthead now carries a chip
  next to the language selector with three states — `read-only` (open shackle,
  muted), `checking`, `admin` (closed shackle, accent) — distinguishable by shape
  rather than tone, in the shell so the 2-s tick cannot eat what is being typed.
  Nothing is written before the key is verified: `GET /admin/models` has to
  answer 200 first, a 401 leaves the field open and `sessionStorage` untouched,
  and there is exactly one `setItem` in the whole script, standing behind that
  check. The verification is the request the padlock warnings already make
  (#126), so it costs one request and not two. Signing out clears the key and
  repaints the keyed blocks inert. No `localStorage` and no "loopback is admin":
  the first would put the secret on disk in plain text, the second would let any
  local script stop the process and write the policy file without a credential.

### Changed

- **The four statements about a model are documented, and separated** (issue
  #120). Capability (the allow-list: this host may serve it at all), rule (the
  policy, per host and model: RIPPLE routes here), lock (per model: it may not be
  unloaded) and residency (`loaded`: the weights are here now) are four
  independent axes, and #116 was one of them being read as another. The documents
  now carry the axis table with a code reference each, the combination matrix —
  including `locked` + not routed here, which is the *drain* pattern and
  legitimate — and the rule that two intents belong in two places: "exists on
  this SSD but must not be routed" is the allow-list, "should not exist on this
  machine" is oMLX. Seven statements in the manual and the design document were
  no longer true and are corrected, among them "the page is read-only" and "a
  locked model refuses the calibration run entirely".

- **The parameters table shows where one node's settings end** (issue #131).
  Section 6.5 was thirty rows with the node name repeated as plain text and
  nothing grouping them, so the eye had to parse the first column to find the
  boundary — while the rest of the page had solved that with the stripe from #89.
  Every node row now carries that stripe in its scope cell, the same `data-slot`
  as the same node in the fleet card, taken from the node object in `/config`
  instead of a name rebuilt in the renderer; `global`, `placement` and `affinity`
  get an empty slot of the same width, so all 40 scope texts start at x = 78.5.
  Six pixels, `aria-hidden`, no new id: the name is in the cell next to it, and
  the pattern still separates two nodes that share a tone. Below 1100 px, 12 px
  move from the parameter column into the scope column (the sum stays 476) —
  without that the stripe would have been paid for by the value column, which was
  already 7 px over its inner width.

- **The model table reads left to right as what runs, how fast, how much, how
  big** (issue #137). `Size` and `Context` are static and sat in the middle of the
  row while `tok/s` — the one number that changes second by second, and the reason
  to open the table at all — was the last column. The order is `Model · Node ·
  Loaded · tok/s · Req · Threads · Size · Context`, and `tok/s` is the only column
  in bold: weight 600 and full contrast, still right-aligned, because bold and
  right-aligned is a column of numbers while bold and left-aligned would be a
  second alignment edge in the same table. The fixed widths follow the content and
  still sum to 740 px, so the phone measurement in #135 is unchanged. `_tps_cell()`
  in the tests found the cell as `cells[-1]`; it now finds it by class — five tests
  would otherwise have stayed green against the Context column.

- **The sum in 2.1 is the taller row, not the quieter one** (issue #138). The 1-px
  rule above it is `--line-3` on the panel background, **2.22:1** — that is a table
  line, and a table line means "another row follows", not "the enumeration ends and
  the sum begins". The sum bar is 50 % taller than the bars it sums (14 → 21 px)
  and keeps their segment colours, so it reads as the same quantity, larger,
  instead of as a different one. What #106 decided stands: no ring, no second
  colour, no opacity.

### Fixed

- **A not-loaded model is not routable, lock or no lock** (issue #116). The
  availability column read `model_available()` — allow-list membership — and
  called it residency, so a listed-but-cold model claimed it could serve a
  request that would in fact cost a several-hundred-GB load. `HostRuntime.
  model_routable()` answers the stronger question, `routable` is reported in
  `/healthz` and `/admin/models`, and the eviction lock is deliberately not an
  input: a lock forbids an unload, it never proves a load.

- **The dashboard waited for a reachable node before it could be served** (issue
  #117). The app's lifespan awaited the whole host resolution, and the dashboard
  is served by that same app: with oMLX on the M5 switched off and only the M3
  reachable, uvicorn accepted no connection for 30 s (measured; the live log has
  the same shape at 45 s) and the page appeared only once a node answered.
  Resolution now runs in a background task, `alpha` reads `down` as soon as
  its address list is exhausted, and the walk probes the addresses concurrently
  with a 1.5 s connect cap instead of 5 s per entry — the startup itself no
  longer waits at all. A failed host is no longer resolved a second time by the
  refresh that follows it.

- **The tok/s column no longer presents the machine's capability as a model's
  rate** (issue #124). Two numbers lived in one cell: the per-(host, model) EMA
  over measured decode rates, and `hosts[].tg_tps` from `config.yaml`, which is a
  measurement of the *machine* from an AS-6 run. With no measurement for the
  model, the cell fell back to the machine's number, so a model that had never
  been served inherited a plausible rate belonging to a different model. The cell
  now shows the measured rate or a dash, and `MarkItDown` never shows a value.
  "Not measured yet" and "measured zero" look different and say different
  things; the machine capability stays in the tooltip, labelled as a property of
  the machine.

- **A thread with no traffic is visible as such** (issue #93). A conversation
  whose client session id changed mid-conversation can never be resumed, yet it
  kept a slot and a row until the TTL expired — indistinguishable from a live
  idle thread, while the operator reads the thread count as "what is running".
  The Threads tab now marks a thread silent after 900 s (the placement thread
  decay window, half the default TTL) with its age in its unit, and the host card
  splits its slot figure into active and idle. No backend field was added: the
  payload already carried `idle_seconds`, `turns` and `active`, and the TTL
  behaviour is unchanged.

- **A red full suite means something again** (issue #125).
  `test_streaming` failed intermittently in the full suite and never in
  isolation, and the failing assertion was a `200` that came back `503` — not the
  double upstream call the issue had assumed. Since #117 `Fleet.start(blocking=
  False)` resolves the hosts in a background task, so the fixture's
  `server.started` only meant the socket was open, and a request arriving before
  the first probe found no healthy host; it only lost on a busy machine, which is
  why the same tree produced three failures in six runs.
  `live_proxy_factory` gained `wait_healthy=True` — polls `/healthz` until every
  configured host has a verdict, 15 s deadline, and raises with the last payload
  instead of timing out silently. The default stays `False`, because
  `test_doctor` and `test_coldstart` deliberately observe the unresolved state.
  Full suite after the fix: 1406 passed in 241.98 s, twice green in a row.

- **The distribution fills its row instead of ending 38 px short** (issue #127).
  `#dist` was `width:max-content` with tracks capped at 660 px, so the container
  shrank to its content — 1323 px inside a 1361 px section — and the operator
  read the dead strip on the right as a layout error while the fleet cards below
  filled their row. `width:100%` with `minmax(470px,1fr)` fills it: 679 + 679 at
  1440 px, 509 + 509 at 1100, 0 px dead at either width, and three blocks wrap
  instead of squeezing. 470 and not the old 380 because the inner row is 418 px
  of fixed columns plus padding — three equal columns of ~453 px would have
  collapsed the host column. The 660 px cap did not disappear, it moved from the
  track to `:only-child`, so a single block still does not stretch across the
  page.

- **A button that could only produce a 409 is greyed, with the reason** (issue
  #128). The Calibrate button was disabled by two conditions — a running job, a
  bound thread — while the endpoint refuses on a third one the button never read
  (`host.locked_loaded()`), so it was enabled exactly when the click would be
  refused: measured on 21.11., `alpha` greyed for its thread while `beta`
  looked free with DeepSeek locked *and* resident on it. The page already carried
  the fact in `models[].hosts[].lock`, the same predicate, so no new field and no
  second computation. The lock is the third reason in the endpoint's order — job
  → thread → lock — and the tooltip names the one that would refuse first, so
  the page never promises a different cause than the answer names. `force=1`
  overrides the thread, never the lock, and both tooltips say so: that is the
  difference between "wait" and "unlock".

- **The sparkline no longer tears on one late probe** (issue #129). `SPARK_HOLD
  = 1` bridged exactly one empty bucket, and with a 5 s bucket against a 10 s
  probe one empty bucket is the *normal* cadence — the threshold sat on the
  boundary, so a few seconds of jitter lifted the pen. Measured on the M5
  DeepSeek series over 180 buckets: 88 gaps of length 1 and one of length 2, and
  that single gap broke the line into two subpaths at `dy 0` — a break at the
  same height, carrying nothing about the traffic. The hold is 2 now, the cadence
  plus one probe of slack, and a gap of 3 or more still tears, so "a probe that
  did not happen breaks the line" holds; the readout bridges with the same
  constant, and `stats.py`'s reference constant follows instead of documenting
  the old rule.

- **On a phone the two tables stop being tables** (issue #134). Measured on the
  operator's Android at a 492 px viewport: the value column of 6.5 was 0 px wide,
  so `0.0.0.0:8010` broke into three lines and read as three numbers, and the log
  table was 622 px in a 356 px panel with `Modell` and `Thread` headers at the
  same x, 95 = 95. One cause for both: `table-layout:fixed` with `<colgroup>`
  widths tuned for ≥1100 px hands the auto column a negative remainder, clamped
  to 0, and its content then overflows a box that is not there. Below 700 px both
  tables are one block per row now: the identity on the first line (scope and
  parameter with the #131 stripe in front, timestamp and thread key for the log),
  the rest as labelled pairs from `data-label`, values over the full panel width.
  Measured at 390 px: all 40 value cells 358 px (min = max), no token broken
  character-wise, `documentElement.scrollWidth == innerWidth`. Above the
  breakpoint nothing moved — 96/280/703/150/132 for 6.5 — and the desktop widths
  are pinned by a test. `display:block` costs the table semantics for assistive
  technology, which is why every value keeps its own label instead of leaning on
  a header row that is gone.

- **A chip is as tall above the text as below it** (issue #136). `.th-meta` is a
  flex row whose items stretch to the height of the delete button — deliberate,
  because the dotted underline of a `.tip` sits at the edge of its own box — but a
  `.chip` paints a background and a border, so the stretch became 4 px of empty box
  below the text: `still` measured 3.0 px above and 7.4 px below, while the button
  next to it was symmetric. A chip keeps its own height and is centred; only text
  fields stretch. The fix exposed a second defect in the same element: `.tip`
  stands later in the stylesheet than `.chip`, and its shorthand had replaced the
  chip's bottom border with a grey dotted line that the stretch had hidden 7.4 px
  below the text. A chip now keeps its own border and the tooltip stays on
  `data-tip`; measured after both: 21.4 px, 3.0 / 3.4, bottom edge identical to the
  top.

- **The work table's headers carry their own explanation** (issue #138). `RQ`,
  `PTK` and `CTK` are readable only to someone who already knows them, and the
  sentence stood on the number below and nowhere on the head. The header now
  carries the same tooltip (`tip.load_requests`, `tip.load_prompt_tokens`,
  `tip.load_completion_tokens`) — no new string, no second source that can age.

## [0.1.1] - 2026-11-20

### Added

- **`docs/competitive-analysis.md`** (issue #110) — what exists in the field and
  what does not, in four profiles under `docs/comparisons/`: **LiteLLM**
  (v1.110.2 — keys, budgets, spend tracking, 100+ provider adapters, the
  surrounding platform RIPPLE deliberately does not build), the **open-source
  landscape** (435 repositories screened, 16 profiled), the **commercial options**
  (12 products, list prices), and RIPPLE's **own inventory**, where every `yes`
  carries a `file:line`. `factcheck.md` is the audit trail: 40 claims checked
  against sources and against this repository, 10 corrected — two of them
  weakening our own argument. The verdict the documents defend: no open-source
  and no commercial tool combines thread placement, a deficit-corrected weighted
  share, residency awareness and eviction protection, and for anyone who rents
  capacity the honest answer is to use LiteLLM.

### Changed

- **The documentation no longer contradicts the code** (issue #110).
  `docs/loading-state-contract.md` gains a section "Protected and unprotected
  loads": `cold_load` is the client token both for a load the forecast cleared
  and for a load nobody forecast, so the header promises nothing about eviction,
  and `evict_idle` is a load as well — only the request log line says which
  happened. `warmup` is documented as inert wherever it was still sold as a
  startup feature: `config.py` refuses a non-empty list with a warning and FA-16
  is not implemented, so the key exists to let an old config parse. `model_pinned`
  was defined as an eviction lock; the lock is `model_locked`, and the two are no
  longer used as synonyms. FA-18 no longer claims that nothing is ever evicted —
  `evict_idle` is an eviction RIPPLE allows, and the requirement now names it
  instead of denying it.

### Fixed

- **`hosts[].prefer_resident` was parsed and never read** (issue #111).
  Placement ignored it, so a host marked `prefer_resident: false` behaved exactly
  like one that had not been marked at all — the key was documented, parsed,
  exported, in the overlay allow-list and covered by config tests, and no routing
  code looked at it. A resident model is now tier `resident` only while the
  preference is on; otherwise the host keeps taking part as an ordinary
  cold-load candidate with the note `resident_not_preferred` and the deficit
  decides. The eviction guard is unchanged: a host that would evict a locked,
  pinned or running model stays a 503.

- **`evict_idle` in `/stats` was always 0** (issue #112). The counter was
  incremented in an `elif` that a dispatched request never reached — an
  `evict_idle` placement is a cold load too, so the branch above it always won,
  and the number that says whether the fleet is thrashing read 0 for the lifetime
  of the process. It is counted from the placement tier now, as a separate `if`:
  `cold_load` stays "a load RIPPLE supervised" and `evict_idle` becomes the
  subset that first had to unload an idle model. Counting from the tier and not
  from the reason keeps the loads of a degraded host, whose reason is
  `degraded_host`.

## [0.1.0] - 2026-11-19

### Added

- **Sites as data** (issue #67) — a top-level `sites:` block of named locations
  with the CIDR networks they contain, and `hosts[].site` naming one by id. The
  claim is checked: the address a node is actually reached through is matched
  against the networks, and a contradiction is one `ripple doctor` line naming
  both sites plus a hint on the fleet card. `hosts[].replication.address` is an
  ordered list now (bridge first, LAN as the fallback — the first reachable
  entry wins and a fallback is reported). Placement never reads a site. A bare
  `site: SA` still parses; an unknown id is a warning, not an error.
- **`ripple init`** — first-run configuration for a machine that has nothing:
  port check that names the process holding it, nodes from discovery, an optional
  oMLX key (continuing without one works and is reported), a generated 32-hex
  admin key, one client key shown exactly once, and both files written with mode
  0600. `--yes` runs the same code path as the conversation.
- **`ripple discover`** — finds oMLX nodes without an address list. Probes the
  addresses it is given (default `127.0.0.1:8000`) and sweeps a network only with
  `--scan`. Identity over `GET /api/cluster/node_id`, admin rights probed
  separately so a 401 is a verdict about rights, not about the node.
- **`ripple doctor`** — one line per check, `OK`/`WARN`/`FAIL`, exit 0/1, ending
  in a real completion through the proxy. Missing admin rights are a warning, not
  a failure.
- **Packaging** — `pyproject.toml` (PEP 621), distribution `ripple-proxy`,
  console script `ripple`, `requires-python = ">=3.9"`, static assets as package
  data. A wheel test builds the real distribution and counts the 16 i18n
  catalogs, so a wheel that starts the dashboard without translations fails in CI
  rather than in front of a user.
- **`tools/coldstart.sh`** — the cold start as a script: clone, fresh venv,
  `init --yes`, `doctor`, a completion, `doctor` again against the running proxy,
  the dashboard, then stop and report.
- **`tools/secret_scan.py`** — scans the tracked tree and the whole history for
  secrets, including a literal search for a short key (`--key-file`), because a
  four-character key has no entropy signature. Prints redacted samples only.
- `SECURITY.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, and the internal cold-start
  handover notes (not published).
- **`docs/install.md`** (English, primary) and **`docs/install-de.md`** — the
  walkthrough from nothing to a running proxy:
  requirements, `pip install .` (and why not `-e`), the real `init` conversation,
  `doctor`, the client switch, the dashboard with screenshots, keys, and the
  failures that actually happened.
- **`docs/coldstart-report.md`** — the report of the first cold start on a second
  machine (macOS, system Python 3.9.6): what broke, in the order it broke, why,
  what was changed, and which check covers each finding today.

### Changed

- **`docs/measurements.md` replaces `docs/as6-measurement.md` and
  `docs/load-report.md`.** One document, two parts: what a node can do (decode,
  prefill, and what that says about the 3 : 2 weights) and what the fleet did
  (busy time per host, hourly shape, thread movements). Every number and table is
  carried over unchanged; the internal acceptance-step name is gone from the
  title. A closing section, "Measuring your own node", shows how to run
  `tools/as6_bench.py` against your own machine and what its number does not say.
- `config.example.yaml` no longer carries a live admin key, this fleet's
  `node_id`s, internal addresses, measured throughput or operator paths. It is a
  generic single-node example with the tuning knobs documented.
- `deploy/ai.ripple.plist` is a template (`__REPO__`, `__HOME__`);
  `deploy/install-launchd.command` renders it while installing.
- `ripple/__main__.py` dispatches `init`/`discover`/`doctor` by hand before
  argparse, so `--config`, `--check` and `--version` behave exactly as before.
- **`tools/coldstart.sh` takes `--model`.** Step 7 completed against whatever
  `/v1/models` listed first; on a node that serves real traffic that is the
  453 GB model, so a protocol run could have triggered its load. A named model is
  used when given, and a model the proxy does not offer is a red step.

### Fixed

- **`ripple doctor` called a node healthy when its answer was empty** (issue
  #82). The completion probe sent `max_tokens: 5` and only counted `choices`,
  so a reasoning model that spent the whole budget on its reasoning channel
  returned `content: ""` and the check said `OK` — the exact shape of the first
  cold start's finding 1. The probe now asks for one word and requires it back,
  and an empty answer is a `FAIL` naming the cause ("the node answers, but the
  answer is empty — a reasoning model spent the whole 64-token budget on its
  reasoning channel; raise max_tokens or clients see empty replies"). The budget
  is measured, not rounded: 5, 6, 8 and 10 tokens produced no visible text in
  any run, 16 did in every run, 64 in 43 of 43 (32 failed once in 19). The check
  costs about 0.8 s more per `doctor` run (median 558 ms → 1332 ms against the
  live fleet), because the probe now generates 12-26 tokens instead of five.

- **A loopback node is not placed, and `doctor` says so instead of skipping in
  silence** (issue #76). A fresh `ripple init` writes the `sites:` block but no
  `site:` on `127.0.0.1`, so the site check from #67 could only ever `SKIP` on the
  reference path, while a hand-written `site:` on that node turned it into a
  `WARN`. Loopback is in no site's networks on purpose: every machine's loopback
  is `127.0.0.1`, so a site containing it would contain every machine, and an
  `OK` there would report a comparison that never happened. `init` now prints one
  line naming the loopback node, `doctor` reports `SKIP` with the loopback as the
  reason (also when a `site:` was declared — unconfirmed, not contradicted), and
  both install guides state it.

- **The proxy did not start on Python 3.9.** FastAPI evaluates the annotations of
  a route handler when it builds the app, and `minutes: float | None` raises
  `TypeError: unsupported operand type(s) for |` there — every module imported
  cleanly, so neither an import check nor `--check` could see it. The annotation
  is `Optional[float]` now, guarded by a static test that runs everywhere.
- A test read the operator's real `~/.ripple/maintenance.yaml` and failed on the
  day a node was taken out of the pool, for reasons unrelated to the code under
  test. The harness keeps every operator store under `/tmp` again, with a guard
  test for all four paths.
- `ripple init --yes` ignored `$OMLX_API_KEY` and configured a read-limited node,
  then skipped its own test request.
- `ripple init` wrote the whole offered model list into `models:`. An empty
  allow-list means "everything this node offers"; a frozen list hides every model
  added to the node later, silently.
- `config.yaml.bak*` was not ignored, although a backup carries the same secrets
  as the file.
- **`tools/coldstart.sh` read a reasoning-only answer as a failure.** `max_tokens:
  16` was spent entirely on `reasoning_content`, so `content` came back empty and
  step 7 went red on a machine where the same model simply thought longer. The
  budget is 128 now and the extraction accepts `content` or `reasoning_content`,
  naming which one it used.
- **`tools/coldstart.sh` went red on `--no-key`,** which is a documented path.
  Measured: oMLX answers 401 on `/v1/models` without a key, so a read-limited node
  offers no model and there is nothing to complete with. That is a note now, not a
  failure; with a key an empty model list is still an error.
- **`tools/coldstart.sh` never proved doctor's `dashboard` and `completion`
  checks.** The only doctor run happened before the proxy started, so both were
  `SKIP` — the report said "green, including a real completion" without one ever
  having run. A second `ripple doctor` against the running proxy is step 8 now,
  and a red doctor stops the run.
