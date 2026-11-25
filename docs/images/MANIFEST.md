# The pictures in docs/images — generated, do not edit

Every file in this directory comes out of one run of
`tools/mockup_snapshot.py`. The run starts a throwaway instance on a free
port, answers `/healthz`, `/stats` and `/config` from the test fleet in
`tools/mockup_fleet.py` (documentation addresses, node-a/node-b/node-c,
invented model names), renders the tabs in headless Chrome and writes the
pictures and this file. Nothing here was cut, scaled or retouched by hand;
a picture that looks wrong is a bug in the tool or in the style sheet.

| | |
|---|---|
| **Command** | `./.venv/bin/python tools/mockup_snapshot.py --manual-crops --screenshot docs/images/readme-fleet.png` |
| **Commit** | `d8b8543` |
| **Date** | 2026-11-25T17:09:44+01:00 |
| **Files** | 21 |

| file | pixels | bytes | shows | state |
|---|---|---|---|---|
| `dashboard-fleet-de.png` | 1440 × 1000 | 163774 | the Fleet tab, German, for install-de.md | admin |
| `dashboard-fleet.png` | 1440 × 1000 | 154716 | the Fleet tab, for install.md | admin |
| `dashboard-models-de.png` | 1440 × 1000 | 155894 | the Models tab, German, for install-de.md | admin |
| `dashboard-models.png` | 1440 × 1000 | 154493 | the Models tab, for install.md | admin |
| `manual-fleet-card.png` | 1408 × 1106 | 136972 | one node card: memory bar and metrics | admin |
| `manual-fleet.png` | 1440 × 1000 | 154651 | the Fleet tab: legend, two cards, KPI strip | admin |
| `manual-history.png` | 1440 × 1000 | 247187 | the History tab: filters, rows, footer | admin |
| `manual-keys.png` | 1440 × 500 | 70528 | the key page (/keys) | read-only |
| `manual-models.png` | 1440 × 1000 | 154497 | the Models tab: rows, fold, policy fields | admin |
| `manual-node-drain-timeout.png` | 2800 × 416 | 76190 | the deadline passing | admin |
| `manual-node-drain.png` | 2800 × 356 | 59086 | the drain dialog | admin |
| `manual-nodes-scan.png` | 2800 × 228 | 65445 | a sweep over a documentation range | admin |
| `manual-nodes.png` | 1440 × 1000 | 188514 | the Nodes tab, with a preview | admin |
| `manual-ops.png` | 1440 × 1000 | 197870 | the Operations tab with a maintenance window set | admin |
| `manual-params.png` | 2808 × 3130 | 671176 | the parameter table: node stripes, global rows | admin |
| `manual-restart.png` | 2808 × 438 | 115007 | the restart block (6.6) | admin |
| `manual-tabs.png` | 2792 × 130 | 11950 | the tab bar, six numbered tabs | admin |
| `manual-thread-busy.png` | 1420 × 268 | 27157 | one thread with a request in flight | admin |
| `manual-threads.png` | 1440 × 1000 | 148561 | the Threads tab: six conversations | admin |
| `manual-tooltip.png` | 672 × 350 | 56231 | a tooltip: definition and source | admin |
| `readme-fleet.png` | 2880 × 4560 | 862806 | the Fleet tab, full height (the README picture) | read-only |

`state` is the tab's admin mode in that picture. `admin` means the demo
key was in `sessionStorage` when it was taken, so the masthead chip says
`admin` and the operator blocks drew live; `read-only` is the state a
fresh tab has — the chip says `read-only` and the write controls are
inert. Most pictures are `admin` because they are about the operator
blocks; the README picture and `/keys` are not.

Detail crops are clipped from the rectangle of the panel they show
(`getBoundingClientRect`, plus a few pixels of air) and rendered at 2×;
full views are the 1440 × 1000 viewport at 1×. `readme-fleet.png` is the
whole Fleet tab at 2×, the picture the README carries.
