# Replicating a model from one node to another (issue #13)

Two machines are connected directly over Thunderbolt 5. A model that exists on one
of them is not routable on the other until its files are there and oMLX has
re-read its model directories. This document is the runbook: what the job does,
what it refuses, how to undo it — and the three answers the issue asked for
before any code was written.

Everything below was read off the running system on **15.11.2026**, not inferred
from the specification. Each answer names the call and the line it came from.

---

## 1. The exact shape of the settings payload

**Read it, put it back, read it again, compare. Three keys must be dropped.**

| Call | Result |
|---|---|
| `GET /admin/api/models` on the M5 (`http://localhost:8000`) and on the M3 (`http://192.0.2.12:8000`) | per model: `id`, `model_path`, `settings`, `pinned`, `is_favorite`, `is_hidden`, `estimated_size`, `actual_size`, `loaded` — the fields the issue names |
| `PUT /admin/api/models/{id}/settings` | takes a **flat** body: `ModelSettingsRequest` in `admin/routes.py` (`extra="forbid"`) |
| the `settings` object of the GET | **86 keys** |
| the fields of `ModelSettingsRequest` | **83** |
| the difference | `display_name`, `description`, `active_profile_name` |

Consequences, all of them in the code:

* The body of the PUT is the `settings` object of the GET **minus those three
  keys** (`replication.settings_exclude`). A whole-object PUT would answer 422
  for the entire body because of `extra="forbid"` — one rejected key would lose
  every accepted one.
* `is_pinned`, `is_favorite`, `is_hidden` and `is_default` **are** fields of
  `ModelSettingsRequest`. There is no separate pin/favourite/hide endpoint to
  call; the same PUT carries them. (Searched: no `@router.post|put|patch` with
  `pin`, `favorite` or `hidden` in `admin/routes.py`.)
* `null` means "the default" in oMLX (the route distinguishes `sent as null` from
  `not sent` via `model_fields_set`). A freshly discovered model already has the
  defaults, so the job sends only non-null values. A key that is `null` on the
  source and non-null on the target stays a mismatch and is reported.
* A model with no settings entry answers `settings: {}` (the M3 does this for
  `Qwen3.8-Flash-Next-DFlash2`). The job then writes nothing and reports
  `settings.sent: 0` — an empty payload is not a success story about parameters.
* The PUT answers `{"success": true, "settings": {...}, "requires_reload": …}`.
  The job does not trust that answer: it re-reads `GET /admin/api/models` and
  compares every key it sent (`ripple.replication.compare_settings`, floats with a
  1e-9 tolerance). A difference becomes `state: failed` with
  `settings.mismatches[]` and a line in `report.not_replicated` — never a green
  job.

## 2. Does `POST /admin/api/reload` require the node to hold nothing loaded?

**No — it empties the node itself, and it preloads the pinned models afterwards.
The maintenance window is therefore mandatory, not polite.**

`admin/routes.py:2943` → `_reload_models()` (`:1456`) → `_apply_model_dirs_runtime()`
(`:1360`):

1. re-reads `model_settings.json`,
2. **unloads every loaded model** (`pool.get_loaded_model_ids()` → `_unload_engine`),
3. clears the pool (`_entries.clear()`, `_current_model_memory = 0`),
4. re-discovers the model directories,
5. `preload_pinned_models()`.

So the reload is not a read. It takes the target's residency away by itself, and a
target with a pinned model starts a cold load in the middle of the job — a load
RIPPLE would otherwise never cause (NZ-4). That is why:

* the target goes into maintenance **before** the copy and comes back **after**
  the settings comparison (window `by="replication"`, not persisted — a RIPPLE
  crash mid-job must bring the node back by itself);
* an operator window on the target is neither replaced nor cleared by the job;
* the job report names the reload result (`reload.ok`, `reload.message`) so a
  refused reload is visible as such instead of as "the model did not appear".

Discovery runs on exactly two occasions besides this route: at startup and after a
finished HuggingFace download (`server.py:2690`). There is no periodic rescan — a
new directory is invisible until someone reloads.

