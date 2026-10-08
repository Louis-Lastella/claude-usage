#!/usr/bin/env python3
"""claude-usage: zeigt, was in Hermes deine Claude-Usage frisst.

Liest ~/.hermes/state.db nur lesend. Pro Session werden die echten Anthropic-Zahlen (Tokens aus
session_model_usage, bewertet mit den API-Preisen unten) Call für Call auf das verteilt, was dabei im
Kontext stand: Tools, Skills, Plugins, Teile des Systemprompts, Denken, Cache-Neuaufbau nach Pausen.

  python3 app.py                         Server auf 127.0.0.1:7682 (Umgebung: PORT, HERMES_HOME, CLAUDE_USAGE_DATA)
  python3 app.py --test                  Selbsttest
  <hermes-venv>/python app.py --snapshot Systemprompt + Tool-Beschreibungen messen (macht der Server täglich selbst)
"""
import bisect, glob, gzip, html, json, math, os, re, sqlite3, subprocess, sys, threading, time, traceback, urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

HERMES = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
HERMES_PY = HERMES / "hermes-agent" / "venv" / "bin" / "python"
DATA = Path(os.environ.get("CLAUDE_USAGE_DATA", Path(__file__).resolve().parent / "data"))
PORT = int(os.environ.get("PORT", "7682"))
PUBLIC_URL = os.environ.get("CLAUDE_USAGE_URL", "")  # Adresse des Dashboards, damit ein Tipp auf die Warnung es öffnet
CPT = 3.5            # Zeichen pro Token, nur für Größenverhältnisse; die Beträge kommen aus den echten Zahlen
IMAGE_TOK = 3000     # ein Bild im Verlauf, in denselben Einheiten (laut agent.log ~3.900 echte Tokens)
LONG_CTX = 100_000   # ab so vielen Tokens Verlauf gilt ein Schritt als teuer
# "w" = seit dem letzten Reset des Wochenlimits; ohne Limit-Daten wie 7 Tage
PERIODS = {"w": (7, "Limit-Woche"), "1": (1, "24 Std."), "7": (7, "7 Tage"), "30": (30, "30 Tage")}
DEFAULT_P = "w"

# $ pro Mio. Tokens: Input, Cache schreiben 5 min, Cache schreiben 1 h, Cache lesen, Output.
# Quelle: platform.claude.com/docs/en/about-claude/pricing, Stand 08.10.2026. Längstes passendes Präfix gewinnt.
PRICES = {
    "claude-fable-5-1": (10, 12.5, 20, 0.25, 50), "claude-fable-5": (10, 12.5, 20, 1, 50),
    "claude-mythos-5-1": (10, 12.5, 20, 0.25, 50), "claude-mythos-5": (10, 12.5, 20, 1, 50),
    "claude-opus-5-5": (4, 5, 8, 0.2, 20), "claude-opus-5": (5, 6.25, 10, 0.5, 25),
    "claude-opus-4-5": (5, 6.25, 10, 0.5, 25), "claude-opus-4-6": (5, 6.25, 10, 0.5, 25),
    "claude-opus-4-7": (5, 6.25, 10, 0.5, 25), "claude-opus-4-8": (5, 6.25, 10, 0.5, 25),
    "claude-opus-4": (15, 18.75, 30, 1.5, 75),
    "claude-sonnet-5-5": (2, 2.5, 4, 0.1, 10), "claude-sonnet-5": (2, 2.5, 4, 0.2, 10),
    "claude-sonnet-4": (3, 3.75, 6, 0.3, 15), "claude-haiku-4": (1, 1.25, 2, 0.1, 5),
}

# Feste Abschnitte, die Hermes in jeden Systemprompt baut; jeder läuft bis zum nächsten gefundenen Marker.
# SOUL.md, Plugins und Memory-Anbieter kommen je nach Installation dazu, siehe markers().
HERMES_MARKERS = [
    ("Hermes-Grundprompt", r"^You run on |^# Finishing the job"),
    ("Computer-Use-Anleitung", r"^# Computer Use"),
    ("Hermes-Grundprompt", r"^Host: "),
    ("Skills-Liste", r"^## Skills \(mandatory\)"),
    ("Memory + Profil", r"^═+\nMEMORY"),
    ("Hermes-Grundprompt", r"^Conversation started:"),
]
FIXED = {
    "schema": ("Tool-Beschreibungen", "Die Anleitung zu jedem verfügbaren Tool geht bei jedem Schritt mit, auch wenn das Tool gar nicht benutzt wird."),
    "rebuild": ("Cache-Neuaufbau nach Pausen", "Nach {ttl} ohne Schritt verwirft Anthropic den Zwischenspeicher. Der nächste Schritt schreibt dann den ganzen bisherigen Verlauf neu, und Schreiben kostet ein Vielfaches vom Lesen."),
    "break": ("Cache-Bruch ohne Pause", "Der Zwischenspeicher ging verloren, obwohl gerade erst ein Schritt war, und der Verlauf musste neu geschrieben werden. Das löst Hermes selbst aus, wenn sich zwischen zwei Schritten der Anfang des Prompts ändert, nicht deine Nutzung. Erkennbar nur, wo Hermes' Log echte Werte pro Schritt hat."),
    "think": ("Denken", "Claudes Nachdenken vor Antworten und Tool-Aufrufen. Wird als Output abgerechnet, der pro Token am teuersten ist, und bleibt danach im Verlauf."),
    "reply": ("Antworten von Hermes", "Der Text, den Hermes dir zurückschreibt."),
    "user": ("Deine Nachrichten", "Was du schreibst, inklusive weitergeleiteter Texte."),
    "sub": ("Subagenten (delegate_task)", "Helfer-Agenten, die Hermes für Teilaufgaben startet. Jeder hat seinen eigenen Verlauf."),
    "other": ("Ohne gespeicherten Verlauf", "Calls, zu denen Hermes keine Nachrichten gespeichert hat."),
    "task:background_review": ("Hintergrund-Prüfung", "Nach Antworten liest Hermes den Verlauf ein zweites Mal, um Memory und Skills zu pflegen."),
    "task:compression": ("Kontext-Komprimierung", "Fasst zu lange Verläufe zusammen."),
    "task:approval": ("Freigabe-Prüfung", "Lässt riskante Befehle vor dem Ausführen prüfen."),
    "task:title": ("Titel-Erzeugung", "Gibt neuen Sessions einen Namen."),
    "tool:skill_view": ("Skills laden (skill_view)", "Inhalt der Skills, die Hermes nachlädt. Bleibt danach im Verlauf und wird bei jedem weiteren Schritt mitgelesen."),
}
TOOL_HINTS = {
    "terminal": "Lange Befehlsausgaben bleiben im Verlauf und werden bei jedem weiteren Schritt neu gelesen. Ausgaben mit head, tail oder grep kürzen spart hier am meisten.",
    "browser_exec": "Seiteninhalte und Screenshots aus dem Browser sind groß. Gezielt nur den nötigen Text auslesen.",
    "session_search": "Treffer aus alten Sessions bringen viel Text mit. Mit kleinerem limit suchen.",
    "read_file": "Ganze Dateien lesen kostet. Mit offset und limit nur die nötigen Zeilen holen.",
    "vision_analyze": "Bilder kosten viele Tokens und bleiben danach im Verlauf.",
}
SOURCES = {"telegram": "Telegram", "discord": "Discord", "cli": "Terminal (CLI)", "subagent": "Subagenten", "cron": "Cron-Jobs"}
HERMES_PROJ, NO_PROJ = "Hermes selbst", "Ohne Projekt"
OPT_SKIP = {"homebrew", "containerd", "local", "google", "bin", "lib"}  # Systemordner unter /opt und /srv, keine Projekte
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


# ---------- Analyse ----------
def tok(s):
    return len(s or "") / CPT


def price(model):
    key = max((k for k in PRICES if model.startswith(k)), key=len, default="claude-opus-5-5")
    return [p / 1e6 for p in PRICES[key]]


def row_cost(model, i, r, w, o, ttl):
    """Kosten einer Usage-Zeile in $: (Input, Cache lesen, Cache schreiben, Output)."""
    p = price(model)
    return (i or 0) * p[0], (r or 0) * p[3], (w or 0) * (p[2] if ttl > 300 else p[1]), (o or 0) * p[4]


def config_text():
    try:
        return (HERMES / "config.yaml").read_text()
    except OSError:
        return ""


def config_value(section, key, text=None):
    """Einfacher Wert section.key aus Hermes' config.yaml, ohne YAML-Bibliothek."""
    m = re.search(rf"^{section}:[ \t]*\n(?:[ \t]*\n|[ \t]+.*\n)*?[ \t]+{key}:[ \t]*['\"]?([^'\"\s#]*)",
                  config_text() if text is None else text, re.M)
    return m.group(1) if m else ""


def cache_ttl():
    return 3600 if config_value("prompt_caching", "cache_ttl").lower() == "1h" else 300


def plugin_names(text=None):
    """Aktive Plugins (plugins.enabled) und der Memory-Anbieter, ohne Plattform-Adapter wie platforms/ntfy."""
    text = config_text() if text is None else text
    m = re.search(r"^plugins:[ \t]*\n(?:[ \t]+.*\n)*?[ \t]+enabled:[ \t]*\n((?:[ \t]+-.*\n)+)", text, re.M)
    names = re.findall(r"-[ \t]*['\"]?([^'\"\s#]+)", m.group(1)) if m else []
    return [n for n in names + [config_value("memory", "provider", text)] if n and "/" not in n]


def name_rx(n):
    """Name als Regex, egal ob mit -, _ oder Leerzeichen geschrieben und in welcher Groß-/Kleinschreibung."""
    return "(?i:" + "[-_ ]?".join(map(re.escape, re.split(r"[-_ ]+", n))) + ")"


def markers(plugins=None):
    """(Abschnitte des Systemprompts, Einblendungen an Nachrichten) für diese Hermes-Installation."""
    plugins = plugin_names() if plugins is None else plugins
    soul = "Deine Anweisungen (SOUL.md)" if (HERMES / "SOUL.md").is_file() else "Hermes-Grundprompt"
    prompt = [(soul, r"\A"), *HERMES_MARKERS, *(("Plugin: " + n, rf"^#+ .*{name_rx(n)}") for n in plugins)]
    inject = [*(("Plugin: " + n, rf"^(?:<\w+>\s*)?[^\w\n]*{name_rx(n)}") for n in plugins),
              ("Hermes-Hinweise", r"^\[(?:Note|System note|Context from)")]
    return prompt, inject


def segments(text, markers):
    """[(label, abschnitt)] nach Markern; Text vor dem ersten Marker heißt 'Sonstige Einblendungen'."""
    hits = sorted((m.start(), label) for label, rx in markers for m in [re.search(rx, text, re.M)] if m)
    if not hits or hits[0][0] > 0:
        hits.insert(0, (0, "Sonstige Einblendungen"))
    return [(label, text[s:e]) for (s, label), (e, _) in zip(hits, hits[1:] + [(len(text), "")]) if text[s:e].strip()]


def injections(content, api, marks):
    extra = api or ""
    if extra and content:
        extra = extra[len(content):] if extra.startswith(content) else extra.replace(content, "", 1)
    return segments(extra, marks) if extra.strip() else []


def tool_of(tc):
    f = tc.get("function") or {}
    name, args = f.get("name") or "?", f.get("arguments") or ""
    if name == "tool_call":  # nachgeladene Tools: echten Namen nehmen
        try:
            a = json.loads(args)
            name, args = a.get("name") or name, json.dumps(a.get("arguments") or {})
        except (ValueError, AttributeError):
            pass
    return name, args


