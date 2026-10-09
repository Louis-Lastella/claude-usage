#!/usr/bin/env python3
"""Report this machine's Claude Code usage to a Usagecast dashboard, so its limit split counts it as Claude Code.

  python3 cc-report.py --url https://dashboard.example:8443 --token TOKEN [--host NAME] [--install]
  python3 cc-report.py              # report once with the saved settings (what the background job runs)
  python3 cc-report.py --dry-run    # show what would be sent, send nothing

Reads ~/.claude/projects/**/*.jsonl (or $CLAUDE_CONFIG_DIR/projects), sums the token counts of the last 8 days per hour
and model and POSTs them to <url>/api/ingest. Only numbers leave the machine: no prompts, no file names, no paths.
--url, --token and --host are saved to ~/.config/usagecast/report.json (mode 600). --install copies this script next to
it and, on macOS, loads a LaunchAgent that reports every 10 minutes; on Linux it prints a crontab line instead.
Standard library only, Python 3.9 or later.
"""
import argparse, json, os, platform, plistlib, shutil, socket, subprocess, sys, time, urllib.error, urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HOME = Path.home() / ".config" / "usagecast"
CONF, LABEL = HOME / "report.json", "dev.usagecast.report"
PROJECTS = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"
DAYS = 8


def hours(since):
    """[[hour, model, input, cache_write, cache_read, output, of the writes 1-hour cache]] since `since`. Claude Code
    writes one line per content block, all with the same message id and usage: counted once."""
    agg, seen = defaultdict(lambda: [0, 0, 0, 0, 0]), set()
    for p in PROJECTS.rglob("*.jsonl"):
        try:
            if p.stat().st_mtime < since:
                continue
            fh = open(p, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    x = json.loads(line)
                    msg, u = x["message"], x["message"]["usage"]
                    key, model = (msg.get("id"), x.get("requestId")), str(msg.get("model") or "")
                    t = datetime.fromisoformat(x["timestamp"].replace("Z", "+00:00")).timestamp()
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
                if x.get("type") != "assistant" or key in seen or not model.startswith("claude") or t < since:
                    continue
                seen.add(key)
                a = agg[(int(t) - int(t) % 3600, model)]
                for i, k in enumerate(("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")):
                    a[i] += u.get(k) or 0
                a[4] += (u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0
    return [[h, m, *v] for (h, m), v in sorted(agg.items())]


def load():
    try:
        return json.loads(CONF.read_text())
    except (OSError, ValueError):
        return {}


def save(conf):
    HOME.mkdir(parents=True, exist_ok=True)
    CONF.write_text(json.dumps(conf, indent=2))
    CONF.chmod(0o600)


def send(conf, body):
    req = urllib.request.Request(conf["url"].rstrip("/") + "/api/ingest", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": "Bearer " + conf["token"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def install(conf):
    target = HOME / "cc-report.py"
    if Path(__file__).resolve() != target:
        shutil.copy(__file__, target)
    if platform.system() == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(plistlib.dumps({"Label": LABEL, "ProgramArguments": [sys.executable, str(target)], "StartInterval": 600,
                                          "RunAtLoad": True, "StandardErrorPath": str(HOME / "report.log")}))
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        r = subprocess.run(["launchctl", "load", "-w", str(plist)], capture_output=True, text=True)
        print(f"LaunchAgent {LABEL} loaded, reports every 10 minutes" if r.returncode == 0 else f"launchctl failed: {r.stderr.strip()}")
    else:
        print("Add this line with `crontab -e` to report every 10 minutes:")
        print(f"*/10 * * * * {sys.executable} {target} >/dev/null 2>&1")


def main():
    ap = argparse.ArgumentParser(description="Report Claude Code usage to a Usagecast dashboard.")
    ap.add_argument("--url"); ap.add_argument("--token"); ap.add_argument("--host")
    ap.add_argument("--install", action="store_true"); ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    conf = load()
    given = {k: v for k, v in (("url", a.url), ("token", a.token), ("host", a.host)) if v}
    if given:
        conf.update(given)
        save(conf)
    conf.setdefault("host", socket.gethostname().split(".")[0][:40] or "machine")
    since = time.time() - DAYS * 86400
    body = {"host": conf["host"], "since": int(since) - int(since) % 3600, "hours": hours(since)}
    if a.dry_run:
        tok = sum(sum(r[2:6]) for r in body["hours"])
        print(f"host {body['host']}: {len(body['hours'])} hour/model rows, {tok:,} tokens, models {sorted({r[1] for r in body['hours']})}")
        return 0
    if not conf.get("url") or not conf.get("token"):
        print("Missing --url or --token (saved in %s once given)" % CONF, file=sys.stderr)
        return 2
    try:
        status = send(conf, body)
    except urllib.error.HTTPError as err:
        print(f"Usagecast answered {err.code}" + (": wrong token?" if err.code == 401 else ""), file=sys.stderr)
        return 1
    except OSError as err:
        print(f"Usagecast not reachable: {err}", file=sys.stderr)
        return 1
    if a.install:
        install(conf)
    if given or a.install:
        print(f"reported {len(body['hours'])} rows as '{conf['host']}' (HTTP {status})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