## 3. Does the Thunderbolt link appear as its own address?

**Yes, it is its own address — and no, `ordered_addresses()` does not prefer it.**

`GET /healthz` on RIPPLE (`:8010`) reports:

| Host | `addresses[]` |
|---|---|
| M5-alpha (`0d7114d5-…`) | `http://192.0.2.11:8000`, `http://192.168.77.1:8000`, `http://localhost:8000` |
| M3-beta (`e19d4c08-…`) | `http://192.0.2.12:8000`, `http://192.168.77.2:8000`, `http://198.51.100.49:8000` |

`192.168.77.0/24` is the Thunderbolt bridge: `ifconfig` on the M5 shows
`en4 … inet 192.168.77.1 netmask 0xffffff00`, and the M3 answers on
`192.168.77.2` (verified with a read-only `GET /admin/api/models` over that
address).

The fleet's address choice (`ripple/hosts.py`) is `_directly_connected()`, which
asks "is the target on the same /24 as the source address I would use?":

```
http://192.0.2.12:8000     direct=True   src=192.0.2.11
http://192.168.77.2:8000   direct=True   src=192.168.77.1
http://198.51.100.49:8000  direct=False  src=192.0.2.11
```

Both are on-link, so both count as "direct" and `ordered_addresses()` keeps
configuration order — the **LAN** address wins, and the copy would take the routed
path. The heuristic cannot tell a cable from a LAN segment; nothing in the oMLX
answer says which one is the 80 Gb/s link.