def key_of(name, args):
    if name != "skill_view":
        return "tool:" + name
    try:
        a = json.loads(args)
        return "skill:" + a.get("name", "?") + (" / " + a["file_path"] if a.get("file_path") else "")
    except (ValueError, AttributeError, TypeError):
        return "skill:?"


def prefix_for(prompt, snap, marks):
    p = defaultdict(float)
    if prompt:
        for label, seg in segments(prompt, marks):
            p["sys:" + label] += tok(seg)
    else:
        for label, t in snap.get("prompt", {}).items():
            p["sys:" + label] += t
    p["schema"] = sum(snap.get("tools", {}).values())
    return p


def simulate(msgs, prefix, extra_out=0.0, inject=()):
    """Spielt eine Session Call für Call nach: was bei jedem API-Call im Prompt stand.

    msgs: (role, content, api_content, tool_name, tool_calls, tool_call_id, timestamp, reasoning)
    extra_out: Output pro Call, den Hermes nicht als Text speichert (Denken), der aber im Verlauf bleibt.
    inject: Marker für Blöcke, die Plugins und Hermes an Nachrichten hängen (siehe markers()).
    Pro Call: (startzeit, abstand zum vorigen Call, prompt davor{}, prompt jetzt{}, output{}, [aufgerufene Komponenten]).
    Dazu Größen [(zeit, komponente, tokens)] von Tool-Rückgaben und Plugin-Einblendungen."""
    ctx, before, last, prev, calls, sizes, pending = defaultdict(float, prefix), {}, None, None, [], [], {}
    for role, content, api, tname, tcalls, tcid, ts, reasoning in msgs:
        if role == "assistant":
            t = prev if prev is not None else ts  # Call startet mit der Nachricht davor
            now = dict(ctx)
            out, keys = defaultdict(float), []
            out["reply"] += tok(content)
            out["think"] += tok(reasoning) + extra_out
            for tc in json.loads(tcalls or "[]"):
                name, args = tool_of(tc)
                k = key_of(name, args)
                pending[tc.get("id")] = k
                out[k] += tok(args)
                keys.append(k)
            for k, v in out.items():
                ctx[k] += v
            calls.append((t, t - last if last is not None else float("inf"), before, now, dict(out), keys))
            before, last = now, t
        elif role == "tool":
            k = pending.get(tcid) or "tool:" + (tname or "?")
            n = tok(content) + IMAGE_TOK * (content or "").count("[screenshot]")
            ctx[k] += n
            sizes.append((ts, k, n))
        else:  # user, session_meta
            ctx["user"] += tok(content) + IMAGE_TOK * (content or "").count("[Image attached")
            for label, seg in injections(content, api, inject):
                ctx["inj:" + label] += tok(seg)
                sizes.append((ts, "inj:" + label, tok(seg)))
        prev = ts
    return calls, sizes


def is_prefix(k):
    return k == "schema" or k.startswith("sys:")


def cache_split(before, now, gap, ttl, rho=None):
    """Was ein Call aus dem Cache las, was er neu schrieb und wie viel alter Prompt dabei verloren ging.

    rho: Anteil des vorigen Prompts, der laut agent.log wirklich aus dem Cache kam. Ohne Log gilt die Regel:
    nach mehr als `ttl` Sekunden Pause ist der Cache weg. Was der Cache hergibt, ist immer der Anfang des Prompts,
    also zuerst Systemprompt und Tools, dann der Verlauf."""
    cand = before or {k: v for k, v in now.items() if is_prefix(k)}
    if rho is None:
        rho = 1.0 if before and gap <= ttl else 0.0
    total, pre = sum(cand.values()), sum(v for k, v in cand.items() if is_prefix(k))
    keep = total if rho > 0.97 else rho * total  # kleine Abweichungen sind Messrauschen
    f = min(keep / pre, 1.0) if pre else 0.0
    g = min(max(keep - pre, 0.0) / (total - pre), 1.0) if total > pre else 0.0
    hit = {k: v * (f if is_prefix(k) else g) for k, v in cand.items()}
    new = {k: v - max(before.get(k, 0.0), hit.get(k, 0.0)) for k, v in now.items()
           if v - max(before.get(k, 0.0), hit.get(k, 0.0)) > 1e-9}
    lost = sum(max(v - hit.get(k, 0.0), 0.0) for k, v in before.items())
    return hit, new, lost


LOG_RX = re.compile(r"^(\S+ \S+),(\d+) \w+ \[([^\]]+)\] agent\.conversation_loop: API call #\d+: .*? in=(\d+) out=\d+ "
                    r".*?latency=[\d.]+s(?: cache=(\d+)/)?")
_LOGS = {}


def log_index():
    """Echte Werte pro API-Call aus Hermes' agent.log: {session: ([endzeit], [(input gesamt, davon aus dem Cache)])}.
    Rotierte Dateien werden nur einmal gelesen."""
    idx = defaultdict(list)
    for f in glob.glob(str(HERMES / "logs" / "agent.log*")):
        try:
            sig = (os.path.getmtime(f), os.path.getsize(f))
        except OSError:
            continue
        if _LOGS.get(f, (None,))[0] != sig:
            rows = []
            with (gzip.open if f.endswith(".gz") else open)(f, "rt", errors="replace") as fh:
                for line in fh:
                    m = "API call #" in line and LOG_RX.match(line)
                    if m:
                        t = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) + int(m.group(2)) / 1000
                        rows.append((m.group(3), t, int(m.group(4)), int(m.group(5) or 0)))
            _LOGS[f] = (sig, rows)
        for sid, t, i, r in _LOGS[f][1]:
            idx[sid].append((t, i, r))
    return {k: ([x[0] for x in sorted(v)], [x[1:] for x in sorted(v)]) for k, v in idx.items()}


def origin(src, title):
    if src == "cron":
        return "Cron: " + ((title or "").split(" · ")[0] or "?")
    return SOURCES.get(src, (src or "?").capitalize())


def floor(t, hourly):
    if hourly:
        return t - t % 3600
    return datetime.fromtimestamp(t).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def analyze(c, since, snap, ttl, sid=None, logs=None):
    """Verteilt die echten Kosten ab `since` (oder einer Session) auf Komponenten, Herkunft und Zeit.
    Invariante: sum(comp) == echte Kosten der Calls im Zeitraum."""
    logs = log_index() if logs is None else logs
    pm, im = markers()
    cond, arg = ("u.session_id = ?", sid) if sid else ("u.last_seen > ?", since)
    usage = defaultdict(list)
    for r in c.execute(f"""select u.session_id, u.model, coalesce(nullif(u.task, ''), 'chat'), u.api_call_count,
            u.input_tokens, u.cache_read_tokens, u.cache_write_tokens, u.output_tokens, coalesce(u.last_seen, 0)
            from session_model_usage u where {cond} and u.model like 'claude%'""", (arg,)):
        usage[r[0]].append(r[1:])
    ids = list(usage)
    q = ",".join("?" * len(ids))
    root = "git_repo_root" if "git_repo_root" in {r[1] for r in c.execute("pragma table_info(sessions)")} else "null"
    meta = {r[0]: r[1:] for r in c.execute(
        f"select id, source, title, system_prompt_hash, started_at, {root} from sessions where id in ({q})", ids)}
    prompts = dict(c.execute(f"""select hash, prompt from system_prompts where hash in
        (select system_prompt_hash from sessions where id in ({q}))""", ids))
    msgs = defaultdict(list)
    for r in c.execute(f"""select session_id, role, content, api_content, tool_name, tool_calls, tool_call_id,
            timestamp, reasoning from messages where session_id in ({q}) order by session_id, id""", ids):
        msgs[r[0]].append(r[1:])

    hourly = sid is None and since > time.time() - 2 * 86400
    comp, where = defaultdict(float), defaultdict(float)
    hours = defaultdict(lambda: defaultdict(float))       # Stundenbeginn -> Posten -> $
    hsteps, hsess = defaultdict(int), defaultdict(set)    # Stundenbeginn -> Schritte, Sessions
    models = defaultdict(lambda: [0.0, 0.0, 0.0])        # Calls, Tokens, $
    uses, sizes_acc = defaultdict(int), defaultdict(lambda: [0, 0.0])
    sess, steps = {}, []
    tot = dict(calls=0.0, tokens=0.0, long=0.0, writes=0.0, avoid=0.0, extra=0.0, n_steps=0, logged=0)
    repos, prx = home_repos(), project_rx()

    for s, rows in usage.items():
        src, title, hsh, started, repo_root = meta.get(s, ("?", None, None, 0, None))
        sc, n_calls = defaultdict(float), 0

        def put(k, v, t):
            sc[k] += v
            h = t - t % 3600
            hours[h][k] += v
            hsess[h].add(s)

        for m, task, n, i, r, w, o, seen in (x for x in rows if x[1] != "chat"):
            if seen > since:
                cost = sum(row_cost(m, i, r, w, o, ttl))
                put("task:" + task, cost, seen)
                models[m][0] += n; models[m][1] += i + r + w + o; models[m][2] += cost
                tot["calls"] += n; tot["tokens"] += i + r + w + o
        chat = [x for x in rows if x[1] == "chat"]
        if not chat:
            pass
        elif not msgs.get(s):
            seen = max(x[7] for x in chat)
            if seen > since:
                cost = sum(sum(row_cost(m, i, r, w, o, ttl)) for m, _, _, i, r, w, o, _ in chat)
                put("other", cost, seen)
                for m, _, n, i, r, w, o, _ in chat:
                    models[m][0] += n; models[m][1] += i + r + w + o; models[m][2] += sum(row_cost(m, i, r, w, o, ttl))
                    tot["calls"] += n; tot["tokens"] += i + r + w + o
        else:
            ci, cr, cw, co = (sum(x) for x in zip(*(row_cost(m, i, r, w, o, ttl) for m, _, _, i, r, w, o, _ in chat)))
            I, R, W, O = (sum(x) for x in zip(*((i, r, w, o) for _, _, _, i, r, w, o, _ in chat)))
            chat_cost = ci + cr + cw + co
            pre = prefix_for(prompts.get(hsh), snap, pm)
            calls, sizes = simulate(msgs[s], pre, inject=im)
            # Output, den Hermes nicht als Text speichert (Denken), steht trotzdem im Verlauf: zweiter Durchgang
            kt = (I + R + W) / (sum(sum(x[3].values()) for x in calls) or 1)  # echte Tokens pro Simulations-Token
            missing = O / kt - sum(sum(x[4].values()) for x in calls)
            if missing > 0 and calls:
                calls, sizes = simulate(msgs[s], pre, missing / len(calls), im)
                kt = (I + R + W) / (sum(sum(x[3].values()) for x in calls) or 1)
            # Echte Werte pro Call aus dem Log zuordnen (Endzeit des Calls = Zeitstempel der Antwort)
            lt, lv = logs.get(s, ([], []))
            ends = [m[6] for m in msgs[s] if m[0] == "assistant"]
            real = []
            for at in ends:
                i = bisect.bisect_left(lt, at - 3)
                real.append(lv[i] if i < len(lt) and abs(lt[i] - at) <= 3 else None)
            splits = []
            for j, (t, gap, before, now, out, keys) in enumerate(calls):
                rl, rho = real[j], None
                if rl:
                    prev_in = real[j - 1][0] if j and real[j - 1] else \
                        (sum(before.values()) or sum(v for k, v in now.items() if is_prefix(k))) * kt
                    rho = min(rl[1] / prev_in, 1.0) if prev_in else 0.0
                hit, new, lost = cache_split(before, now, gap, ttl, rho)
                splits.append((hit, new, lost, "rebuild" if gap > ttl else "break", rl is not None))
            # Echte Kosten pro Token-Art auf die simulierten Anteile verteilen
            SR = sum(sum(x[0].values()) for x in splits)
            SW = sum(sum(x[1].values()) + x[2] for x in splits)
            SO = sum(sum(x[4].values()) for x in calls)
            fr, fw = (cr / SR, (cw + ci) / SW) if SR and SW else (0.0, (cr + cw + ci) / (SR + SW or 1))
            fo = co / SO if SO else 0.0
            p_cost = 0.0
            for (t, gap, before, now, out, keys), (hit, new, lost, kind, logged) in zip(calls, splits):
                if t <= since:
                    continue
                parts = defaultdict(float)
                for k, v in hit.items():
                    parts[k] += v * fr
                for k, v in new.items():
                    parts[k] += v * fw
                parts[kind] += lost * fw
                for k, v in out.items():
                    parts[k] += v * fo
                if not SO:
                    parts["think"] += co / len(calls)
                cost = sum(parts.values())
                for k, v in parts.items():
                    put(k, v, t)
                p_cost += cost
                n_calls += 1
                hsteps[t - t % 3600] += 1
                tot["n_steps"] += 1; tot["logged"] += logged
                ctx_tok = sum(now.values()) * kt
                if ctx_tok > LONG_CTX:
                    tot["long"] += cost
                # für den Tipp zur Cache-Dauer: was 1 h statt 5 min (oder umgekehrt) geändert hätte
                tot["writes"] += (sum(new.values()) + lost) * fw
                if kind == "rebuild" and gap <= 3600:
                    tot["avoid"] += lost * fw
                if not lost and 300 < gap <= 3600:
                    tot["extra"] += sum(before.values()) * fw
                for k in keys:
                    uses[k] += 1
                if sid:
                    steps.append((t, ctx_tok, cost, lost * fw, kind if lost > 1e-9 else "", keys))
            for t, k, n in sizes:
                if t > since:
                    sizes_acc[k][0] += 1; sizes_acc[k][1] += n
            f = p_cost / chat_cost if chat_cost else 0.0  # Anteil der Session im Zeitraum
            for m, _, n, i, r, w, o, _ in chat:
                models[m][0] += n * f; models[m][1] += (i + r + w + o) * f; models[m][2] += sum(row_cost(m, i, r, w, o, ttl)) * f
                tot["calls"] += n * f; tot["tokens"] += (i + r + w + o) * f
        total_s = sum(sc.values())
        if total_s <= 0:
            continue
        for k, v in sc.items():
            comp["sub" if src == "subagent" and sid is None else k] += v
        org = origin(src, title)
        where[org] += total_s
        top = max(sc.items(), key=lambda x: x[1])[0]
        sess[s] = dict(title=title, src=src, origin=org, calls=n_calls, cost=total_s, top=top, started=started, comp=dict(sc),
                       project=project_of((m[4] for m in msgs.get(s, ()) if m[4]), repos, prx, repo_root))
    # 5 min -> 1 h: Pausen-Neuaufbauten bis 1 h fallen weg, alles Schreiben kostet 2/1,25 = 1,6x. Umgekehrt entsprechend.
    if ttl <= 300:
        ttl_save, ttl_alt = tot["avoid"] - (tot["writes"] - tot["avoid"]) * 0.6, 3600
    else:
        ttl_save, ttl_alt = tot["writes"] * 0.375 - tot["extra"] * 0.625, 300
    return dict(comp=comp, where=where, models=models, hours=hours, hsteps=hsteps, hsess=hsess, sess=sess, steps=steps,
                uses=uses, sizes=sizes_acc, total=sum(comp.values()), ttl=ttl, ttl_alt=ttl_alt, ttl_save=ttl_save,
                since=since, hourly=hourly, **tot)


