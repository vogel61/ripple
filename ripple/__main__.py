"""CLI entry point: run the proxy, validate the configuration, re-resolve hosts."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, default_config_path, load_config

#: `ripple <word>` — the first-run commands. Each module owns its own flags and
#: returns an exit code; `main()` only decides which one runs.
FIRST_RUN_WORDS = ("init", "discover", "doctor")

#: `ripple <word>` — the service commands (#121). One module owns all five, and
#: `ripple` with no word at all is `start`.
SERVICE_WORDS = ("start", "status", "shutdown", "restart", "update")

#: `ripple -perm` (#122) — autostart, which is a flag rather than a word because
#: it qualifies `shutdown` too. It has to be routed before argparse sees it,
#: otherwise the foreground parser would answer "unrecognized arguments".
AUTOSTART_FLAG = "-perm"

SUBCOMMANDS = FIRST_RUN_WORDS + SERVICE_WORDS


def _same_file(stream: object, path: Path) -> bool:
    """True when `path` is the very file `stream` is writing to.

    launchd points StandardOutPath/StandardErrorPath at the same file that
    `log_file` names, so sys.stderr *is* the log file. Comparing inode and
    device (not the path strings) catches that regardless of symlinks,
    relative paths or a `~` in the config.
    """
    try:
        fd = stream.fileno()          # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return False                  # not a real file (StringIO, closed stream)
    try:
        st_stream = os.fstat(fd)
        st_path = os.stat(path)
    except OSError:
        return False                  # the file does not exist yet: cannot be it
    return (st_stream.st_dev, st_stream.st_ino) == (st_path.st_dev, st_path.st_ino)


def setup_logging(cfg: Config, level: str | None = None) -> None:
    lvl = getattr(logging, (level or cfg.log_level).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(lvl)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    path = Path(cfg.log_file).expanduser() if cfg.log_file else None
    # Under launchd, stderr (and stdout) are redirected into the log file, so a
    # StreamHandler on top of the file handler writes every record twice into
    # one file — 1476 request lines, 740 distinct. When they are the same file,
    # only the rotating handler is registered: it can rotate, a launchd
    # redirect cannot.
    redirected = [name for name, stream in (("stderr", sys.stderr), ("stdout", sys.stdout))
                  if path is not None and _same_file(stream, path)]

    if not redirected:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        root.addHandler(stream)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # NFA-8: rotating file, 10 MB x 5 — never an unbounded log file
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=10 * 1024 * 1024,
                                                  backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if redirected:
        logging.getLogger("ripple").info(
            "%s is redirected into %s — using the rotating file handler only "
            "(one record per line; a launchd redirect cannot rotate)",
            " and ".join(redirected), path)


async def run_check(cfg: Config) -> int:
    """FA-20: validate the configuration, probe both upstreams and print the
    routing table — without accepting requests."""
    from .hosts import Fleet
    from .stats import Stats

    print(f"RIPPLE {__version__} — configuration check")
    print(f"config: {cfg.source}")
    print(f"listen: {cfg.listen_host}:{cfg.listen_port}")
    print(f"strategy: {cfg.placement.binding_strategy}  "
          f"hysteresis: abs>{cfg.placement.balance_abs_threshold} "
          f"rel>{cfg.placement.balance_rel_threshold}")
    if cfg.warnings:
        print("\nwarnings:")
        for w in cfg.warnings:
            print(f"  ! {w}")

    fleet = Fleet(cfg, Stats())
    try:
        await fleet.start(warmup=False)
        print(f"\nfleet capabilities: {', '.join(sorted(fleet.capabilities)) or 'none'}")
        if "residency" not in fleet.capabilities:
            print("  ! eviction_guard: false (FA-18 switched off)")
        print()
        for h in fleet.hosts:
            print(f"[{h.cfg.name}] role={h.cfg.role} site={h.cfg.site or '-'} "
                  f"weight={h.cfg.weight} target={cfg.target_share(h.cfg):.0%}")
            print(f"  address:  {h.resolved_address or '-'}   status: {h.status}")
            print(f"  node_id:  {h.node_id_seen or '-'}")
            print(f"  version:  {h.version or '-'}   rtt: {h.rtt_ms} ms")
            if h.ceiling_bytes:
                print(f"  memory:   {h.used_bytes}/{h.ceiling_bytes} bytes "
                      f"({(h.used_bytes or 0) / h.ceiling_bytes:.1%}), "
                      f"guard={'on' if h.guard_enabled else 'OFF'}")
            print(f"  models:   {len(h.models)} offered, "
                  f"{sum(1 for m in h.models.values() if m.loaded)} loaded")
            if h.last_error:
                print(f"  error:    {h.last_error}")
            print()
        print("routing table (model -> host: state):")
        rows: dict[str, list[str]] = {}
        for h in fleet.hosts:
            for mid, info in h.models.items():
                if not h.cfg.model_allowed(mid):
                    continue
                state = "loaded" if info.loaded else "available"
                rows.setdefault(mid, []).append(f"{h.cfg.name}:{state}")
        for mid in sorted(rows):
            print(f"  {mid}: {', '.join(rows[mid])}")
        return 0 if fleet.healthy_hosts() else 2
    finally:
        await fleet.stop()


def run_foreground(cfg: Config) -> int:
    """Run the proxy in this process until it stops — the blocking code path.

    This is what launchd calls (`python -m ripple --config …`), what
    `deploy/start-ripple.command` ends up in, and what `ripple` alone runs when
    no launchd job owns the service (#121). One function, so the three cannot
    drift apart.
    """
    import uvicorn
    from .proxy import Ripple

    # Issue #81: one line, once, at start-up — the same sentence `ripple
    # doctor` reports, built in one place (`Config.rebind_cannot_fire`). A
    # rebind that can never fire is otherwise invisible: the proxy runs,
    # requests are served, and only the balancing between nodes is missing.
    _rebind_finding = cfg.rebind_cannot_fire()
    if _rebind_finding is not None:
        logging.getLogger("ripple").warning("%s", _rebind_finding)

    app = Ripple(cfg).app
    # `proxy_headers=False`: uvicorn's own middleware rewrites `scope["client"]`
    # from `X-Forwarded-For` for every loopback peer — so any local process could
    # write its own origin into the request log, and slip past the loopback auth
    # exemption (observed live: `X-Forwarded-For: 1.2.3.4` from 127.0.0.1 was
    # logged as `from=1.2.3.4` and answered 401). The one trust list is
    # `proxy.trusted_proxies`, applied in `Ripple.client_address`; uvicorn must
    # not decide this behind it.
    uvicorn.run(app, host=cfg.listen_host, port=cfg.listen_port,
                log_config=None, access_log=False, timeout_keep_alive=65,
                proxy_headers=False)
    return 0


_HELP_EPILOG = """\
service commands — every one of them reads RIPPLE_LABEL (default
ai.ripple) and RIPPLE_PORT (default: listen_port from the config):
  ripple              start here, or report that it already runs (exit 0)
  ripple status       running/stopped, pid, port, version, uptime, log path
  ripple shutdown     stop now — the launchd job if there is one, else the
                      listener; the job stays installed, so it returns at login
  ripple restart      stop, start, wait until the dashboard answers, report seconds
  ripple update       fetch, run the suite, restart — only if the suite is green

autostart — the switch for "runs by itself after a login":
  ripple -perm        autostart ON: render the launchd job from THIS checkout,
                      install it and start it. It starts at login and is kept
                      running. A manual instance on the port keeps running;
                      launchd takes over at the next login. Prints one line:
                      autostart: on (ai.ripple, /path/to/checkout)
  ripple shutdown -perm
                      autostart OFF: stop the proxy first, then remove the job,
                      then verify both (launchctl print fails, no listener).
                      Prints one line: autostart: off (ai.ripple)
  -perm alone is ON; -perm after `shutdown` is OFF; with start/status/restart/
  update it is refused, because none of them changes autostart.

first-run commands: ripple init, ripple discover, ripple doctor
  `ripple init` refuses while a config.yaml exists; `ripple init --force`
  replaces it, and copies the previous configuration and key store aside first
  (config.yaml.bak-<YYYYmmdd-HHMMSS>, keys.yaml.bak-<YYYYmmdd-HHMMSS>).

The flags above belong to the foreground run (`ripple` alone, and exactly what
the launchd job calls). `ripple update` runs the suite named by
RIPPLE_UPDATE_SUITE (default: `<venv python> -m pytest -q`); RIPPLE_CONFIG and
RIPPLE_PLIST override the config file and the LaunchAgent path.
"""


def main(argv: list[str] | None = None) -> int:
    # Subcommands first, and by hand rather than through argparse subparsers:
    # `ripple --config x` has to keep working exactly as before, and a subparser
    # would swallow the flags that come before the word. One word, one module —
    # `ripple init`, `ripple discover`, `ripple doctor` for the first run, and
    # `ripple start|status|shutdown|restart|update` for the service (#121).
    raw = list(sys.argv[1:] if argv is None else argv)
    if AUTOSTART_FLAG in raw:
        # `ripple -perm` and `ripple shutdown -perm` (#122). Routed here, before
        # the foreground parser, because `-perm` is not one of its options — and
        # before the word dispatch, because service.main() is the one place that
        # decides whether the flag fits the word (`shutdown`) or not.
        from . import service
        return service.main(raw)
    if raw and not raw[0].startswith("-"):
        if raw[0] in SERVICE_WORDS:
            from . import service
            return service.main(raw)
        if raw[0] in FIRST_RUN_WORDS:
            module = importlib.import_module(f".{raw[0]}", __package__)
            return int(module.main(raw[1:]))
    if not raw:
        # `ripple` alone means start: report a running launchd job, or run in
        # the foreground. It must not race a second instance onto the port.
        from . import service
        return service.main([])

    p = argparse.ArgumentParser(prog="ripple", description="RIPPLE — thread-based "
                                "placement proxy in front of several oMLX instances",
                                epilog=_HELP_EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default=default_config_path(),
                   help="path to the YAML configuration (default: "
                        "~/.ripple/config.yaml, else ./config.yaml)")
    p.add_argument("--check", action="store_true",
                   help="validate configuration and upstreams, then exit")
    p.add_argument("--reinit", action="store_true",
                   help="re-resolve all host addresses before starting")
    p.add_argument("--host", help="override listen_host")
    p.add_argument("--port", type=int, help="override listen_port")
    p.add_argument("--log-level", help="override log_level (debug, info, warning)")
    p.add_argument("--version", action="version", version=f"RIPPLE {__version__}")
    args = p.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if args.host:
        cfg.listen_host = args.host
    if args.port:
        cfg.listen_port = args.port
    setup_logging(cfg, args.log_level)

    if args.check:
        return asyncio.run(run_check(cfg))

    if args.reinit:
        async def _reinit() -> None:
            from .hosts import Fleet
            from .stats import Stats
            fleet = Fleet(cfg, Stats())
            await fleet.start(warmup=False)
            changed = await fleet.reinit()
            print(json.dumps({"changed": changed}, indent=2))
            await fleet.stop()
        asyncio.run(_reinit())
        return 0

    return run_foreground(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