**Consequence:** the copy needs a configured replication address per host —
`hosts[].replication.address`. It is also the SSH destination, so one knob covers
both. Without it the preflight still runs (it falls back to the fleet's order) but
the plan carries a warning, and `config.py` warns at startup.

### 3a. The local side never goes over SSH (issue #28)

`replication.address` is needed for the **remote** side only. RIPPLE runs on the
entry node, so the M5 side of every copy is *this machine*, and its address is not
just useless there — it is fatal: `_rsync_argv` built
`vogel@192.168.77.1:/…` → `vogel@192.168.77.2:/…` and rsync refused with
`both source and destination cannot be remote files` (second real attempt, issue
#13, 16.11.2026 00:40). The local host is therefore identified from what the
configuration already says — the host whose `hosts[].addresses` contains a
loopback entry (`localhost`, `127.0.0.1`, `[::1]`); the M5 lists
`http://localhost:8000`, the M3 lists nothing loopback. That host gets
`ssh_target == ""` (`Endpoint`: empty means "this machine"), whatever
`replication.address` says, and the plan **warns** that the configured address was
ignored — a configured value that is dropped in silence is what costs an evening.
If no host can be identified as local, the plan is **refused** with the fix in the
message ("give the entry host a loopback address in `hosts[].addresses`") and no
argv is built at all, because the two sides would be indistinguishable and rsync
cannot copy from one remote host to another.

## 4. What the job does

One model, one target, one job at a time (`POST /admin/nodes/{target}/replicate`).

| Step | What happens | Refusal / failure |
|---|---|---|
| plan | `GET /admin/api/models` on the source, `df -k` and `test -e` on the target over SSH | every refusal is collected, not just the first |
| maintenance | `maintenance.set(target, by="replication")` | an operator window stays untouched |
| copy | `rsync -a <progress flag> --partial --stats` into `<model_root>/.replication-incoming/<relative>` | cancellable; the staging copy is kept; a refused progress flag is dropped, never the copy (§4a) |
| move | `mv` inside the model root | the model appears complete or not at all |
| register | `POST /admin/api/reload` on the target | failure → job failed, `registered: false` |
| verify | the model must appear in `GET /admin/api/models` with the same `estimated_size` | missing → failed; size differs → `not_replicated` |
| settings | read the source again, `PUT …/settings` flat, read back and compare | mismatch → failed, `settings.mismatches[]` |
| report | bytes, seconds, MB/s, `not_replicated[]` | — |
| back into service | window cleared, unless it was the operator's | — |

### What it refuses

* source and target are the same node;
* the source does not report the model, or reports no `model_path`, or no size;
* the model path is under none of the source's configured model roots (the
  relative subpath cannot be derived);
* no model root for the target, or no `replication.address` for a **remote**
  target (the local side never needs one, §3a);
* no host can be identified as this machine, or neither side of the copy is it —
  a remote→remote copy does not exist (§3a);
* the target is not reachable over SSH (with the SSH error text);
* the free space cannot be read, or is below `size × 1.05`;
* the model is already on the target and `overwrite` was not asked for.

### Why the staging directory

`ripple/replication.py` copies into `<model_root>/.replication-incoming/<relative>`
and moves it into place when rsync is done. Two measured reasons:

* oMLX's discovery **skips `.`-entries** (`model_discovery.py`: `subdir.name.startswith(".")`),
  so a half-copied directory is never registered as a model — not even if a reload
  happens while the copy runs;
* the staging directory is on the same filesystem as the destination, so the
  `mv` is a rename: the model is either complete or absent.

A cancelled copy therefore leaves no half-registered model, and the next run
resumes from the staging directory instead of starting over.

### 4a. What the copy needs from rsync (issue #26)

`rsync` on the fleet is **openrsync**, not GNU rsync:

```
$ rsync --version
openrsync: protocol version 29
rsync version 2.6.9 compatible
```

openrsync has no `--info=progress2`. It prints its usage and exits 1 — and the
last line of that usage, `\tsource ... directory`, is what a job reports as the
error. That is why the first real attempt (issue #13, 15.11.2026, M5 → M3,
73,07 GB) died after 0,15 s with a message that looked like a truncated error
instead of "unknown option". The copy step had never run on this hardware.

**The flag is probed, not assumed.** `rsync --info=progress2 --version` exits 0
on GNU rsync and 1 on openrsync (measured on the M5), so
`SshTransport.rsync_progress_flag` asks exactly that once per transport and
caches the answer:

| Probe answer | Flag the copy uses |
|---|---|
| exit 0, or `--help` documents `--info=FLAGS` / `info=progress2` | `--info=progress2` |
| anything else, or the probe cannot run at all | `--progress` |

`--progress` is the safe choice: both implementations take it. `-a`, `--partial`
and `--stats` are in the argv in every case — `--partial` is what makes an
interrupted copy resumable, `--stats` is what still gives bytes and rate when no
progress flag survives.

**A copy does not fail on an option.** If the chosen flag is rejected anyway
(exit 1 with "unrecognized option" / "unknown option" / "syntax or usage error"),
the copy is retried with `--progress` and then with no progress flag at all. An
option rejection costs milliseconds, a dead job costs the whole copy. The job
reports which flag ran and why it changed:

```
report.progress       "--info=progress2" | "--progress" | "none"
report.progress_note  "" or "rsync rejected --progress; copied with no progress
                      flag — bytes and rate come from --stats, …"
```

A fallback is not a failure: `not_replicated[]` stays empty and the job reaches
`done`.

**Two progress formats, two meanings.** The lines look alike, so the flag that
was passed decides how they are read (`ProgressReader`):

```
# GNU --info=progress2 — cumulative bytes, the copy's own percentage
  1,234,567  45%  123.45MB/s    0:01:23

# openrsync --progress — that file's bytes and percentage, plus a counter
       41943040 100%  348.71MB/s   00:00:00 (xfer#1, to-check=1/3)
```

A per-file percentage is *not* a copy percentage: in the capture above the copy
is half done after the first file while the line says 100 %. openrsync's counter
is what makes an overall number out of it — `to-check=k/m` is the number of
file-list entries checked so far out of m, and the last file's k is m−1, because
openrsync counts the copy's own root directory as an entry it never prints
(measured: 2 files → `1/3`, then `2/3`; 4 files and one subdirectory → `…5/6`).
So the copy's percentage is `(entries checked + the current file's share) /
(m − 1)`, which is 0 % at the start and 100 % at the end. GNU's `to-chk=k/m`
counts *down*, and is read that way.

`bytes_done` is the copy's bytes in both formats (openrsync's are accumulated
across files) and is corrected from `--stats` (`total size is N`) when the run
ends, so a resumed copy that skipped files reports the model's size and not the
traffic. openrsync's eta is the current *file's*, so it is recomputed from the
copy's percentage and the elapsed time; the rate is printed as it is.

**What a configured `rsync_command` must accept:** `-a`, `--partial`, `--stats`
and at least one of `--info=progress2` / `--progress`. The default `rsync` works
on macOS (openrsync) and on Linux (GNU). `brew install rsync` is not required and
not wanted: a shipped feature must not depend on a package the OS does not have.

Tests: `tests/test_replication.py` — the captured openrsync output and a GNU
line through the parser, the probe in both directions, the argv it produces, and
two copies against a stub rsync that refuses the flags the way openrsync does
(one of them through the whole job), plus one real 2 MB local copy.

## 5. Runbook: the live run

The real copy is an operator step in a silent window, not a CI test. 111,6 GB
take minutes, and the target leaves the pool for the duration.

### Configuration

```yaml
replication:
  enabled: true
  # oMLX's own API key — the admin routes take it as a Bearer token.
  admin_key: "${OMLX_API_KEY}"
  # SSH as a template with {target}; user and key belong to the operator's setup.
  ssh_command: "ssh -o BatchMode=yes -o ConnectTimeout=8 {target}"
  rsync_command: "rsync"       # `-a --partial --stats` plus a probed progress
                               # flag; openrsync (macOS) and GNU rsync both work (#26)
  free_space_factor: 1.05
  # model_roots is a fallback for hosts that do not name their own.
  model_roots: []