# ---------- Projekte ----------
def home_repos(home=None):
    """Git-Repos direkt im Home-Ordner oder eine Ebene tiefer (z. B. ~/projects/app): {relativer Pfad: Name}."""
    home, out = Path.home() if home is None else Path(home), {}
    try:
        dirs = [d for d in home.iterdir() if not d.name.startswith(".") and d.is_dir()]
    except OSError:
        return out
    for d in dirs:
        try:
            if (d / ".git").exists():
                out[d.name] = d.name
            else:
                out.update({f"{d.name}/{x.name}": x.name for x in d.iterdir() if not x.name.startswith(".") and (x / ".git").exists()})
        except OSError:
            pass
    return out


def project_rx(home=None, hermes=None):
    """Pfade in Tool-Aufrufen: Hermes' eigener Ordner, /opt/x und /srv/x, alles unter ~ (als ~, $HOME oder ausgeschrieben)."""
    home, hermes = str(Path.home() if home is None else home), str(HERMES if hermes is None else hermes)
    h = rf"(?:{re.escape(home)}|~|\$HOME)"
    herm = re.escape(hermes) + (f"|{h}{re.escape(hermes[len(home):])}" if hermes.startswith(home + "/") else "")
    return re.compile(rf"(?P<herm>(?:{herm})(?![\w.-])(?P<junk>/(?:cache|sandboxes)\b)?)|/(?:opt|srv)/(?P<opt>[\w.-]+)"
                      rf"|{h}/(?P<home>(?!\.)[\w.-]+(?:/(?!\.)[\w.-]+)?)")


def project_of(texts, repos, rx, repo_root=None):
    """Projekt einer Session: ihr Git-Root, falls Hermes ihn kennt (nur im Terminal), sonst das Projekt, dessen Pfad in den
    Tool-Aufrufen am häufigsten vorkommt. Hermes' eigener Ordner zählt nur, wenn sonst kaum etwas vorkommt, weil fast jede
    Session dort nebenbei Skills oder Skripte anfasst."""
    if repo_root:
        return Path(repo_root).name
    n = Counter()
    for t in texts:
        for m in rx.finditer(t):
            if m.group("herm"):
                n[HERMES_PROJ] += not m.group("junk")
            elif m.group("opt"):
                n[m.group("opt")] += m.group("opt") not in OPT_SKIP
            else:
                p = m.group("home")
                name = repos.get(p) or repos.get(p.split("/")[0])
                if name:
                    n[name] += 1
    hermes = n.pop(HERMES_PROJ, 0)
    best = [x for x in n.most_common(1) if x[1]]
    # ponytail: Mehrheit der Pfade; eine Session über zwei Projekte zählt ganz zum häufigeren
    if best and best[0][1] >= (max(2, hermes / 4) if hermes else 1):
        return best[0][0]
    return HERMES_PROJ if hermes else None


def ranked(comp, group_skills=True):
    g = defaultdict(float)
    for k, v in comp.items():
        g["tool:skill_view" if group_skills and k.startswith("skill:") else k] += v
    return sorted(((k, v) for k, v in g.items() if v > 0), key=lambda x: -x[1])


def ttl_text(ttl):
    return "einer Stunde" if ttl > 300 else "5 Minuten"


def label(k, ttl=300):
    if k in FIXED:
        name, why = FIXED[k]
        return name, why.format(ttl=ttl_text(ttl))
    kind, _, name = k.partition(":")
    if kind == "tool":
        return "Tool: " + name, f"Was {name} zurückgibt, plus die Befehle dafür. Bleibt im Verlauf und wird bei jedem weiteren Schritt mitgelesen."
    if kind == "skill":
        return "Skill: " + name, "Skill-Inhalt, der nach dem Laden im Verlauf bleibt."
    if kind == "inj":
        return name, "Hängt sich an jede deiner Nachrichten und bleibt danach im Verlauf."
    if kind == "sys":
        return (name if name.startswith("Plugin") else "Systemprompt: " + name), "Fester Teil des Systemprompts, geht bei jedem Schritt mit."
    if kind == "task":
        return "Hintergrund: " + name, "Eigene Aufgabe von Hermes neben dem Chat."
    return k, ""


def tips(d, snap):
    tot, out = d["total"] or 1, []
    p = lambda v: pct(v, tot)
    save = d["ttl_save"]
    if save > 0.03 * tot:
        if d["ttl_alt"] > 300:
            out.append((save, "Cache eine Stunde halten",
                        "Mit <code>prompt_caching:\n  cache_ttl: 1h</code> in der config.yaml hält Anthropic den Zwischenspeicher eine Stunde statt "
                        "5 Minuten. Das Schreiben kostet dann das 1,6-Fache, dafür lösen Pausen unter einer Stunde keinen Neuaufbau mehr "
                        f"aus. Mit deinen echten Zeitabständen nachgerechnet: etwa {p(save)} weniger."))
        else:
            out.append((save, "Cache wieder 5 Minuten halten",
                        f"Bei deinen Zeitabständen wäre die 5-Minuten-Einstellung um etwa {p(save)} billiger als 1 Stunde."))
    br = d["comp"].get("break", 0)
    if br > 0.05 * tot:
        out.append((br * 0.9, "Cache-Brüche in Hermes beheben",
                    f"{p(br)} der Kosten entstanden, weil der Zwischenspeicher ohne Pause verloren ging und der Verlauf neu "
                    "geschrieben wurde. Das passiert in Hermes selbst, wenn sich zwischen zwei Schritten der Anfang des Prompts "
                    "ändert. Die Ursache dort zu finden wäre der größte Hebel, der nichts an deiner Nutzung ändert."))
    tools = snap.get("tools", {})
    used = {k.split(":", 1)[1] for k in d["uses"] if k.startswith("tool:")} | ({"skill_view"} if any(k.startswith("skill:") for k in d["uses"]) else set())
    st = sum(tools.values()) or 1
    unused = sorted(((n, d["comp"].get("schema", 0) * t / st) for n, t in tools.items() if n not in used), key=lambda x: -x[1])
    s = sum(v for _, v in unused)
    if s > 0.02 * tot:
        out.append((s, "Unbenutzte Tools abschalten",
                    f"{len(unused)} Tools wurden im Zeitraum kein einziges Mal benutzt, ihre Beschreibungen kosteten trotzdem {p(s)}. "
                    f"Am teuersten: {', '.join(f'{html.escape(n)} ({p(v)})' for n, v in unused[:4])}. Abschalten geht mit <code>hermes tools</code>."))
    if d["long"] > 0.2 * tot:
        out.append((d["long"] / 2, "Bei Themenwechsel neu anfangen",
                    f"{p(d['long'])} der Kosten entstanden in Schritten, die schon über 100.000 Tokens Verlauf mitlesen mussten. "
                    "Jeder Schritt liest den kompletten Verlauf erneut, deshalb wird eine Session mit der Zeit immer teurer. "
                    "<code>/new</code> bei einem neuen Thema hilft am meisten."))
    think = d["comp"].get("think", 0)
    effort = config_value("agent", "reasoning_effort")
    if think > 0.15 * tot and effort in ("xhigh", "max", "high"):
        out.append((think * 0.3, "Denken ist ein großer Posten",
                    f"Claudes Nachdenken macht {p(think)} aus. In der config.yaml steht <code>agent.reasoning_effort: {effort}</code>"
                    f"{', die höchste Stufe' if effort in ('xhigh', 'max') else ''}. Eine Stufe tiefer denkt Claude kürzer und "
                    "spart Output, kann aber bei schweren Aufgaben an Qualität verlieren."))
    bg = d["comp"].get("task:background_review", 0)
    if bg > 0.05 * tot:  # laut agent.log liest die Prüfung den Verlauf mit eigenem Prompt, also ohne Cache-Treffer
        out.append((bg, "Hintergrund-Prüfung seltener laufen lassen",
                    f"Die Hintergrund-Prüfung kostete {p(bg)}. Sie läuft mit eigenem Prompt und schreibt dabei den ganzen "
                    "bisherigen Verlauf neu in den Cache. Seltener läuft sie mit höherem <code>memory.nudge_interval</code> und "
                    "<code>skills.creation_nudge_interval</code>, ganz aus geht sie mit <code>auxiliary.background_review.enabled: false</code>."))
    tl = sorted(((k, v) for k, v in d["comp"].items() if k.startswith("tool:")), key=lambda x: -x[1])
    if tl and tl[0][1] > 0.08 * tot:
        name = tl[0][0][5:]
        out.append((tl[0][1] / 3, f"{name} ist das teuerste Tool",
                    TOOL_HINTS.get(name, f"Alles, was {html.escape(name)} zurückgibt, wird bei jedem weiteren Schritt erneut gelesen.")
                    + f" Anteil: {p(tl[0][1])}."))
    for org, v in sorted(d["where"].items(), key=lambda x: -x[1]):
        if org.startswith("Cron: ") and v > 0.05 * tot:
            out.append((v / 2, f"{org} ist teuer", f"Dieser Cron-Job kostete {p(v)}. Seltener laufen lassen oder seinen Prompt kürzen."))
    for k, v in d["comp"].items():
        if k.startswith("inj:") and v > 0.03 * tot:
            out.append((v, f"{k[4:]} hängt viel an", f"Was dieses Plugin an jede Nachricht hängt, kostete {p(v)}."))
    return sorted(out, key=lambda x: -x[0])


