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
| **Command** | `./.venv/bin/python tools/mockup_snapshot.py --manual-crops --screenshot docs/images/readme-fleet.png --out /tmp/ripple-mockup.html` |
| **Commit** | `ab59913` |
| **Date** | 2026-11-19T17:33:23+01:00 |
| **Files** | 20 |

| file | pixels | bytes | shows |
|---|---|---|---|
| `dashboard-fleet-de.png` | 1440 × 1000 | 162903 | the Fleet tab, German, for install-de.md |
| `dashboard-fleet.png` | 1440 × 1000 | 172905 | the Fleet tab, for install.md |
| `dashboard-models-de.png` | 1440 × 1000 | 159009 | the Models tab, German, for install-de.md |
| `dashboard-models.png` | 1440 × 1000 | 156701 | the Models tab, for install.md |
| `manual-fleet-card.png` | 1408 × 1066 | 131592 | one node card: memory bar and metrics |
| `manual-fleet.png` | 1440 × 1000 | 172928 | the Fleet tab: legend, two cards, KPI strip |
| `manual-history.png` | 1440 × 1000 | 247205 | the History tab: filters, rows, footer |
| `manual-keys.png` | 1440 × 500 | 69773 | the key page (/keys) |
| `manual-models.png` | 1440 × 1000 | 156992 | the Models tab: rows, fold, policy fields |
| `manual-node-drain-timeout.png` | 2800 × 416 | 76190 | the deadline passing |
| `manual-node-drain.png` | 2800 × 356 | 59086 | the drain dialog |
| `manual-nodes-scan.png` | 2800 × 228 | 65469 | a sweep over a documentation range |
| `manual-nodes.png` | 1440 × 1000 | 173650 | the Nodes tab, with a preview |
| `manual-ops.png` | 1440 × 1000 | 194869 | the Operations tab with a maintenance window set |
| `manual-restart.png` | 2808 × 438 | 114983 | the restart block (6.6) |
| `manual-tabs.png` | 2792 × 130 | 11950 | the tab bar, six numbered tabs |
| `manual-thread-busy.png` | 1420 × 268 | 27157 | one thread with a request in flight |
| `manual-threads.png` | 1440 × 1000 | 144448 | the Threads tab: six conversations |
| `manual-tooltip.png` | 672 × 350 | 57967 | a tooltip: definition and source |
| `readme-fleet.png` | 2880 × 4240 | 853445 | the Fleet tab, full height (the README picture) |

Detail crops are clipped from the rectangle of the panel they show
(`getBoundingClientRect`, plus a few pixels of air) and rendered at 2×;
full views are the 1440 × 1000 viewport at 1×. `readme-fleet.png` is the
whole Fleet tab at 2×, the picture the README carries.