hosts:
  - name: M5-alpha
    replication:
      address: 192.0.2.11        # the machine RIPPLE runs on: ignored, no SSH (§3a)
      model_roots:                 # `model.model_dirs` of ~/.omlx/settings.json
        - /Users/operator/.omlx/models
        - /Volumes/GLM4TB/models
  - name: M3-beta
    replication:
      address: 192.168.77.2        # the Thunderbolt address, not the LAN one (§3)
      ssh_user: vogel
      model_roots:
        - /Users/operator/.omlx/models
        - /Volumes/GLM4TB/models
```

Without `replication.enabled: true` the endpoints answer 409 and nothing else
changes. `GET /config` shows the effective values (`admin_key: set|none`, never
the key).

### The run

1. **Silent window.** No long generation on either node, no other job. The target
   leaves the pool, so its threads fall back to the other host.
2. **Plan** — read-only, and the same numbers the POST uses:

   ```
   curl -s -H "Authorization: Bearer $RIPPLE_ADMIN" \
     "http://localhost:8010/admin/replication/plan?source=M5-alpha&target=M3-beta&model=Qwen3.8-Flash-Next-oQ4e-mtp"
   ```

   Expect `"ok": true`, `size_gb`, `free_gb`, `target_path`, `staging_path`.
   A refusal names what to fix (`errors[]`).
3. **Start** — one action:

   ```
   curl -s -X POST -H "Authorization: Bearer $RIPPLE_ADMIN" \
     -H 'content-type: application/json' \
     -d '{"source":"M5-alpha","model":"Qwen3.8-Flash-Next-oQ4e-mtp"}' \
     http://localhost:8010/admin/nodes/M3-beta/replicate
   ```

   `202` with `job`, or `409` with the refusal and the plan.
4. **Watch** — the dashboard's *Betrieb → Replikation* panel, or:

   ```
   curl -s -H "Authorization: Bearer $RIPPLE_ADMIN" \
     http://localhost:8010/admin/nodes/M3-beta/replication
   ```

   `job.percent`, `job.rate`, `job.step` (`copy` → `move` → `reload` → `verify`
   → `settings`). The target is out of the pool the whole time
   (`GET /admin/nodes/M3-beta/maintenance` → `by: "replication"`).
5. **Result.** `job.state: done` and `job.report` = `{bytes, seconds, mb_per_s,
   not_replicated: []}`. The acceptance check:

   ```
   curl -s -H "Authorization: Bearer $RIPPLE_ADMIN" http://192.0.2.12:8000/admin/api/models
   ```

   the model is listed with the same `estimated_size`, and its `settings` equal
   the source's (the job already compared them; `job.settings.mismatches` is the
   machine-readable version).
6. **Cancel** if it has to be stopped:

   ```
   curl -s -X DELETE -H "Authorization: Bearer $RIPPLE_ADMIN" \
     http://localhost:8010/admin/nodes/M3-beta/replication
   ```

   The rsync child is terminated, nothing is registered, the staging copy stays.

### What is still missing before the real run

* **The acceptance model is already on both machines.** Measured on 15.11.2026:
  the M3 holds `Qwen3.8-Flash-Next-oQ4e-mtp` under `~/.omlx/models/Jundot/…`
  (`du -sh` = 99 G), the M5 under `/Volumes/GLM4TB/models/Jundot/…` (99 G). The
  preflight will therefore refuse with *"already on M3-beta … pass overwrite=1"*.
  The live run either uses `overwrite: true` (a real 111,6 GB re-copy — the
  acceptance wording) or a model that exists only on the source.
* **A model root on the target that is not the same disk as the staging
  directory** would make the `mv` a copy instead of a rename. Both hosts keep the
  model under the root they are configured with, so this is a note, not a defect.
* The 111,6 GB copy itself: operator window, both machines quiet, and the
  go-ahead. Nothing in this repository writes to the M3.

### Undo

Nothing in RIPPLE has to be undone: the job keeps no state beyond the last job in
memory, and the maintenance window is cleared by the job itself.

On the target (operator, by hand — this is the documented way back):

```sh
rm -rf /Users/operator/.omlx/models/Jundot/Qwen3.8-Flash-Next-oQ4e-mtp   # the copy
rm -rf /Users/operator/.omlx/models/.replication-incoming/Jundot         # a cancelled copy
```

then `POST /admin/api/reload` on the target so oMLX forgets the model. The
settings entry in `~/.omlx/model_settings.json` stays behind; delete it by hand if
the model must disappear completely. A `disabled`/`hidden` rule in RIPPLE's model
policy is separate state and is not touched by any of this.

## 6. Governance

**FA-22, third exception.** The dashboard stays a window. This is the third and
last thing that writes, on the same terms as the calibration and the maintenance
window: operator-initiated, admin key, one model at a time, behind a confirmation
dialog that names source, target, size and free space. It writes to another
machine's filesystem, which is more than the calibration does — which is why it is
the last exception and not a family of them.

**NZ-4.** RIPPLE does not change oMLX, but it may place model files for the
operator — never on its own, never as part of routing.

**NZ-3** ("no copying of model files") is narrowed by the same sentence: the ban
stays for routing and for anything automatic. The replication job is the
operator's hand, executed over SSH.

## 7. Open points

* A pinned model on the target is loaded by the reload (§2) — inside the
  maintenance window, so no request lands in the middle of it, but it does mean a
  job can start a load nobody asked for. It is reported in `reload.message`.
* The settings PUT can make oMLX auto-unload a *loaded* target model for the
  fields that only take effect at engine construction (`requires_reload` in
  `admin/routes.py`). The model the job just copied is not loaded, so this only
  affects a pinned model — and only inside the window.
* `PUT …/settings` accepts 83 keys today. A future oMLX that adds or renames a
  field will show up as a `settings.mismatches[]` entry or a 422, not as a silent
  half-copy: the comparison is against what was sent, not against a guessed list.
* ~~**The copy has two remote sides as soon as both nodes name an address.**~~
  Fixed by #28 (§3a): the host with a loopback entry in `hosts[].addresses` is
  this machine, its `replication.address` is ignored and reported as a plan
  warning, and a plan that cannot name a local side is refused instead of being
  handed to rsync. Measured against the live `config.yaml`: one remote spec per
  direction, the other side a plain path.