def limit_history(days=7):
    cut = time.time() - days * 86400
    try:
        return [h for h in map(json.loads, (DATA / "limits.jsonl").read_text().splitlines()) if h["t"] > cut]
    except (OSError, ValueError):
        return []


# ---------- Hintergrund: Limits + Snapshot ----------
LIMITS_PY = r'''
import json, urllib.request
try:
    from agent.anthropic_credentials import resolve_anthropic_token
except ImportError:  # Hermes vor Oktober 2026
    from agent.anthropic_adapter import resolve_anthropic_token
req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
    "Authorization": "Bearer " + (resolve_anthropic_token() or ""), "Accept": "application/json",
    "anthropic-beta": "oauth-2025-04-20", "User-Agent": "claude-code/2.1.0"})
print(urllib.request.urlopen(req, timeout=20).read().decode())
'''
STATE = {"limits": None, "at": 0.0, "ok": 0.0}  # at = letzter Versuch, ok = letzter erfolgreicher Abruf
LIMITS_LOCK = threading.Lock()


def refresh_limits(max_age):
    """Pro-Limits über Hermes' OAuth-Login holen (Token bleibt im Hermes-Prozess) und Verlauf mitschreiben."""
    with LIMITS_LOCK:
        if time.time() - STATE["at"] < max_age:
            return
        r = None
        try:
            r = subprocess.run([str(HERMES_PY), "-c", LIMITS_PY], cwd=HERMES / "hermes-agent",
                               capture_output=True, text=True, timeout=40)
            data = json.loads(r.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            print("limits: nicht abrufbar:", (r.stderr.strip().splitlines() or [""])[-1] if r else "", flush=True)
            STATE["at"] = time.time() - max(max_age - 60, 0)  # in einer Minute nochmal
            return
        STATE.update(limits=data, at=time.time(), ok=time.time())
        row = {"t": round(time.time())}
        for k in ("five_hour", "seven_day"):
            row[k] = (data.get(k) or {}).get("utilization")
        DATA.mkdir(parents=True, exist_ok=True)
        with open(DATA / "limits.jsonl", "a") as f:
            f.write(json.dumps(row) + "\n")


# ---------- Limits: Prognose + Warnungen ----------
WINDOWS = (("five_hour", "5-Stunden-Fenster", 5 * 3600), ("seven_day", "Woche", 7 * 86400),
           ("seven_day_opus", "Opus-Woche", 7 * 86400), ("seven_day_sonnet", "Sonnet-Woche", 7 * 86400))


def windows(L, now):
    """[(key, name, auslastung %, Anteil der Fensterzeit vorbei oder None, reset-zeit oder None, länge)]."""
    out = []
    for key, name, length in WINDOWS:
        w = (L or {}).get(key) or {}
        if w.get("utilization") is None:
            continue
        try:
            reset = datetime.fromisoformat(w["resets_at"]).timestamp()
        except (KeyError, TypeError, ValueError):
            reset = None
        frac = min(max(1 - (reset - now) / length, 0.0), 1.0) if reset else None
        out.append((key, name, float(w["utilization"]), frac, reset, length))
    return out


def week_forecast(u, frac):
    """Auslastung am Reset, linear aus dem Tempo seit Beginn des Fensters. Erst nach einem halben Tag aussagekräftig."""
    return u / frac if frac and frac >= 1 / 14 else None


def rate(hist, key, u, start, now):
    """Verbrauch in % pro Sekunde über die letzte Stunde, nur aus Messpunkten dieses Fensters; None ohne 10 min Daten."""
    pts = [h for h in hist if h["t"] >= max(start, now - 3600) and h.get(key) is not None]
    if not pts or now - pts[0]["t"] < 600:
        return None
    return max((u - pts[0][key]) / (now - pts[0]["t"]), 0.0)


def hm(t):
    return datetime.fromtimestamp(t).strftime("%H:%M")


def day_time(t):
    dt = datetime.fromtimestamp(t)
    return f"{WEEKDAYS[dt.weekday()]} {dt.strftime('%d.%m. %H:%M')}"


def de(v):
    return f"{v:.2f}".replace(".", ",")


def alerts(L, hist, sent, now):
    """Fällige Warnungen [(tag, titel, text)] und der neue Stand. Ein tag steht für ein Fenster; der Aufrufer trägt ihn
    nach erfolgreichem Senden in den Stand ein, damit jede Warnung höchstens einmal pro Fenster kommt."""
    sent, out, full = {k: v for k, v in sent.items() if k == "extra_used" or now - v < 8 * 86400}, [], []
    for key, name, u, frac, reset, length in windows(L, now):
        if u >= 100:
            full.append(name)
        if not reset or u >= 100 or f"{key}:{reset:.0f}" in sent:
            continue
        tag = f"{key}:{reset:.0f}"
        if length <= 5 * 3600:
            r = rate(hist, key, u, reset - length, now)
            fa = now + (100 - u) / r if r else None
            if fa and fa - now < 1800 and fa < reset:
                out.append((tag, f"{name} bald voll", f"{u:.0f} % verbraucht. Beim Tempo der letzten Stunde ist das Fenster "
                                                       f"gegen {hm(fa)} voll, der Reset kommt erst um {hm(reset)}."))
        else:
            fc = week_forecast(u, frac)
            if fc and fc > 100 and frac >= 1 / 7:
                start = reset - length
                out.append((tag, f"{name}: Limit reicht nicht", f"{u:.0f} % verbraucht, {frac * 100:.0f} % der Zeit sind um. "
                            f"Beim jetzigen Tempo ist es ca. {day_time(start + (now - start) * 100 / u)} voll, Reset {day_time(reset)}."))
    x = (L or {}).get("extra_usage") or {}
    used = x.get("used_credits")
    if x.get("is_enabled") and used is not None:
        prev = sent.get("extra_used")
        reset = next((w[4] for w in windows(L, now) if w[4]), 0)  # 5-Stunden-Fenster zuerst, sonst Woche
        tag = f"extra:{reset:.0f}"
        if prev is not None and used > prev and tag not in sent:  # Stand bleibt alt, bis die Warnung raus ist
            dp = 10 ** (x.get("decimal_places") or 0)
            out.append((tag, "Extra-Guthaben wird genutzt",
                        f"{de(used / dp)} von {de((x.get('monthly_limit') or 0) / dp)} {x.get('currency') or ''} verbraucht. "
                        + (f"Voll: {', '.join(full)}. " if full else "") + "Ab jetzt kostet jede Anfrage echtes Geld."))
        else:
            sent["extra_used"] = used
    return out, sent


def ntfy_target():
    """(server, topic, token) aus CLAUDE_USAGE_NTFY (volle URL) oder Hermes' ntfy-Einstellungen in .env, sonst None."""
    url, token = os.environ.get("CLAUDE_USAGE_NTFY", ""), None
    if not url:
        env = {}
        try:
            for line in (HERMES / ".env").read_text().splitlines():
                k, _, v = line.partition("=")
                if k.strip().startswith("NTFY_"):
                    env[k.strip()] = v.strip().strip("'\"")
        except OSError:
            pass
        if env.get("NTFY_HOME_CHANNEL"):
            url, token = f"{env.get('NTFY_SERVER_URL') or 'https://ntfy.sh'}/{env['NTFY_HOME_CHANNEL']}", env.get("NTFY_TOKEN")
    if not url:
        return None
    u = urlparse(url if "://" in url else "https://" + url)
    return f"{u.scheme}://{u.netloc}", u.path.strip("/"), token


def notify(title, text):
    t = ntfy_target()
    if not t:
        return False
    server, topic, token = t
    body = {"topic": topic, "title": title, "message": text, "priority": 4, **({"click": PUBLIC_URL} if PUBLIC_URL else {})}
    headers = {"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})}
    try:
        urllib.request.urlopen(urllib.request.Request(server, json.dumps(body).encode(), headers), timeout=15).read()
        return True
    except OSError as err:
        print("ntfy:", err, flush=True)
        return False


def check_alerts():
    f = DATA / "alerts.json"
    try:
        sent = json.loads(f.read_text())
    except (OSError, ValueError):
        sent = {}
    if not STATE["limits"]:
        return
    msgs, new = alerts(STATE["limits"], limit_history(), sent, time.time())
    for tag, title, text in msgs:
        if notify(title, text):
            new[tag] = time.time()
    if new != sent:
        DATA.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(new))


def load_snap():
    try:
        return json.loads((DATA / "snapshot.json").read_text())
    except (OSError, ValueError):
        return {}


def background():
    while True:
        snap = DATA / "snapshot.json"
        if not snap.exists() or time.time() - snap.stat().st_mtime > 86400:
            try:
                subprocess.run([str(HERMES_PY), str(Path(__file__).resolve()), "--snapshot"], cwd=HERMES / "hermes-agent",
                               capture_output=True, timeout=600)
            except (OSError, subprocess.TimeoutExpired) as e:
                print("snapshot:", e, flush=True)
        refresh_limits(540)
        check_alerts()
        time.sleep(600)


def snapshot():
    """Läuft im Hermes-venv: misst Systemprompt-Teile und Tool-Beschreibungen, wie Hermes sie gerade mitschickt.
    Baut dafür nur Hermes' Agent-Objekt zusammen und liest dessen Prompt und Tool-Liste; es geht keine Anfrage an ein
    Modell raus. Plattform ist die, von der die meisten Sessions kommen, weil die Tool-Auswahl davon abhängt."""
    import logging
    logging.disable(logging.WARNING)
    sys.path.insert(0, str(HERMES / "hermes-agent"))
    os.chdir(HERMES / "hermes-agent")
    from run_agent import AIAgent
    from agent.system_prompt import build_system_prompt
    platform = (connect().execute("""select source from sessions where source not in ('cron', 'subagent')
        group by source order by count(*) desc limit 1""").fetchone() or ("cli",))[0]
    agent = AIAgent(model=config_value("model", "default") or None, provider=config_value("model", "provider") or None,
                    platform=platform, quiet_mode=True)
    prompt = defaultdict(float)
    for label_, seg in segments(build_system_prompt(agent), markers()[0]):
        prompt[label_] += tok(seg)
    tools = {t["function"]["name"]: len(json.dumps(t)) / CPT for t in (agent.tools or [])}
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = DATA / "snapshot.json.tmp"
    tmp.write_text(json.dumps({"prompt": prompt, "tools": tools, "at": time.time()}))
    tmp.replace(DATA / "snapshot.json")


# ---------- HTML ----------
e = html.escape
STATIC = Path(__file__).resolve().parent / "static"
STATIC_FILES = {"style.css": "text/css; charset=utf-8", "manifest.webmanifest": "application/manifest+json",
                "icon.svg": "image/svg+xml", "icon-180.png": "image/png", "icon-192.png": "image/png", "icon-512.png": "image/png"}
ICONS = {  # 24er-Raster, Strich in currentColor
    "/": '<path d="M4 16a8 8 0 1 1 16 0"/><path d="M12 16l3.5-4.5"/>',
    "/verlauf": '<path d="M5 19v-7M10 19V6M15 19v-4M20 19V9"/>',
    "/details": '<path d="M9 7h11M9 12h11M9 17h11M4.5 7h.01M4.5 12h.01M4.5 17h.01"/>',
    "/sessions": '<path d="M20 14.5a2 2 0 0 1-2 2H8.5L4 20V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z"/>',
    "/projekte": '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.8l2 2h7.2a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
}
NAV = (("Überblick", (("/", "Übersicht"), ("/verlauf", "Verlauf"))),
       ("Aufschlüsselung", (("/details", "Details"), ("/sessions", "Sessions"), ("/projekte", "Projekte"))))
LOGO = ('<svg viewBox="0 0 24 24" aria-hidden="true"><path class="lt" d="M5 16a7 7 0 0 1 14 0"/>'
        '<path class="la" d="M5 16a7 7 0 0 1 10.5-6.06"/><path class="ln" d="M12 16l2.6-3"/></svg>')
COLORS = 5  # farbige Posten im gestapelten Verlauf, dazu "Rest"


def brk(s):
    """Escapen und lange Namen wie memory_tencentdb_conversation_search an _ und / umbrechbar machen."""
    return e(s).replace("_", "_<wbr>").replace("/", "/<wbr>")


def money(v):
    if 0 < v < 0.01:
        return "< 0,01 $"
    return f"{v:,.2f} $".replace(",", "§").replace(".", ",").replace("§", ".")


def pct(v, tot):
    return f"{v / (tot or 1) * 100:.1f} %".replace(".", ",")


def num(n):
    n = float(n or 0)
    if n >= 1e6:
        return f"{n / 1e6:.1f}".replace(".", ",") + " Mio."
    return f"{n / 1e3:.0f} Tsd." if n >= 1e3 else f"{n:.0f}"


def cnt(n):
    return f"{round(n or 0):,}".replace(",", ".")


def link(path, **q):
    q = urlencode({k: v for k, v in q.items() if v})
    return path + ("?" + q if q else "")


def chip(text, on, href):
    return f'<a href="{href}"{" class=on aria-current=true" if on else ""}>{text}</a>'


def group(k):
    return "tool:skill_view" if k.startswith("skill:") else k


def table(head, rows, cls=(), tcls=""):
    """cls: CSS-Klassen pro Spalte, l = linksbündig, o = auf dem Handy ausgeblendet."""
    c = lambda i: f' class="{cls[i]}"' if i < len(cls) and cls[i] else ""
    h = "".join(f"<th{c(i)}>{e(x)}</th>" for i, x in enumerate(head))
    b = "".join("<tr>" + "".join(f"<td{c(i)}>{x}</td>" for i, x in enumerate(r)) + "</tr>" for r in rows) \
        or f'<tr><td colspan="{len(head)}">Keine Daten im Zeitraum.</td></tr>'
    return f'<div class="scroll"><table{f" class={tcls}" if tcls else ""}><tr>{h}</tr>{b}</table></div>'


def bars(items, tot, ttl, explain=True, n=12, p=None, soft=False):
    if not items:
        return '<p class="hint">Keine Daten im Zeitraum.</p>'
    top = items[0][1] or 1
    out = ""
    for k, v in items[:n]:
        name, why = label(k, ttl) if ":" in k or k in FIXED else (k, "")
        out += (f'<div class="bar"><div class="row"><span>{brk(name)}</span><span class="v">{pct(v, tot)} · {money(v)}</span></div>'
                f'<div class="t{" soft" if soft else ""}"><i style="width:{v / top * 100:.1f}%"></i></div>'
                + (f'<div class="why">{e(why)}</div>' if explain and why and (k in FIXED or not k.startswith("tool:")) else "")
                + "</div>")  # der Satz zu Tools stünde sonst bei jedem Tool gleich da
    rest = sum(v for _, v in items[n:])
    if rest:
        more = f'<a href="{link("/details", p=p)}">Details</a>' if p else "Details"
        out += f'<p class="hint">Dazu {len(items) - n} kleinere Posten mit zusammen {pct(rest, tot)}, alle auf der Seite {more}.</p>'
    return out


def until(t):
    if not t:
        return ""
    s = t - time.time()
    if s < 3600:
        return f"Reset in {max(int(s // 60), 1)} Min."
    if s < 86400:
        return f"Reset in {int(s // 3600)} Std. {int(s % 3600 // 60)} Min."
    return "Reset " + day_time(t)


def meter(u, frac=None, hot=False):
    mark = (f'<b class="now" style="left:{frac * 100:.1f}%" title="{frac * 100:.0f} % der Zeit sind um"></b>'
            if frac is not None else "")
    return f'<div class="meter{" hot" if hot else ""}"><i style="width:{min(max(u, 0), 100):.1f}%"></i>{mark}</div>'


def limits_card(week_cost):
    refresh_limits(120)
    L, now = STATE["limits"], time.time()
    if not L:
        return ('<section class="card"><h2>Dein Claude-Limit</h2><p class="hint">Gerade nicht abrufbar. Die Anzeige braucht '
                "Hermes' Anmeldung über ein Claude-Abo (Pro oder Max), mit einem API-Schlüssel gibt es kein Limit.</p></section>")
    hist, left, right = limit_history(), "", ""
    for key, name, u, frac, reset, length in windows(L, now):
        start = reset - length if reset else now
        if key == "seven_day":
            fc = week_forecast(u, frac)
            if u >= 100:
                txt = f"Voll. {until(reset)}"
            elif fc is None:
                txt = f"Für eine Prognose ist die Woche noch zu jung. {until(reset)}"
            elif fc > 100:
                txt = (f"Beim jetzigen Tempo ist die Woche <strong>ca. {day_time(start + (now - start) * 100 / u)} voll</strong>. "
                       f"{until(reset)}")
            else:
                txt = (f"Beim jetzigen Tempo landet die Woche bei <strong>ca. {fc:.0f} %</strong>. "
                       f"{frac * 100:.0f} % der Zeit sind um. {until(reset)}")
            left = (f'<div class="lbl">Wochenlimit</div><div class="big">{u:.0f}<span> %</span></div>'
                    f'{meter(u, frac, u >= 80 or (fc or 0) > 100)}<p class="fc">{txt}</p>')
            if week_cost is not None:
                left += (f'<p class="why">Hermes hat in dieser Limit-Woche {money(week_cost)} API-Gegenwert verbraucht. '
                         "Das Limit zählt auch alles, was du außerhalb von Hermes mit Claude machst.</p>")
            continue
        hot = u >= 80
        if u >= 100:
            txt = f"Voll. {until(reset)}"
        elif length <= 5 * 3600:
            r = rate(hist, key, u, start, now) if reset else None
            fa = now + (100 - u) / r if r else None
            if fa and fa < reset:
                txt, hot = f"Beim Tempo der letzten Stunde voll ca. {hm(fa)}. {until(reset)}", hot or fa - now < 1800
            elif r is not None:
                txt = f"Reicht bis zum Reset um {hm(reset)}, dann ca. {min(u + r * (reset - now), 100):.0f} %."
            else:
                txt = until(reset)
        else:
            fc = week_forecast(u, frac)
            txt = (f"Prognose ca. {fc:.0f} %. " if fc else "") + until(reset)
        right += (f'<div class="lim"><div class="row"><span>{name}</span><b>{u:.0f} %</b></div>'
                  f'{meter(u, frac, hot)}<div class="why">{txt}</div></div>')
    x = L.get("extra_usage") or {}
    if x.get("is_enabled") and x.get("monthly_limit"):
        dp, used = 10 ** (x.get("decimal_places") or 0), x.get("used_credits") or 0
        right += (f'<div class="lim"><div class="row"><span>Extra-Guthaben</span><b>{de(used / dp)} von '
                  f'{de(x["monthly_limit"] / dp)} {e(x.get("currency") or "")}</b></div>{meter(used / x["monthly_limit"] * 100)}'
                  '<div class="why">Springt ein, wenn ein Limit voll ist, und kostet echtes Geld.</div></div>')
    spark = ""
    if len(hist) >= 3:
        t0, t1 = hist[0]["t"], hist[-1]["t"]
        line = lambda k: " ".join(f"{(h['t'] - t0) / ((t1 - t0) or 1) * 100:.2f},{100 - min(h.get(k) or 0, 100):.1f}" for h in hist)
        spark = (f'<div class="trend"><svg class="spark" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">'
                 f'<polyline class="f" points="{line("five_hour")}"/><polyline class="w" points="{line("seven_day")}"/></svg>'
                 f'<div class="axis"><span>Verlauf seit {day_time(t0)}</span>'
                 f'<span><span class="dot c0"></span>Woche<span class="dot f"></span>5 Std.</span></div></div>')
    left = left or '<div class="lbl">Wochenlimit</div><p class="hint">Keine Daten.</p>'
    stale = now - STATE["ok"] > 1800
    stamp = (f'<p class="why">{"<strong>Veraltet:</strong> " if stale else ""}Stand {day_time(STATE["ok"]) if stale else hm(STATE["ok"])}'
             f'{", der Abruf klappt gerade nicht." if stale else ", wird alle 10 Minuten abgefragt."}</p>')
    return (f'<section class="card hero" aria-label="Dein Claude-Limit"><div>{left}</div><div>{right}'
            f'{stamp}</div>{spark}</section>')


def layout(title, active, p, h1, sub, body, tabs=True, keep=None):
    """h1 und sub sind schon escaped. keep: Filter, die beim Wechsel des Zeitraums erhalten bleiben."""
    nav = "".join(f'<div class="grp">{g}</div>' + "".join(
        f'<a href="{link(href, p=p)}"{" class=on aria-current=page" if href == active else ""}>'
        f'<svg viewBox="0 0 24 24" aria-hidden="true">{ICONS[href]}</svg>{name}</a>' for href, name in items) for g, items in NAV)
    seg = "".join(chip(n, k == p, link(active, p=k, **(keep or {}))) for k, (_, n) in PERIODS.items())
    foot = f'Limit-Stand {hm(STATE["ok"]) if STATE["ok"] else "–"}<br>Warnungen per ntfy {"an" if ntfy_target() else "aus"}'
    try:
        v = int((STATIC / "style.css").stat().st_mtime)
    except OSError:
        v = 0
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>{e(title)}</title>
<link rel="stylesheet" href="/style.css?v={v}"><link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/icon-180.png"><link rel="manifest" href="/manifest.webmanifest">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Verbrauch">
<meta name="theme-color" content="#f7f3ec" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#1b1713" media="(prefers-color-scheme: dark)"></head>
<body><div class="app"><aside><a class="brand" href="{link("/", p=p)}"><span class="logo">{LOGO}</span><span>Claude-Verbrauch</span></a>
<nav aria-label="Seiten">{nav}</nav><div class="foot">{foot}</div></aside>
<main><header class="top"><div><h1>{h1}</h1><p class="sub">{sub}</p></div>{f'<nav class="seg" aria-label="Zeitraum">{seg}</nav>' if tabs else ""}</header>
{body}</main></div></body></html>"""


CACHE, LOCK = {}, threading.Lock()


def connect():
    return sqlite3.connect(f"file:{HERMES / 'state.db'}?mode=ro", uri=True, timeout=15)


def since_for(p):
    """(Beginn des Zeitraums, ob es wirklich die Limit-Woche ist)."""
    if p == "w":
        refresh_limits(120)
        try:
            return datetime.fromisoformat(STATE["limits"]["seven_day"]["resets_at"]).timestamp() - 7 * 86400, True
        except (KeyError, TypeError, ValueError):
            pass
    return time.time() - PERIODS[p][0] * 86400, False


def data_for(p):
    with LOCK:
        hit = CACHE.get(p)
        if hit and time.time() - hit[0] < 120:
            return hit[1]
        since, week = since_for(p)
        d = analyze(connect(), since, load_snap(), cache_ttl())
        d.update(week=week, p=p, at=time.time())
        CACHE[p] = (time.time(), d)
        return d


def period_text(d):
    if d["week"]:
        return "diese Limit-Woche seit " + day_time(d["since"])
    return {"1": "letzte 24 Stunden", "30": "letzte 30 Tage"}.get(d["p"], "letzte 7 Tage")


def window_sum(d, since):
    h0 = since - since % 3600
    return sum(sum(v.values()) for h, v in d["hours"].items() if h >= h0)


def per_day(d):
    """{Tagesbeginn: [{posten: $}, schritte, {sessions}]}"""
    out = defaultdict(lambda: [defaultdict(float), 0, set()])
    for h, comp in d["hours"].items():
        a = out[floor(h, False)]
        for k, v in comp.items():
            a[0][group(k)] += v
        a[1] += d["hsteps"].get(h, 0)
        a[2] |= d["hsess"].get(h, set())
    return out


def page_overview(p):
    d, d30, snap = data_for(p), data_for("30"), load_snap()
    w = d if p == "w" else data_for("w")
    tot, ttl, now = d["total"] or 1, d["ttl"], time.time()
    active = {floor(h, False) for h, v in d30["hours"].items() if sum(v.values()) > 0}
    sums = {"1": window_sum(d30, now - 86400), "7": window_sum(d30, now - 7 * 86400), "30": d30["total"]}
    kpis = [("main", PERIODS[p][1], money(d["total"]), f'{num(d["tokens"])} Tokens · {cnt(d["calls"])} Schritte')]
    kpis += [("", PERIODS[k][1], money(sums[k]), "API-Gegenwert") for k in [k for k in ("7", "30", "1") if k != p][:2]]
    kpis += [("", "Ø pro Tag", money(d30["total"] / max(len(active), 1)), f"{len(active)} aktive Tage von 30"),
             ("", "Sessions", cnt(len(d["sess"])), "im Zeitraum")]
    kpi_html = "".join(f'<div class="kpi {c}"><span>{l}</span><b>{v}</b><small>{s}</small></div>' for c, l, v, s in kpis)
    tip_html = "".join(f'<div class="tip"><b>{e(t)}</b><p>{txt}</p></div>' for _, t, txt in tips(d, snap)) \
        or '<p class="hint">Gerade nichts Auffälliges.</p>'
    where = sorted(d["where"].items(), key=lambda x: -x[1])
    models = sorted(d["models"].items(), key=lambda x: -x[1][2])
    models_html = '<ol class="rank">' + "".join(
        f'<li><span class="n">{i}</span><span class="m">{brk(m)}</span><span class="v">{money(x[2])}</span><b>{pct(x[2], tot)}</b></li>'
        for i, (m, x) in enumerate(models, 1)) + "</ol>"
    body = f"""{limits_card(w["total"] if w["week"] else None)}
<div class="kpis">{kpi_html}</div>
<div class="grid g2">
<section class="card"><h2>Was am meisten frisst</h2>
<p class="hint">Jeder Schritt schickt den kompletten bisherigen Verlauf an Claude. Was einmal drinsteht, auch jede Rückgabe eines
Tools, kostet deshalb bei jedem weiteren Schritt erneut. Prozent und Dollar sind auf Basis der API-Preise gerechnet, so wie Anthropic
auch dein Abo-Limit bemisst.</p>
{bars(ranked(d["comp"]), tot, ttl, n=10, p=p)}
<p class="hint">Für {d["logged"] / (d["n_steps"] or 1) * 100:.0f} % der Schritte lagen echte Cache-Werte pro Schritt aus Hermes' Log vor,
für den Rest ist der Cache nach der Pausen-Regel nachgerechnet.</p></section>
<div class="col"><section class="card"><h2>Wo es anfällt</h2>{bars(where, tot, ttl, explain=False, n=8, soft=True)}</section>
<section class="card"><h2>Modelle</h2>{models_html}</section></div></div>
<section class="sec"><h2>Was du sparen kannst</h2>
<p class="hint">Aus deinen echten Daten berechnet, die Prozente beziehen sich auf den gewählten Zeitraum.</p>
<div class="tips">{tip_html}</div></section>"""
    return layout("Claude-Verbrauch", "/", p, "Was frisst deine Claude-Usage?",
                  f"Hermes, {period_text(d)} · berechnet {hm(d['at'])}", body)


def stacked(d):
    """Gestapelte Balken pro Tag (bei kurzen Zeiträumen pro Stunde): die größten Posten farbig, alles andere als Rest."""
    hourly, ttl = d["hourly"], d["ttl"]
    top = [k for k, _ in ranked(d["comp"])[:COLORS]]
    cols = defaultdict(lambda: defaultdict(float))
    for h, comp in d["hours"].items():
        b = floor(h, hourly)
        for k, v in comp.items():
            cols[b][group(k) if group(k) in top else "rest"] += v
    keys, t, step = [], floor(d["since"], hourly), 3600 if hourly else 86400
    while t <= time.time():
        keys.append(t)
        t = floor(t + step + (0 if hourly else 7200), hourly)  # +2 h fängt Sommerzeit-Wechsel ab
    if not keys or not d["total"]:
        return '<p class="hint">Keine Daten im Zeitraum.</p>'
    names = [label(k, ttl)[0] for k in top] + ["Rest"]
    tl = lambda k: datetime.fromtimestamp(k).strftime("%H Uhr") if hourly else \
        f"{WEEKDAYS[datetime.fromtimestamp(k).weekday()]} {datetime.fromtimestamp(k).strftime('%d.%m.')}"
    sums = [sum(cols[k].values()) for k in keys]
    mx, w, svg = max(sums), 100 / len(keys), ""
    bw = min(w * .66, 4.5)  # bei wenigen Tagen keine Klötze
    for i, (k, s) in enumerate(zip(keys, sums)):
        y, rects, tip = 100.0, "", f"{tl(k)}: {money(s)}"
        for j, c in enumerate(top + ["rest"]):
            v = cols[k].get(c, 0.0)
            if v > 0:
                y -= v / mx * 96
                rects += f'<rect class="c{j}" x="{i * w + (w - bw) / 2:.2f}" y="{y:.2f}" width="{bw:.2f}" height="{v / mx * 96:.2f}"/>'
                tip += f"\n{names[j]}: {money(v)}"
        svg += f'<g><title>{e(tip)}</title><rect class="hit" x="{i * w:.2f}" y="0" width="{w:.2f}" height="100"/>{rects}</g>'
    peak = max(range(len(keys)), key=lambda i: sums[i])
    ax = [tl(keys[i]) for i in (0, len(keys) // 2, -1)]
    legend = "".join(f'<span><span class="dot c{j}"></span>{brk(n)}</span>' for j, n in enumerate(names))
    return (f'<svg class="chart" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" '
            f'aria-label="API-Gegenwert pro {"Stunde" if hourly else "Tag"}">{svg}</svg>'
            f'<div class="axis"><span>{ax[0]}</span><span>{ax[1]}</span><span>{ax[2]}</span></div>'
            f'<div class="lg">{legend}</div><p class="hint">Höchster Wert: {tl(keys[peak])} mit {money(sums[peak])}.</p>')


def heatmap(d):
    heat, wd_tot = defaultdict(float), defaultdict(float)
    for h, comp in d["hours"].items():
        lt = time.localtime(h)
        heat[(lt.tm_wday, lt.tm_hour)] += sum(comp.values())
        wd_tot[lt.tm_wday] += sum(comp.values())
    if not heat:
        return '<p class="hint">Keine Daten.</p>'
    mx, cells = max(heat.values()) or 1, ""
    for wd in range(7):
        cells += f"<span>{WEEKDAYS[wd]}</span>"
        for hr in range(24):
            v = heat.get((wd, hr), 0.0)
            lvl = 1 + min(int(math.sqrt(v / mx) * 4), 3) if v > 0 else 0  # Wurzel, damit kleine Werte nicht verschwinden
            cells += f'<i class="h{lvl}" title="{WEEKDAYS[wd]} {hr}–{hr + 1} Uhr: {money(v)}"></i>'
    cells += "<span></span>" + "".join(f'<span class="hx">{hr} Uhr</span>' for hr in (0, 6, 12, 18))
    (pw, ph), pv = max(heat.items(), key=lambda x: x[1])
    bw = max(wd_tot, key=wd_tot.get)
    scale = "".join(f'<i class="h{i}"></i>' for i in range(5))
    return (f'<div class="heat">{cells}</div><div class="scale">weniger{scale}mehr</div>'
            f'<p class="hint">Am meisten fiel {WEEKDAYS[pw]} zwischen {ph} und {ph + 1} Uhr an ({money(pv)}). '
            f'Teuerster Wochentag: {WEEKDAYS[bw]} mit {money(wd_tot[bw])}.</p>')


def page_history(p):
    d, d30 = data_for(p), data_for("30")
    tot, ttl = d["total"] or 1, d["ttl"]
    rows = []
    for t, (comp, steps, ss) in sorted(per_day(d).items(), reverse=True):
        c = sum(comp.values())
        if c > 0:
            dt = datetime.fromtimestamp(t)
            rows.append((f"{WEEKDAYS[dt.weekday()]} {dt.strftime('%d.%m.')}", money(c), pct(c, tot), cnt(steps), cnt(len(ss)),
                         f'<span class="why">{e(label(max(comp.items(), key=lambda x: x[1])[0], ttl)[0])}</span>'))
    body = f"""<section class="card"><div class="row"><h2>API-Gegenwert pro {"Stunde" if d["hourly"] else "Tag"}</h2>
<span class="v">{money(d["total"])}</span></div>
<p class="hint">Farbig sind die fünf größten Posten im Zeitraum, alles andere ist Rest. Die genauen Zahlen stehen unten.</p>
{stacked(d)}</section>
<section class="card"><h2>Wann es anfällt</h2>
<p class="hint">Letzte 30 Tage nach Wochentag und Uhrzeit, unabhängig vom gewählten Zeitraum.</p>{heatmap(d30)}</section>
<section class="sec"><h2>Tag für Tag</h2>
{table(["Tag", "Kosten", "Anteil", "Schritte", "Sessions", "Größter Posten"], rows, ("", "", "o", "", "o", "l o"))}</section>"""
    return layout("Verlauf · Claude-Verbrauch", "/verlauf", p, "Verlauf", f"Hermes, {period_text(d)}", body)


def page_details(p):
    d, snap = data_for(p), load_snap()
    tot, comp, uses, sizes = d["total"] or 1, d["comp"], d["uses"], d["sizes"]
    avg = lambda k: num(sizes[k][1] / sizes[k][0]) if sizes.get(k) and sizes[k][0] else "–"
    tools = sorted(((k, v) for k, v in ranked(comp) if k.startswith("tool:")), key=lambda x: -x[1])
    skills = sorted(((k, v) for k, v in comp.items() if k.startswith("skill:")), key=lambda x: -x[1])
    inj = sorted(((k, v) for k, v in comp.items() if k.startswith("inj:")), key=lambda x: -x[1])
    sysp = sorted(((k, v) for k, v in comp.items() if k.startswith("sys:")), key=lambda x: -x[1])
    tasks = sorted(((k, v) for k, v in comp.items() if k.startswith("task:") or k in ("sub", "other", "rebuild", "break", "think", "reply", "user")), key=lambda x: -x[1])
    st = sum(snap.get("tools", {}).values()) or 1
    schemas = sorted(snap.get("tools", {}).items(), key=lambda x: -x[1])
    skill_uses = sum(n for k, n in uses.items() if k.startswith("skill:"))
    trow = lambda k, v: (brk("skill_view (Skills laden)" if k == "tool:skill_view" else k[5:]),
                         cnt(skill_uses if k == "tool:skill_view" else uses.get(k, 0)),
                         "–" if k == "tool:skill_view" else avg(k), money(v), pct(v, tot))
    body = f"""<section><h2>Tools</h2>
<p class="hint">Rückgabe = Größe dessen, was das Tool im Schnitt zurückgibt (ca. Tokens). Kosten = Rückgabe und Befehle,
inklusive jedem späteren Wiederlesen.</p>
{table(["Tool", "Aufrufe", "Ø Rückgabe", "Kosten", "Anteil"], [trow(k, v) for k, v in tools], ("", "", "", "", "o"))}</section>
<section class="sec"><h2>Skills</h2>
{table(["Skill", "geladen", "Ø Größe", "Kosten", "Anteil"], [(brk(k[6:]), cnt(uses.get(k, 0)), avg(k), money(v), pct(v, tot)) for k, v in skills], ("", "", "", "", "o"))}</section>
<section class="sec"><h2>Plugins an deinen Nachrichten</h2>
{table(["Plugin", "Nachrichten", "Ø Tokens", "Kosten", "Anteil"], [(brk(k[4:]), cnt(sizes.get(k, [0])[0]), avg(k), money(v), pct(v, tot)) for k, v in inj], ("", "", "", "", "o"))}</section>
<section class="sec"><h2>Systemprompt</h2>
<p class="hint">Geht bei jedem Schritt mit. Größe pro Schritt laut Messung vom {datetime.fromtimestamp(snap.get("at", 0)).strftime("%d.%m. %H:%M")}.</p>
{table(["Teil", "Tokens pro Schritt", "Kosten", "Anteil"], [(e(k[4:]), num(snap.get("prompt", {}).get(k[4:], 0)), money(v), pct(v, tot)) for k, v in sysp]
       + [("Tool-Beschreibungen (alle)", num(st), money(comp.get("schema", 0)), pct(comp.get("schema", 0), tot))], ("", "", "", "o"))}</section>
<section class="sec"><h2>Tool-Beschreibungen einzeln</h2>
<p class="hint">Kosten anteilig nach Größe. Ein Tool mit 0 Aufrufen kostet hier trotzdem bei jedem Schritt.</p>
{table(["Tool", "Tokens pro Schritt", "Aufrufe", "Kosten", "Anteil"],
       [(brk(n), num(t), cnt(uses.get("tool:" + n, 0) if n != "skill_view" else skill_uses), money(comp.get("schema", 0) * t / st), pct(comp.get("schema", 0) * t / st, tot)) for n, t in schemas], ("", "", "", "", "o"))}</section>
<section class="sec"><h2>Verlauf, Denken und Hintergrund</h2>
{table(["Posten", "Kosten", "Anteil"], [(e(label(k, d["ttl"])[0]), money(v), pct(v, tot)) for k, v in tasks])}</section>
<section class="sec"><h2>So wird gerechnet</h2>
<p class="hint">Hermes speichert pro Session die echten Token-Zahlen von Anthropic: frischer Input, aus dem Cache gelesen, in den Cache
geschrieben, Output. Daraus und aus der offiziellen Preisliste ergibt sich der exakte API-Gegenwert jeder Session. Für die Aufteilung wird
jede Session Schritt für Schritt nachgespielt: was zu dem Zeitpunkt im Verlauf stand, was neu dazukam und ob der Cache nach einer Pause
von mehr als {ttl_text(d["ttl"])} verfallen war. Wo Hermes' agent.log für einen Schritt echte Werte hat (Input gesamt und davon aus dem Cache),
zählt der echte Wert, so werden auch Cache-Brüche ohne Pause sichtbar. Das galt im Zeitraum für
{d["logged"] / (d["n_steps"] or 1) * 100:.0f} % der Schritte. Die Größen kommen aus der Textlänge, Bilder zählen pauschal, und alles wird pro
Session auf die echten Zahlen geeicht. Summen und Gesamtkosten sind exakt, die Aufteilung auf einzelne Posten ist eine gute Näherung.</p></section>"""
    return layout("Details · Claude-Verbrauch", "/details", p, "Details", f"Hermes, {period_text(d)} · alle Posten einzeln", body)


def cron_jobs(d, p):
    try:
        jobs = json.loads((HERMES / "cron" / "jobs.json").read_text())
        jobs = jobs.get("jobs", []) if isinstance(jobs, dict) else jobs
        sched = {j.get("name"): j.get("schedule_display") or (j.get("schedule") or {}).get("display") or "" for j in jobs}
    except (OSError, ValueError, AttributeError):
        sched = {}
    days = max((time.time() - d["since"]) / 86400, 1 / 24)
    g = defaultdict(lambda: [0, 0.0])
    for x in d["sess"].values():
        if x["src"] == "cron":
            a = g[x["origin"][len("Cron: "):]]
            a[0] += 1; a[1] += x["cost"]
    rows = sorted(g.items(), key=lambda kv: -kv[1][1])
    s = sum(c for _, (_, c) in rows)
    return (f'<p class="hint">{len(rows)} Cron-Jobs mit Kosten im Zeitraum, zusammen {money(s)} ({pct(s, d["total"])} vom Zeitraum). '
            f"Hochgerechnet auf eine Woche: {money(s / days * 7)}. Jobs, die nur ein Skript ohne KI starten, kosten nichts und fehlen hier.</p>"
            + table(["Cron-Job", "Zeitplan", "Läufe", "Ø pro Lauf", "Kosten", "pro Woche"],
                    [(f'<a href="{link("/sessions", p=p, src="cron", q=n)}">{e(n)}</a>',
                      f"<code>{e(sched[n])}</code>" if sched.get(n) else "–", cnt(r), money(c / r), money(c), money(c / days * 7))
                     for n, (r, c) in rows], ("", "l o", "", "o", "", "")))


def page_sessions(p, q):
    d = data_for(p)
    tot = d["total"] or 1
    arg = lambda k: (q.get(k) or [""])[0].strip()
    view, src, proj, term = arg("view"), arg("src"), arg("proj"), arg("q")
    tabs = ('<nav class="chips tabs" aria-label="Ansicht">' + chip("Alle Sessions", view != "cron", link("/sessions", p=p))
            + chip("Cron-Jobs", view == "cron", link("/sessions", p=p, view="cron")) + "</nav>")
    sub = f"Hermes, {period_text(d)}"
    if view == "cron":
        return layout("Cron-Jobs · Claude-Verbrauch", "/sessions", p, "Sessions", sub, tabs + cron_jobs(d, p), keep={"view": "cron"})
    sel = [(s, x) for s, x in d["sess"].items() if (not proj or (x["project"] or NO_PROJ) == proj)
           and (not term or term.lower() in (x["title"] or "").lower())]
    counts = Counter(x["src"] for _, x in sel)
    rows = sorted(((s, x) for s, x in sel if not src or x["src"] == src), key=lambda r: -r[1]["cost"])
    chips = chip("Alle", not src, link("/sessions", p=p, proj=proj, q=term)) + "".join(
        chip(f'{e(SOURCES.get(k, (k or "?").capitalize()))}<span>{n}</span>', k == src, link("/sessions", p=p, src=k, proj=proj, q=term))
        for k, n in counts.most_common())
    hidden = "".join(f'<input type="hidden" name="{k}" value="{e(v)}">' for k, v in (("p", p), ("src", src), ("proj", proj)) if v)
    search = (f'<form class="search" action="/sessions" role="search">{hidden}<input type="search" name="q" value="{e(term)}" '
              'placeholder="Titel durchsuchen" aria-label="Titel durchsuchen"></form>')
    flt = f'<div class="filters"><nav class="chips" aria-label="Herkunft">{chips}</nav>{search}</div>'
    if proj:
        flt += f'<p class="hint">Nur Projekt <b>{e(proj)}</b> · <a href="{link("/sessions", p=p, src=src, q=term)}">Filter entfernen</a></p>'
    s_cost = sum(x["cost"] for _, x in rows)
    trs = [(f'<a href="/s/{quote(s)}?p={p}">{e((x["title"] or "ohne Titel")[:80])}</a>', e(x["origin"]),
            e(x["project"] or "–"), cnt(x["calls"]), money(x["cost"]), pct(x["cost"], tot),
            f'<span class="why">{e(label(x["top"], d["ttl"])[0])}</span>') for s, x in rows[:200]]
    body = f"""{tabs}{flt}
<p class="hint">{cnt(len(rows))} Sessions, zusammen {money(s_cost)} ({pct(s_cost, tot)} vom Zeitraum). Die teuersten zuerst, antippen zeigt
die Aufschlüsselung Schritt für Schritt.</p>
{table(["Session", "Herkunft", "Projekt", "Schritte", "Kosten", "Anteil", "Größter Posten"], trs, ("", "l o", "l o", "", "", "o", "l o"), "titles")}"""
    return layout("Sessions · Claude-Verbrauch", "/sessions", p, "Sessions", sub, body, keep={"src": src, "proj": proj, "q": term})


def page_projects(p):
    d = data_for(p)
    tot = d["total"] or 1
    g = defaultdict(lambda: [0, 0, 0.0, defaultdict(float)])
    for x in d["sess"].values():
        a = g[x["project"] or NO_PROJ]
        a[0] += 1; a[1] += x["calls"]; a[2] += x["cost"]
        for k, v in x["comp"].items():
            a[3][group(k)] += v
    rows = sorted(g.items(), key=lambda kv: -kv[1][2])
    top = rows[0][1][2] if rows else 1
    trs = [(f'<a href="{link("/sessions", p=p, proj=n)}">{e(n)}</a><div class="t{" soft" if n in (NO_PROJ, HERMES_PROJ) else ""}">'
            f'<i style="width:{c / (top or 1) * 100:.1f}%"></i></div>', cnt(s), cnt(st), money(c), pct(c, tot),
            f'<span class="why">{e(label(max(comp.items(), key=lambda x: x[1])[0], d["ttl"])[0]) if comp else ""}</span>')
           for n, (s, st, c, comp) in rows]
    body = f"""<p class="hint">Hermes speichert bei Sessions aus Telegram, Discord und Cron keinen Arbeitsordner. Jede Session zählt deshalb
zu dem Projekt, dessen Pfad in ihren Tool-Aufrufen am häufigsten vorkommt: Ordner unter /opt und /srv sowie Git-Repos im Home-Ordner.
„{HERMES_PROJ}“ ist Arbeit an Hermes' eigenen Dateien, wenn sonst kein Projekt vorkommt. Eine Session über zwei Projekte zählt ganz
zum häufigeren.</p>
{table(["Projekt", "Sessions", "Schritte", "Kosten", "Anteil", "Größter Posten"], trs, ("", "", "o", "", "", "l o"), "titles")}"""
    return layout("Projekte · Claude-Verbrauch", "/projekte", p, "Projekte",
                  f"Hermes, {period_text(d)} · antippen zeigt die Sessions", body)


def page_session(sid, p):
    ttl, c = cache_ttl(), connect()
    d = analyze(c, 0, load_snap(), ttl, sid=sid)
    if not d["sess"]:
        return None
    x = d["sess"][sid]
    tot = d["total"] or 1
    rb = [s for s in d["steps"] if s[4]]
    names = lambda keys: ", ".join(f"{label(k)[0].split(': ', 1)[-1]}{f' ×{keys.count(k)}' if keys.count(k) > 1 else ''}" for k in dict.fromkeys(keys))
    days = {datetime.fromtimestamp(s[0]).date() for s in d["steps"]}
    fmt = "%H:%M" if len(days) == 1 else "%d.%m. %H:%M"
    steps = [(datetime.fromtimestamp(t).strftime(fmt), num(ctx), money(cost),
              f'{money(r)} <span class="why">{"nach Pause" if kind == "rebuild" else "ohne Pause"}</span>' if kind else "",
              f'<span class="why">{brk(names(keys))}</span>') for t, ctx, cost, r, kind, keys in d["steps"]]
    sub = (f'<a href="{link("/sessions", p=p)}">← Sessions</a> · {e(x["origin"])} · Projekt {e(x["project"] or NO_PROJ)} · '
           f'gestartet {datetime.fromtimestamp(x["started"] or 0).strftime("%d.%m.%Y %H:%M")} · {cnt(x["calls"])} Schritte · {money(x["cost"])}')
    body = f"""<section class="card"><h2>Was in dieser Session gefressen hat</h2>
{bars(ranked(d["comp"], group_skills=False), tot, ttl, explain=False, n=40)}</section>
<section class="sec"><h2>Schritt für Schritt</h2>
<p class="hint">Verlauf = wie viele Tokens dieser Schritt mitlesen musste. Neuaufbau = was es kostete, den Verlauf neu in den Cache zu
schreiben, weil er nach einer Pause verfallen oder ohne Pause verloren gegangen war ({len(rb)} Mal, zusammen {money(sum(s[3] for s in rb))}).
{f"Für {d['logged'] / d['n_steps'] * 100:.0f} % der Schritte lagen echte Werte aus Hermes' Log vor." if d["n_steps"] else ""}</p>
{table(["Zeit", "Verlauf", "Kosten", "Neuaufbau", "Tools"], steps, ("", "", "", "", "l"))}</section>"""
    return layout(f"{x['title'] or 'Session'} · Claude-Verbrauch", "/sessions", p, e(x["title"] or "ohne Titel"), sub, body, tabs=False)


class Handler(BaseHTTPRequestHandler):
    def send(self, b, ctype, cache="no-cache"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = q.get("p", [DEFAULT_P])[0]
        p = p if p in PERIODS else DEFAULT_P
        name = u.path.lstrip("/")
        if name in STATIC_FILES:
            try:
                return self.send((STATIC / name).read_bytes(), STATIC_FILES[name], "max-age=86400")
            except OSError:
                return self.send_error(404)
        pages = {"/": page_overview, "/verlauf": page_history, "/details": page_details, "/projekte": page_projects}
        try:
            if u.path in pages:
                body = pages[u.path](p)
            elif u.path == "/sessions":
                body = page_sessions(p, q)
            elif u.path.startswith("/s/"):
                body = page_session(unquote(u.path[3:]), p)
            elif u.path == "/health":
                body = "ok"
            else:
                body = None
        except Exception:
            traceback.print_exc()
            return self.send_error(500)
        if body is None:
            return self.send_error(404)
        self.send(body.encode(), "text/html; charset=utf-8")

    def log_message(self, *a):
        pass


def selftest():
    cfg = ("model:\n  default: m\n  provider: anthropic\nmemory:\n\n  provider: memory_tencentdb\n"
           "plugins:\n  enabled:\n    - ponytail\n    - 'superpowers'\n    - platforms/ntfy\n  disabled: []\n")
    assert config_value("model", "provider", cfg) == "anthropic" and config_value("memory", "provider", cfg) == "memory_tencentdb"
    assert plugin_names(cfg) == ["ponytail", "superpowers", "memory_tencentdb"], plugin_names(cfg)
    pm, im = markers(plugin_names(cfg))
    s = segments("hallo\n\nPONYTAIL MODE ACTIVE x\n<EXTREMELY_IMPORTANT>\nsuperpowers y\n[Note: Modell gewechselt]", im)
    assert [l for l, _ in s] == ["Sonstige Einblendungen", "Plugin: ponytail", "Plugin: superpowers", "Hermes-Hinweise"], s
    assert segments("ich\n# memory-tencentdb\nx", pm)[-1][0] == "Plugin: memory_tencentdb"   # - und _ gleichwertig
    assert price("claude-opus-5-5")[0] == 4e-6 and price("claude-opus-4-8")[4] == 25e-6 and price("claude-opus-4-1-x")[0] == 15e-6
    tc = json.dumps([{"id": "a", "function": {"name": "terminal", "arguments": '{"command": "ls"}'}}])
    msgs = [("user", "hi " * 50, None, None, None, None, 0.0, None),
            ("assistant", "", None, None, tc, None, 1.0, "denke " * 20),
            ("tool", "x" * 700, None, "terminal", None, "a", 2.0, None),
            ("assistant", "fertig", None, None, None, None, 3.0, None),
            ("user", "weiter", None, None, None, None, 1000.0, None),
            ("assistant", "ok", None, None, None, None, 1001.0, None)]
    calls, _ = simulate(msgs, {"schema": 100.0})
    splits = [cache_split(b, n, g, 300) for _, g, b, n, _, _ in calls]
    assert [x[2] > 0 for x in splits] == [False, False, True], splits     # Pause > 5 min: Neuaufbau
    assert "tool:terminal" in splits[1][1] and splits[1][0]["schema"] == 100  # Rückgabe neu geschrieben, Präfix gelesen
    assert not cache_split(calls[2][2], calls[2][3], calls[2][1], 3600)[2]    # mit 1 h kein Neuaufbau
    hit, new, lost = cache_split({"schema": 100, "user": 100}, {"schema": 100, "user": 150}, 1, 300, rho=0.5)
    assert hit == {"schema": 100, "user": 0} and new == {"user": 50} and lost == 100  # Log: Cache gibt den Anfang her
    db = sqlite3.connect(":memory:")
    db.executescript("""create table sessions(id, source, title, system_prompt_hash, started_at);
        create table system_prompts(hash, prompt);
        create table messages(id integer primary key, session_id, role, content, api_content, tool_name, tool_calls,
            tool_call_id, timestamp, reasoning);
        create table session_model_usage(session_id, model, task, api_call_count, input_tokens, cache_read_tokens,
            cache_write_tokens, output_tokens, last_seen);
        insert into sessions values('s', 'telegram', 'Test', null, 0);
        insert into session_model_usage values('s', 'claude-opus-5-5', '', 3, 10, 5000, 3000, 400, 1001);
        insert into session_model_usage values('s', 'claude-opus-5-5', 'background_review', 1, 0, 2000, 500, 100, 1001);""")
    db.executemany("""insert into messages(session_id, role, content, api_content, tool_name, tool_calls, tool_call_id,
        timestamp, reasoning) values('s', ?, ?, ?, ?, ?, ?, ?, ?)""", msgs)
    d = analyze(db, -1, {"tools": {"terminal": 100.0}}, 300, logs={})
    real = sum(sum(row_cost("claude-opus-5-5", *r, 300)) for r in [(10, 5000, 3000, 400), (0, 2000, 500, 100)])
    assert abs(d["total"] - real) < 1e-12, (d["total"], real)               # nichts geht verloren, nichts kommt dazu
    assert d["comp"]["rebuild"] > 0 and d["comp"]["task:background_review"] > 0 and d["comp"]["tool:terminal"] > 0
    late = analyze(db, 999, {"tools": {"terminal": 100.0}}, 300, logs={})    # Zeitraum schneidet nach Call-Zeit
    assert 0 < late["total"] < d["total"] and late["comp"]["rebuild"] > 0 and "tool:terminal" not in late["uses"]
    logged = analyze(db, -1, {"tools": {"terminal": 100.0}}, 300, logs={"s": ([1.0, 3.0, 1001.0], [(5000, 0), (6000, 0), (6100, 0)])})
    assert logged["comp"]["break"] > 0 and abs(logged["total"] - real) < 1e-12 and logged["logged"] == 3  # Bruch ohne Pause aus dem Log
    assert abs(sum(sum(v.values()) for v in d["hours"].values()) - d["total"]) < 1e-12        # Verlauf summiert sich auf
    assert sum(d["hsteps"].values()) == d["n_steps"] == 3 and d["sess"]["s"]["project"] is None
    # Projekte: Mehrheit der Pfade, Hermes' eigener Ordner nur ohne anderes Projekt, Systemordner zählen nicht
    rx, repos = project_rx("/h/u", "/h/u/.hermes"), {"code/app": "app", "tool": "tool"}
    assert project_of(['{"command": "cat ~/.hermes/x; cd /opt/shop && ls /opt/shop/src"}'], repos, rx) == "shop"
    assert project_of(["~/.hermes/skills/a", "/h/u/.hermes/b"], repos, rx) == HERMES_PROJ
    assert project_of(["/opt/homebrew/bin/x", "~/.hermes/cache/y", "~/Downloads/z"], repos, rx) is None
    assert project_of(["/h/u/code/app/main.py", "$HOME/tool/x"], repos, rx) in ("app", "tool")
    assert project_of([], repos, rx, "/srv/git/werk") == "werk"
    assert project_of(["~/.hermes/a"] * 12 + ["/opt/shop"] * 2, repos, rx) == HERMES_PROJ  # Nebenbei-Erwähnung zählt nicht
    # Prognose: linear seit Wochenbeginn, 5-Stunden-Tempo nur aus Messpunkten des laufenden Fensters
    assert abs(week_forecast(34, 0.47) - 72.34) < 0.01 and week_forecast(5, 0.05) is None
    hist = [{"t": 1000, "five_hour": 40}, {"t": 2800, "five_hour": 50}]
    assert abs(rate(hist, "five_hour", 60, 0, 4600) - 20 / 3600) < 1e-12 and rate(hist, "five_hour", 60, 3000, 4600) is None
    # Warnungen: einmal pro Fenster, Extra-Guthaben erst ab dem zweiten Stand und nur bei Anstieg
    now = 1_800_000_000.0
    iso = lambda t: datetime.fromtimestamp(t).astimezone().isoformat()
    L = {"five_hour": {"utilization": 80, "resets_at": iso(now + 7200)}, "seven_day": {"utilization": 60, "resets_at": iso(now + 3.5 * 86400)},
         "extra_usage": {"is_enabled": True, "used_credits": 100, "monthly_limit": 2500, "decimal_places": 2, "currency": "EUR"}}
    hist = [{"t": now - 3000, "five_hour": 40}]                   # 40 % pro 50 min: voll in 25 min, Woche landet bei 120 %
    msgs, st = alerts(L, hist, {}, now)
    assert sorted(t.split(":")[0] for t, _, _ in msgs) == ["five_hour", "seven_day"] and st["extra_used"] == 100, msgs
    sent = {**st, **{t: now for t, _, _ in msgs}}
    assert alerts(L, hist, sent, now + 60)[0] == []
    L["extra_usage"]["used_credits"] = 150
    msgs, st = alerts(L, hist, sent, now + 60)
    assert [t.split(":")[0] for t, _, _ in msgs] == ["extra"] and st["extra_used"] == 100  # Stand erst nach dem Senden
    assert alerts(L, hist, {**st, msgs[0][0]: now}, now + 120) == ([], {**st, msgs[0][0]: now, "extra_used": 150})
    print("selftest ok")


if __name__ == "__main__":
    if "--test" in sys.argv:
        selftest()
    elif "--ntfy-test" in sys.argv:
        print("gesendet" if notify("Test von claude-usage", "So sehen Warnungen zu deinem Claude-Limit aus.")
              else "nicht gesendet, ntfy ist nicht eingerichtet oder nicht erreichbar")
    elif "--snapshot" in sys.argv:
        snapshot()
        sys.stdout.flush()
        os._exit(0)  # AIAgent kann Hintergrund-Threads (MCP) offen lassen
    else:
        threading.Thread(target=background, daemon=True).start()
        print(f"claude-usage auf http://127.0.0.1:{PORT}", flush=True)
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
