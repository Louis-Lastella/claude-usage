#!/usr/bin/env python3
"""Usagecast: see what eats your AI usage.

Reads Hermes Agent's ~/.hermes/state.db read-only. For every session the real token counts Anthropic reported
(session_model_usage, priced with the API prices below) are split call by call over what was in the context at that
moment: tools, skills, plugins, system prompt parts, thinking, cache rebuilds after pauses.

  python3 app.py                          server on 127.0.0.1:7682 (env: PORT, HERMES_HOME, USAGECAST_*)
  python3 app.py --test                   self-test
  python3 app.py --ntfy-test              send a test alert
  <hermes-venv>/python app.py --snapshot  measure system prompt parts and tool schemas (the server does this daily)
"""
import bisect, glob, gzip, hmac, html, json, math, os, random, re, secrets, shutil, sqlite3, statistics, subprocess, sys, tempfile, threading, time, traceback, urllib.error, urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

ROOT = Path(__file__).resolve().parent
# USAGECAST_ROOT: snapshot() sets it before pointing HERMES_HOME at a profile; Hermes re-runs this script on import
HERMES = Path(os.environ.get("USAGECAST_ROOT") or os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
HERMES_PY = HERMES / "hermes-agent" / "venv" / "bin" / "python"
DATA = Path(os.environ.get("USAGECAST_DATA", ROOT / "data"))
PORT = int(os.environ.get("PORT", "7682"))
PUBLIC_URL = os.environ.get("USAGECAST_URL", "")   # dashboard address, opened when an alert is tapped
REPO_URL = "https://github.com/Louis-Lastella/usagecast"
VERSION = (ROOT / "VERSION").read_text().strip() if (ROOT / "VERSION").is_file() else "dev"
CPT = 3.5            # characters per token, only for proportions; amounts always come from the real token counts
IMAGE_TOK = 3000     # one image in the history, same units (agent.log shows ~3,900 real tokens)
LONG_CTX = 100_000   # a step that has to read more history than this counts as expensive
PERIODS = {"w": 7, "1": 1, "7": 7, "30": 30}   # days; "w" = since the weekly limit reset, 7 days without limit data
DEFAULT_P = "w"

# $ per million tokens: input, cache write 5 min, cache write 1 h, cache read, output.
# Source: platform.claude.com/docs/en/about-claude/pricing, as of 2026-10-08. The longest matching prefix wins.
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

# Fixed sections Hermes builds into every system prompt; each runs until the next marker found.
# SOUL.md, plugins and the memory provider are added per installation, see markers(). Ids are shown as seg.<id>.
HERMES_MARKERS = [
    ("base", r"^You run on |^# Finishing the job"),
    ("computer_use", r"^# Computer Use"),
    ("base", r"^Host: "),
    ("skills", r"^## Skills \(mandatory\)"),
    ("memory", r"^═+\nMEMORY"),
    ("base", r"^Conversation started:"),
]
# Components with their own name and explanation in the locale files (comp.<key>, comp.<key>.why)
FIXED = ("schema", "rebuild", "break", "think", "reply", "user", "sub", "other", "task:background_review",
         "task:compression", "task:approval", "task:title", "tool:skill_view")
TOOL_HINTS = ("terminal", "browser_exec", "session_search", "read_file", "vision_analyze")  # tip.tool.<name>
HERMES_PROJ, NO_PROJ = "@hermes", "@none"   # project ids, shown as proj.hermes / proj.none
OPT_SKIP = {"homebrew", "containerd", "local", "google", "bin", "lib"}  # system folders under /opt and /srv, no projects


# ---------- Language ----------
LOC = {p.stem: json.loads(p.read_text("utf-8")) for p in sorted((ROOT / "locales").glob("*.json"), key=lambda p: (p.stem != "en", p.stem))}  # English first
DEFAULT_LANG = os.environ.get("USAGECAST_LANG", "en") if os.environ.get("USAGECAST_LANG", "en") in LOC else "en"
_req = threading.local()   # language and URL of the request being rendered


def lang():
    return getattr(_req, "lang", None) or SET["lang"]


def tr(key, **kw):
    """Text for `key` in the current language; falls back to English, then to the key itself."""
    s = LOC[lang()].get(key)
    s = LOC["en"].get(key, key) if s is None else s
    return s.format(**kw) if kw else s


def pick_lang(wanted, headers):
    """?lang= beats the cookie, then the saved language (USAGECAST_LANG, English). The browser language is not used."""
    if wanted in LOC:
        return wanted
    m = re.search(r"(?:^|;)\s*lang=([a-z]+)", headers.get("Cookie", ""))
    return m.group(1) if m and m.group(1) in LOC else SET["lang"]


# ---------- Settings ----------
# /settings saves them in data/settings.json; the environment variables only give the defaults.
def _env_ntfy():
    """(server, topic) from USAGECAST_NTFY, a full topic URL."""
    url = os.environ.get("USAGECAST_NTFY", "")
    u = urlparse(url if "://" in url else "https://" + url)
    return (f"{u.scheme}://{u.netloc}", u.path.strip("/")) if url else ("https://ntfy.sh", "")


_SRV, _TOPIC = _env_ntfy()
DEFAULTS = {
    "theme": "system", "lang": DEFAULT_LANG, "currency": "USD", "numbers": "compact", "period": DEFAULT_P, "limit_view": "used",
    "cost_unit": "money", "source": "",
    "alert_five": True, "five_min": 30, "alert_week": True, "alert_extra": True, "alert_free": True, "alert_steps": True,
    "alert_digest": True, "alert_chat": True, "chat_pct": 0.5, "alert_spike": True, "spike_pct": 3.0, "extra_steps": "",
    "guard": False, "guard_jobs": [], "ingest_token": "",
    "quiet": False, "quiet_from": "22:00", "quiet_to": "07:00",
    "push": "own" if _TOPIC else "off", "ntfy_server": _SRV, "ntfy_topic": _TOPIC, "ntfy_token": "", "url": PUBLIC_URL,
}
CHOICES = {"theme": ("system", "light", "dark"), "lang": tuple(LOC), "currency": ("USD", "EUR"), "numbers": ("compact", "full"),
           "period": tuple(PERIODS), "limit_view": ("used", "left"), "cost_unit": ("money", "week"), "push": ("off", "own", "hermes")}
RANGES = {"five_min": (5, 240), "chat_pct": (0.1, 50.0), "spike_pct": (0.5, 50.0)}
URL_RX = r"https?://[^\s\"'<>]+"
HHMM = r"([01]\d|2[0-3]):[0-5]\d"
FORMATS = {"quiet_from": HHMM, "quiet_to": HHMM, "ntfy_topic": r"[A-Za-z0-9_-]{1,64}", "ntfy_server": URL_RX, "url": f"({URL_RX})?",
           "extra_steps": r"(\d+(\.\d+)?(\s*[,; ]\s*\d+(\.\d+)?)*)?"}


def load_settings():
    try:
        saved = json.loads((DATA / "settings.json").read_text())
    except (OSError, ValueError):
        saved = {}
    ok = lambda k, d: type(saved.get(k)) is type(d) and (k not in CHOICES or saved[k] in CHOICES[k])
    return {k: saved[k] if ok(k, d) else d for k, d in DEFAULTS.items()}


SET = load_settings()


def profiles():
    """Hermes profiles (<home>/profiles/<name> with an own state.db) that /settings can analyse instead."""
    d = HERMES / "profiles"
    return sorted(p.name for p in d.iterdir() if (p / "state.db").is_file()) if d.is_dir() else []


def source_home():
    """Hermes home the analysis reads (state.db, logs, config.yaml, cron, SOUL.md): the main one or the chosen
    profile. Code and limits always come from the main home, profiles share the install and the account."""
    return HERMES / "profiles" / SET["source"] if SET["source"] in profiles() else HERMES


def source_name():
    return "Hermes" if source_home() == HERMES else tr("src.profile", name=html.escape(SET["source"]))


def clean(form, cur):
    """Settings from a submitted /settings form: a missing checkbox is off, an invalid value keeps the old one."""
    s = dict(cur)
    for k, d in DEFAULTS.items():
        v = form.get(k, "").strip()
        if isinstance(d, bool):
            s[k] = k in form
        elif k in CHOICES:
            s[k] = v if v in CHOICES[k] else s[k]
        elif k in RANGES:
            try:
                x = type(d)(v.replace(",", "."))
            except ValueError:
                x = None
            s[k] = min(max(x, RANGES[k][0]), RANGES[k][1]) if x is not None and math.isfinite(x) else s[k]
        elif k == "source":
            s[k] = v if v in ("", *profiles()) else s[k]
        elif k == "ingest_token":
            pass   # only the "new token" button sets it
        elif k == "guard_jobs":
            ids = [j["id"] for j in cron_list()]
            s[k] = [j for j in ids if "gj_" + j in form] if ids else s[k]   # unreadable jobs.json: keep the marks
        elif k == "ntfy_token":
            s[k] = "" if "token_clear" in form else v or s[k]   # empty field = keep the saved token
        elif k in form and re.fullmatch(FORMATS[k], v):
            s[k] = v.rstrip("/")
    return s


def save_settings(s):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = DATA / "settings.json.tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:  # holds the ntfy token
        json.dump(s, fh, indent=1)
    tmp.replace(DATA / "settings.json")
    moved = s.get("source") != SET.get("source")
    SET.clear()
    SET.update(s)
    if moved:   # another Hermes home: nothing computed so far is valid
        with LOCK:
            CACHE.clear()
        CAL.clear()
        RATE.clear()
        threading.Thread(target=ensure_snap, daemon=True).start()


def new_topic():
    return "usagecast-" + secrets.token_hex(8)   # whoever knows a ntfy topic can read it, so 64 random bits


def quiet_now(now, s=None):
    s = s or SET
    t, a, b = datetime.fromtimestamp(now).strftime("%H:%M"), s["quiet_from"], s["quiet_to"]
    return s["quiet"] and (a <= t < b if a <= b else t >= a or t < b)


def same_origin(h):
    """Settings are saved only from Usagecast's own pages: the browser's Sec-Fetch-Site (https), else Origin vs Host."""
    if h.get("Sec-Fetch-Site"):
        return h["Sec-Fetch-Site"] == "same-origin"
    o = urlparse(h.get("Origin") or "")
    return bool(o.netloc) and o.netloc == h.get("Host")


FX = {}   # ECB reference rate: rate = dollars per euro
try:
    FX.update(json.loads((DATA / "fx.json").read_text()))
except (OSError, ValueError):
    pass


def refresh_fx(max_age=6 * 3600):
    """Euro amounts use the ECB's daily reference rate (published on working days around 16:00 CET)."""
    if time.time() - FX.get("at", 0) < max_age:
        return
    try:
        with urllib.request.urlopen("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml", timeout=15) as r:
            x = r.read().decode()
        FX.update(rate=float(re.search(r"currency=.USD. rate=.([\d.]+)", x)[1]), date=re.search(r"time=.([\d-]+)", x)[1], at=time.time())
    except (OSError, ValueError, TypeError) as err:
        print("fx:", err, flush=True)
        return
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "fx.json").write_text(json.dumps(FX))


def nf(v, digits=0):
    """Number with the language's thousands and decimal separators."""
    return f"{v:,.{digits}f}".translate(str.maketrans(",.", tr("num.sep")))


def money(v):
    """API value in the chosen unit: money, or the estimated share of the weekly limit (RATE, see limit_split)."""
    k = RATE.get("k") if SET["cost_unit"] == "week" else None
    if not k:
        return cash(v)
    x = v * k
    return "< " + tr("fmt.week", v=nf(0.1, 1)) if 0 < x < 0.1 else tr("fmt.week", v=nf(x, 1))


def cash(v):
    rate = FX.get("rate") if SET["currency"] == "EUR" else None   # all prices are in dollars
    key, v = ("fmt.eur", v / rate) if rate else ("fmt.money", v)
    return "< " + tr(key, v=nf(0.01, 2)) if 0 < v < 0.01 else tr(key, v=nf(v, 2))


def pct(v, tot):
    return tr("fmt.pct", v=nf(v / (tot or 1) * 100, 1))


def pc(u):
    return tr("fmt.pct", v=nf(u))


def num(n):
    n = float(n or 0)
    if SET["numbers"] == "full":
        return nf(n)
    if n >= 1e6:
        return tr("num.m", v=nf(n / 1e6, 1))
    return tr("num.k", v=nf(n / 1e3)) if n >= 1e3 else nf(n)


def cnt(n):
    return nf(round(n or 0))


def hm(t):
    return datetime.fromtimestamp(t).strftime("%H:%M")


def when(t, kind):
    """Date in the language's pattern; kind: day, daytime, datetime, short."""
    dt, L = datetime.fromtimestamp(t), LOC[lang()]
    return L["fmt." + kind].format(wd=L["weekdays"][dt.weekday()], mon=L["months"][dt.month - 1], d=dt.day,
                                    m=dt.month, y=dt.year, h=dt.hour, hm=dt.strftime("%H:%M"))


# ---------- Analysis ----------
def tok(s):
    return len(s or "") / CPT


def known_model(model):
    return any(model.startswith(k) for k in PRICES)


def price(model):
    # ponytail: unknown Claude models fall back to Opus 5.5 prices, the UI marks them as estimated
    key = max((k for k in PRICES if model.startswith(k)), key=len, default="claude-opus-5-5")
    return [p / 1e6 for p in PRICES[key]]


def row_cost(model, i, r, w, o, ttl):
    """Cost of one usage row in $: (input, cache read, cache write, output)."""
    p = price(model)
    return (i or 0) * p[0], (r or 0) * p[3], (w or 0) * (p[2] if ttl > 300 else p[1]), (o or 0) * p[4]


def config_text():
    try:
        return (source_home() / "config.yaml").read_text()
    except OSError:
        return ""


def config_value(section, key, text=None):
    """Simple value section.key from Hermes' config.yaml, without a YAML library."""
    m = re.search(rf"^{section}:[ \t]*\n(?:[ \t]*\n|[ \t]+.*\n)*?[ \t]+{key}:[ \t]*['\"]?([^'\"\s#]*)",
                  config_text() if text is None else text, re.M)
    return m.group(1) if m else ""


def cache_ttl():
    return 3600 if config_value("prompt_caching", "cache_ttl").lower() == "1h" else 300


def plugin_names(text=None):
    """Enabled plugins (plugins.enabled) plus the memory provider, without platform adapters like platforms/ntfy."""
    text = config_text() if text is None else text
    m = re.search(r"^plugins:[ \t]*\n(?:[ \t]+.*\n)*?[ \t]+enabled:[ \t]*\n((?:[ \t]+-.*\n)+)", text, re.M)
    names = re.findall(r"-[ \t]*['\"]?([^'\"\s#]+)", m.group(1)) if m else []
    return [n for n in names + [config_value("memory", "provider", text)] if n and "/" not in n]


def name_rx(n):
    """Name as a regex, no matter if written with -, _ or spaces and in which case."""
    return "(?i:" + "[-_ ]?".join(map(re.escape, re.split(r"[-_ ]+", n))) + ")"


def markers(plugins=None):
    """(system prompt sections, message injections) for this Hermes installation, as [(id, regex)]."""
    plugins = plugin_names() if plugins is None else plugins
    soul = "soul" if (source_home() / "SOUL.md").is_file() else "base"
    prompt = [(soul, r"\A"), *HERMES_MARKERS, *(("plugin:" + n, rf"^#+ .*{name_rx(n)}") for n in plugins)]
    inject = [*(("plugin:" + n, rf"^(?:<\w+>\s*)?[^\w\n]*{name_rx(n)}") for n in plugins),
              ("notes", r"^\[(?:Note|System note|Context from)")]
    return prompt, inject


def segments(text, markers):
    """[(id, section)] split at the markers; text before the first marker is 'misc'."""
    hits = sorted((m.start(), label) for label, rx in markers for m in [re.search(rx, text, re.M)] if m)
    if not hits or hits[0][0] > 0:
        hits.insert(0, (0, "misc"))
    return [(label, text[s:e]) for (s, label), (e, _) in zip(hits, hits[1:] + [(len(text), "")]) if text[s:e].strip()]


def injections(content, api, marks):
    extra = api or ""
    if extra and content:
        extra = extra[len(content):] if extra.startswith(content) else extra.replace(content, "", 1)
    return segments(extra, marks) if extra.strip() else []


def tool_of(tc):
    f = tc.get("function") or {}
    name, args = f.get("name") or "?", f.get("arguments") or ""
    if name == "tool_call":  # lazily loaded tools: use the real name
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
    """Replays a session call by call: what was in the prompt at every API call.

    msgs: (role, content, api_content, tool_name, tool_calls, tool_call_id, timestamp, reasoning)
    extra_out: output per call that Hermes does not store as text (thinking) but that stays in the history.
    inject: markers for blocks that plugins and Hermes attach to messages (see markers()).
    Per call: (start time, gap to the previous call, prompt before{}, prompt now{}, output{}, [called components]).
    Plus sizes [(time, component, tokens)] of tool results and plugin injections."""
    ctx, before, last, prev, calls, sizes, pending = defaultdict(float, prefix), {}, None, None, [], [], {}
    for role, content, api, tname, tcalls, tcid, ts, reasoning in msgs:
        if role == "assistant":
            t = prev if prev is not None else ts  # the call starts with the message before it
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
    """What a call read from the cache, what it wrote anew and how much of the old prompt was lost.

    rho: share of the previous prompt that really came from the cache according to agent.log. Without the log the
    rule applies: after more than `ttl` seconds of pause the cache is gone. The cache always returns the beginning
    of the prompt, so system prompt and tools first, then the history."""
    cand = before or {k: v for k, v in now.items() if is_prefix(k)}
    if rho is None:
        rho = 1.0 if before and gap <= ttl else 0.0
    total, pre = sum(cand.values()), sum(v for k, v in cand.items() if is_prefix(k))
    keep = total if rho > 0.97 else rho * total  # small deviations are measurement noise
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
    """Real values per API call from Hermes' agent.log: {session: ([end time], [(total input, of that from cache)])}.
    Rotated files are read only once."""
    idx = defaultdict(list)
    for f in glob.glob(str(source_home() / "logs" / "agent.log*")):
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
    """Language-neutral origin id: the source, or cron:<job name> for cron sessions."""
    if src == "cron":
        return "cron:" + (title or "").split(" · ")[0]
    return src or "?"


def floor(t, hourly):
    if hourly:
        return t - t % 3600
    return datetime.fromtimestamp(t).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


IMG = ("[screenshot]", "[Image attached")   # how Hermes stores images in the history
BREAKS = ("shrunk", "image", "turn", "deep", "other", "sim")


def break_cause(rows, apos, j, real, prefix):
    """Why the cache broke right before assistant row j although there was no pause. shrunk: the prompt got smaller,
    older history was removed or rewritten. image: a new image while more than 3 are in the history (Hermes drops old
    ones from the request). turn: a new message came in (the gateway reloads the conversation, images saved as text
    change). deep: only the system prompt was read from cache, the change sat more than ~20 blocks back. other: none
    of these. sim: no log values for this step, the replay only saw the start of the prompt change."""
    if not real[j]:
        return "sim"
    new = rows[apos[j - 1] + 1:apos[j]] if j else rows[:apos[0]]
    if j and real[j - 1] and real[j][0] < real[j - 1][0]:
        return "shrunk"
    imgs = lambda rs: sum((m[1] or "").count(x) for m in rs for x in IMG)
    if imgs(new) and imgs(rows[:apos[j]]) > 3:
        return "image"
    if any(m[0] == "user" for m in new):
        return "turn"
    return "deep" if real[j][1] <= prefix * 1.1 else "other"


def split_turns(steps, users):
    """[(start, end, steps, $)] of one session: a turn = the API steps between two user messages. steps: [(time, $)]
    in order, users: sorted user message times."""
    out = []
    for t, c in steps:
        i = bisect.bisect_right(users, t)
        start = users[i - 1] if i else steps[0][0]
        if out and out[-1][0] == start:
            out[-1] = (start, t, out[-1][2] + 1, out[-1][3] + c)
        else:
            out.append((start, t, 1, c))
    return out


def analyze(c, since, snap, ttl, sid=None, logs=None, until=math.inf):
    """Splits the real costs since `since` (or of one session) over components, origin and time.
    Invariant: sum(comp) == real cost of the calls in the period. Contains only ids, no display text."""
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

    hourly = sid is None and min(until, time.time()) - since < 2 * 86400
    comp, where = defaultdict(float), defaultdict(float)
    hours = defaultdict(lambda: defaultdict(float))       # hour start -> component -> $
    hsteps, hsess = defaultdict(int), defaultdict(set)    # hour start -> steps, sessions
    models = defaultdict(lambda: [0.0, 0.0, 0.0])        # calls, tokens, $
    uses, sizes_acc = defaultdict(int), defaultdict(lambda: [0, 0.0])
    sess, steps, breaks, heavy = {}, [], defaultdict(lambda: [0, 0.0]), []   # breaks: cause -> [count, $]
    tot = dict(calls=0.0, tokens=0.0, long=0.0, writes=0.0, avoid=0.0, extra=0.0, n_steps=0, logged=0)
    repos, prx = home_repos(), project_rx()

    for s, rows in usage.items():
        src, title, hsh, started, repo_root = meta.get(s, ("?", None, None, 0, None))
        sc, n_calls, turns, last = defaultdict(float), 0, [], [0.0]

        def put(k, v, t):
            sc[k] += v
            last[0] = max(last[0], t)
            h = t - t % 3600
            hours[h][k] += v
            hsess[h].add(s)

        for m, task, n, i, r, w, o, seen in (x for x in rows if x[1] != "chat"):
            if since < seen <= until:
                cost = sum(row_cost(m, i, r, w, o, ttl))
                put("task:" + task, cost, seen)
                models[m][0] += n; models[m][1] += i + r + w + o; models[m][2] += cost
                tot["calls"] += n; tot["tokens"] += i + r + w + o
        chat = [x for x in rows if x[1] == "chat"]
        if not chat:
            pass
        elif not msgs.get(s):
            seen = max(x[7] for x in chat)
            if since < seen <= until:
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
            # Output Hermes does not store as text (thinking) still stays in the history: second pass
            kt = (I + R + W) / (sum(sum(x[3].values()) for x in calls) or 1)  # real tokens per simulated token
            missing = O / kt - sum(sum(x[4].values()) for x in calls)
            if missing > 0 and calls:
                calls, sizes = simulate(msgs[s], pre, missing / len(calls), im)
                kt = (I + R + W) / (sum(sum(x[3].values()) for x in calls) or 1)
            # Attach real values per call from the log (end of the call = timestamp of the answer)
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
            # Spread the real cost per token type over the simulated shares
            SR = sum(sum(x[0].values()) for x in splits)
            SW = sum(sum(x[1].values()) + x[2] for x in splits)
            SO = sum(sum(x[4].values()) for x in calls)
            fr, fw = (cr / SR, (cw + ci) / SW) if SR and SW else (0.0, (cr + cw + ci) / (SR + SW or 1))
            fo = co / SO if SO else 0.0
            p_cost, apos, tsteps = 0.0, [i for i, m in enumerate(msgs[s]) if m[0] == "assistant"], []
            # ponytail: a result is written once and read on every later step; rebuilds and compression are ignored
            ts = [x[0] for x in calls]
            for m in msgs[s]:
                if m[0] == "tool" and since < m[6] <= until:
                    n = tok(m[1]) + IMAGE_TOK * sum((m[1] or "").count(x) for x in IMG)
                    later = len(ts) - bisect.bisect_right(ts, m[6])
                    heavy.append((n * (fw + fr * later), s, m[3] or "?", n * kt, later, m[6]))
            for j, ((t, gap, before, now, out, keys), (hit, new, lost, kind, logged)) in enumerate(zip(calls, splits)):
                if t <= since or t > until:
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
                tsteps.append((t, cost))
                n_calls += 1
                hsteps[t - t % 3600] += 1
                tot["n_steps"] += 1; tot["logged"] += logged
                ctx_tok = sum(now.values()) * kt
                if ctx_tok > LONG_CTX:
                    tot["long"] += cost
                # for the cache duration tip: what 1 h instead of 5 min (or the other way round) would have changed
                tot["writes"] += (sum(new.values()) + lost) * fw
                if kind == "rebuild" and gap <= 3600:
                    tot["avoid"] += lost * fw
                if not lost and 300 < gap <= 3600:
                    tot["extra"] += sum(before.values()) * fw
                for k in keys:
                    uses[k] += 1
                why = ""
                if kind == "break" and lost > 1e-9:
                    why = break_cause(msgs[s], apos, j, real, sum(v for k, v in now.items() if is_prefix(k)) * kt)
                    breaks[why][0] += 1
                    breaks[why][1] += lost * fw
                if sid:
                    steps.append((t, ctx_tok, cost, lost * fw, (kind + ":" + why if why else kind) if lost > 1e-9 else "", keys))
            turns = split_turns(tsteps, sorted(m[6] for m in msgs[s] if m[0] == "user"))
            for t, k, n in sizes:
                if since < t <= until:
                    sizes_acc[k][0] += 1; sizes_acc[k][1] += n
            f = p_cost / chat_cost if chat_cost else 0.0  # share of the session inside the period
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
                       turns=turns, last=last[0],
                       project=project_of((m[4] for m in msgs.get(s, ()) if m[4]), repos, prx, repo_root))
    # 5 min -> 1 h: rebuilds after pauses up to 1 h disappear, every write costs 2/1.25 = 1.6x. The other way round accordingly.
    if ttl <= 300:
        ttl_save, ttl_alt = tot["avoid"] - (tot["writes"] - tot["avoid"]) * 0.6, 3600
    else:
        ttl_save, ttl_alt = tot["writes"] * 0.375 - tot["extra"] * 0.625, 300
    return dict(comp=comp, where=where, models=models, hours=hours, hsteps=hsteps, hsess=hsess, sess=sess, steps=steps,
                uses=uses, sizes=sizes_acc, breaks=dict(breaks), heavy=sorted(heavy, reverse=True)[:10], total=sum(comp.values()), ttl=ttl, ttl_alt=ttl_alt, ttl_save=ttl_save,
                since=since, until=until, hourly=hourly, **tot)


# ---------- Projects ----------
def home_repos(home=None):
    """Git repos directly in the home folder or one level deeper (e.g. ~/projects/app): {relative path: name}."""
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
    """Paths in tool calls: Hermes' own folder, /opt/x and /srv/x, anything under ~ (as ~, $HOME or spelled out)."""
    home, hermes = str(Path.home() if home is None else home), str(HERMES if hermes is None else hermes)
    h = rf"(?:{re.escape(home)}|~|\$HOME)"
    herm = re.escape(hermes) + (f"|{h}{re.escape(hermes[len(home):])}" if hermes.startswith(home + "/") else "")
    return re.compile(rf"(?P<herm>(?:{herm})(?![\w.-])(?P<junk>/(?:cache|sandboxes)\b)?)|/(?:opt|srv)/(?P<opt>[\w.-]+)"
                      rf"|{h}/(?P<home>(?!\.)[\w.-]+(?:/(?!\.)[\w.-]+)?)")


def project_of(texts, repos, rx, repo_root=None):
    """Project of a session: its git root if Hermes knows it (terminal sessions only), otherwise the project whose path
    shows up most often in the tool calls. Hermes' own folder only counts if hardly anything else shows up, because
    almost every session touches skills or scripts there on the side."""
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
    # ponytail: majority of paths; a session spanning two projects counts fully for the more frequent one
    if best and best[0][1] >= (max(2, hermes / 4) if hermes else 1):
        return best[0][0]
    return HERMES_PROJ if hermes else None


# ---------- Labels and tips (display text, current language) ----------
def ranked(comp, group_skills=True):
    g = defaultdict(float)
    for k, v in comp.items():
        g["tool:skill_view" if group_skills and k.startswith("skill:") else k] += v
    return sorted(((k, v) for k, v in g.items() if v > 0), key=lambda x: -x[1])


def ttl_text(ttl):
    return tr("ttl.1h") if ttl > 300 else tr("ttl.5m")


def seg_label(name):
    """System prompt part or message injection: soul, base, ..., plugin:<name>."""
    if name.startswith("plugin:"):
        return tr("seg.plugin", name=name[7:])
    return tr("seg." + name) if "seg." + name in LOC["en"] else name


def origin_label(k):
    if k.startswith("cron:"):
        return tr("origin.cron", name=k[5:] or tr("untitled"))
    return tr("src." + k) if "src." + k in LOC["en"] else (k or "?").capitalize()


def proj_label(n):
    return {HERMES_PROJ: tr("proj.hermes"), NO_PROJ: tr("proj.none")}.get(n, n)


def label(k, ttl=300):
    """(name, explanation) of a component key."""
    if k in FIXED:
        return tr("comp." + k), tr("comp." + k + ".why", ttl=ttl_text(ttl))
    kind, _, name = k.partition(":")
    if kind == "tool":
        return tr("label.tool", name=name), tr("label.tool.why", name=name)
    if kind == "skill":
        return tr("label.skill", name=name), tr("label.skill.why")
    if kind == "inj":
        return seg_label(name), tr("label.inj.why")
    if kind == "sys":
        return (seg_label(name) if name.startswith("plugin:") else tr("label.sys", part=seg_label(name))), tr("label.sys.why")
    if kind == "task":
        return tr("label.task", name=name), tr("label.task.why")
    return k, ""


EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")   # Hermes' reasoning_effort steps


def fix(*args):
    """Copyable hermes command under a tip, aimed at the analysed profile."""
    h = source_home()
    words = ["hermes", *(["-p", h.name] if h != HERMES else []), *map(str, args)]
    return f'<code class="cmd fix">{html.escape(" ".join(words))}</code>'


def idle_sets(tools, sets, used):
    """Enabled toolsets none of whose tools ran, heaviest first: [(name, schema cost)]. sets: tool -> its toolsets."""
    cost, busy = defaultdict(float), set()
    for n, v in tools.items():
        for k in sets.get(n, ()):
            cost[k] += v
            if n in used:
                busy.add(k)
    return sorted(((k, v) for k, v in cost.items() if k not in busy), key=lambda x: -x[1])


def tips(d, snap):
    """[(weight, title, html text)], heaviest first."""
    tot, out = d["total"] or 1, []
    p = lambda v: pct(v, tot)
    save = d["ttl_save"]
    if save > 0.03 * tot:
        key = "tip.ttl1h" if d["ttl_alt"] > 300 else "tip.ttl5m"
        out.append((save, tr(key), tr(key + ".text", p=p(save))
                    + fix("config", "set", "prompt_caching.cache_ttl", "1h" if key == "tip.ttl1h" else "5m")))
    br = d["comp"].get("break", 0)
    if br > 0.05 * tot:
        out.append((br * 0.9, tr("tip.break"), tr("tip.break.text", p=p(br))))
    tools = snap.get("tools", {})
    used = {k.split(":", 1)[1] for k in d["uses"] if k.startswith("tool:")} | ({"skill_view"} if any(k.startswith("skill:") for k in d["uses"]) else set())
    st = sum(tools.values()) or 1
    unused = sorted(((n, d["comp"].get("schema", 0) * t / st) for n, t in tools.items() if n not in used), key=lambda x: -x[1])
    s = sum(v for _, v in unused)
    if s > 0.02 * tot:
        top = ", ".join(f"{html.escape(n)} ({p(v)})" for n, v in unused[:4])
        idle = [k for k, _ in idle_sets(tools, snap.get("sets") or {}, used)][:6]
        cmd = fix("tools", "disable", "--platform", snap.get("platform") or "cli", *idle) if idle else ""
        out.append((s, tr("tip.unused"), tr("tip.unused.text", n=len(unused), p=p(s), top=top) + cmd))
    if d["long"] > 0.2 * tot:
        out.append((d["long"] / 2, tr("tip.long"), tr("tip.long.text", p=p(d["long"]), n=nf(LONG_CTX))))
    think = d["comp"].get("think", 0)
    effort = config_value("agent", "reasoning_effort")
    if think > 0.15 * tot and effort in EFFORTS[3:]:
        out.append((think * 0.3, tr("tip.think"), tr("tip.think.text", p=p(think), effort=html.escape(effort),
                                                       highest=tr("tip.think.highest") if effort in ("xhigh", "max") else "")
                    + fix("config", "set", "agent.reasoning_effort", EFFORTS[EFFORTS.index(effort) - 1])))
    bg = d["comp"].get("task:background_review", 0)
    if bg > 0.05 * tot:  # agent.log shows the review reads the history with its own prompt, so without cache hits
        every = ((f"{a}.{b}", config_value(a, b) or "10") for a, b in (("skills", "creation_nudge_interval"), ("memory", "nudge_interval")))
        cmd = "".join(fix("config", "set", k, 2 * int(v)) for k, v in every if v.isdigit() and int(v))   # 0 = already off
        out.append((bg, tr("tip.review"), tr("tip.review.text", p=p(bg)) + cmd))
    tl = sorted(((k, v) for k, v in d["comp"].items() if k.startswith("tool:")), key=lambda x: -x[1])
    if tl and tl[0][1] > 0.08 * tot:
        name = tl[0][0][5:]
        hint = tr("tip.tool." + name) if name in TOOL_HINTS else tr("tip.tool.any", name=html.escape(name))
        out.append((tl[0][1] / 3, tr("tip.tool", name=name), hint + " " + tr("tip.share", p=p(tl[0][1]))))
    for org, v in sorted(d["where"].items(), key=lambda x: -x[1]):
        if org.startswith("cron:") and v > 0.05 * tot:
            out.append((v / 2, tr("tip.cron", name=org[5:] or tr("untitled")), tr("tip.cron.text", p=p(v))))
    mem = config_value("memory", "provider")
    for k, v in d["comp"].items():
        if k.startswith("inj:") and v > 0.03 * tot:
            plug = k[4:].removeprefix("plugin:") if k.startswith("inj:plugin:") else ""
            cmd = fix("plugins", "disable", plug) if plug and plug != mem else ""   # a memory provider isn't a plugin switch
            out.append((v, tr("tip.inj", name=seg_label(k[4:])), tr("tip.inj.text", p=p(v)) + cmd))
    return sorted(out, key=lambda x: -x[0])


def limit_history(days=7):
    cut = time.time() - days * 86400
    try:
        return [h for h in map(json.loads, (DATA / "limits.jsonl").read_text().splitlines()) if h["t"] > cut]
    except (OSError, ValueError):
        return []


# ---------- Background: limits + snapshot ----------
LIMITS_PY = r'''
import json, urllib.request
try:
    from agent.anthropic_credentials import resolve_anthropic_token
except ImportError:  # Hermes before October 2026
    from agent.anthropic_adapter import resolve_anthropic_token
req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
    "Authorization": "Bearer " + (resolve_anthropic_token() or ""), "Accept": "application/json",
    "anthropic-beta": "oauth-2025-04-20", "User-Agent": "claude-code/2.1.0"})
print(urllib.request.urlopen(req, timeout=20).read().decode())
'''
STATE = {"limits": None, "at": 0.0, "ok": 0.0}  # at = last attempt, ok = last successful fetch
LIMITS_LOCK = threading.Lock()


def refresh_limits(max_age):
    """Fetch the subscription limits through Hermes' OAuth login (the token stays in the Hermes process) and log them."""
    with LIMITS_LOCK:
        if time.time() - STATE["at"] < max_age:
            return
        r = None
        try:
            r = subprocess.run([str(HERMES_PY), "-c", LIMITS_PY], cwd=HERMES / "hermes-agent",
                               capture_output=True, text=True, timeout=40)
            data = json.loads(r.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            print("limits: not available:", (r.stderr.strip().splitlines() or [""])[-1] if r else "", flush=True)
            STATE["at"] = time.time() - max(max_age - 60, 0)  # try again in a minute
            return
        STATE.update(limits=data, at=time.time(), ok=time.time())
        row = {"t": round(time.time())}
        for k in ("five_hour", "seven_day"):
            row[k] = (data.get(k) or {}).get("utilization")
        x = data.get("extra_usage") or {}
        if x.get("is_enabled") and x.get("used_credits") is not None:
            row["extra"] = round(x["used_credits"] / 10 ** (x.get("decimal_places") or 0), 2)
        DATA.mkdir(parents=True, exist_ok=True)
        with open(DATA / "limits.jsonl", "a") as f:
            f.write(json.dumps(row) + "\n")


def fresh_limits():
    """Page loads: with an earlier reading answer at once and fetch in the background (the fetch takes ~2 s).
    Only the very first reading after a start is waited for."""
    if STATE["limits"] is None:
        refresh_limits(120)
    elif time.time() - STATE["at"] >= 120 and not LIMITS_LOCK.locked():
        threading.Thread(target=refresh_limits, args=(120,), daemon=True).start()


# ---------- Limits: who used them ----------
CLAUDE = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"
_CC = {}      # transcript -> (mtime, [(time, $)])
RATE = {}     # limit_split over 30 days: k = weekly-limit % per API dollar
FC = {}       # weekly rhythm: prof = 168 hourly shares of a limit week, method weighted/linear, backtest errors


def cc_calls(since):
    """[(time, $)] of Claude Code's API calls since `since`: this machine's transcripts (deduplicated: Claude Code
    writes one line per content block, all with the same message id and usage) plus the hours other machines reported."""
    out = []
    for p in CLAUDE.rglob("*.jsonl"):
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if m < since:
            continue
        if _CC.get(p, (None,))[0] != m:
            seen, rows = set(), []
            with open(p, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        x = json.loads(line)
                        msg, u = x["message"], x["message"]["usage"]
                        key, model = (msg.get("id"), x.get("requestId")), str(msg.get("model") or "")
                        t = datetime.fromisoformat(x["timestamp"].replace("Z", "+00:00")).timestamp()
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue
                    if x.get("type") != "assistant" or key in seen or not model.startswith("claude"):
                        continue
                    seen.add(key)
                    w1h = (u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0
                    pr = price(model)
                    rows.append((t, (u.get("input_tokens") or 0) * pr[0] + (u.get("cache_read_input_tokens") or 0) * pr[3]
                                 + ((u.get("cache_creation_input_tokens") or 0) - w1h) * pr[1] + w1h * pr[2]
                                 + (u.get("output_tokens") or 0) * pr[4]))
            _CC[p] = (m, rows)
        out += [r for r in _CC[p][1] if r[0] >= since]
    return out + [(h, c) for x in ingest_hosts().values() for h, c in x.get("hours", []) if h >= since]


HOST_RX = r"[A-Za-z0-9 ._-]{1,40}"


def parse_ingest(raw):
    """(host, since, rows) of a report from tools/cc-report.py, None when it is malformed. rows: [hour, model, input,
    cache_write, cache_read, output(, of the writes 1-hour cache)] with non-negative numbers."""
    try:
        body = json.loads(raw)
        host, rows, since = body["host"], body["hours"], body.get("since")
        assert isinstance(host, str) and re.fullmatch(HOST_RX, host) and isinstance(rows, list)
        assert since is None or isinstance(since, (int, float))
        out = []
        for r in rows:
            assert isinstance(r, list) and len(r) in (6, 7) and isinstance(r[1], str)
            nums = [r[0], *r[2:]]
            assert all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 1e13 for v in nums)
            out.append([int(r[0]) - int(r[0]) % 3600, r[1][:80], *nums[1:], *([0] if len(r) == 6 else [])])
        return host, since, out
    except (ValueError, KeyError, TypeError, AssertionError, AttributeError):
        return None


def save_ingest(host, since, rows, now=None):
    """Prices a report and stores it per host (data/ingest/<host>.json): hours from `since` on are replaced, older ones
    stay for 35 days, so sending the same report twice changes nothing."""
    now = now or time.time()
    cost = defaultdict(float)
    for h, model, i, w, r, o, w1 in rows:
        pr, w1 = price(model), min(w1, w)
        cost[h] += i * pr[0] + (w - w1) * pr[1] + w1 * pr[2] + r * pr[3] + o * pr[4]
    first = since if since is not None else min(cost, default=now)
    f = DATA / "ingest" / f"{host}.json"
    keep = {int(h): c for h, c in (read_json(f) or {}).get("hours", []) if now - 35 * 86400 < h < first}
    keep.update(cost)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"host": host, "at": round(now), "hours": sorted([h, round(c, 6)] for h, c in keep.items())}))


def ingest_hosts():
    """{host: {at, hours: [[hour, $]]}} of the other machines that report their Claude Code usage."""
    d = DATA / "ingest"
    return {x["host"]: x for x in (read_json(f) for f in sorted(d.glob("*.json"))) if x} if d.is_dir() else {}


def limit_split(hist, hours, cc, since, min_cost=0.25):
    """Who used the weekly limit since `since`, measured per clock hour: the growth between the 10-minute readings
    goes to Hermes and Claude Code (split by their API cost in that hour) or, in hours where neither ran, to "other"
    (Claude app, other devices). k = limit % per API dollar over the hours with Hermes or Claude Code. None without
    readings."""
    # ponytail: hour-level attribution; other usage in an hour where Hermes also ran counts as Hermes
    pts = [h for h in hist if h["t"] >= since and h.get("seven_day") is not None]
    if len(pts) < 2:
        return None
    ccost = defaultdict(float)
    for t, c in cc:
        ccost[t - t % 3600] += c
    out, seen = {"hermes": 0.0, "cc": 0.0, "rest": 0.0}, set()
    for a, b in zip(pts, pts[1:]):
        du, h = b["seven_day"] - a["seven_day"], a["t"] - a["t"] % 3600
        if du < 0 or b["t"] - a["t"] > 1800:   # weekly reset, or a gap without readings
            continue
        seen.add(h)
        hc, c = sum(hours.get(h, {}).values()), ccost.get(h, 0.0)
        if hc + c < min_cost:
            out["rest"] += du
        else:
            out["hermes"] += du * hc / (hc + c)
            out["cc"] += du * c / (hc + c)
    cost = sum(sum(hours.get(h, {}).values()) + ccost.get(h, 0.0) for h in seen)
    used = out["hermes"] + out["cc"]
    out.update(start=pts[0]["t"], before=pts[0]["seven_day"], hours=len(seen),
               gap=pts[-1]["seven_day"] - pts[0]["seven_day"] - used - out["rest"],
               k=used / cost if cost >= 5 and used >= 5 else None)  # 1 % steps: below that it is mostly rounding
    return out


def extra_months(hist):
    """[[start, used]] per billing month of the extra credits, from the readings: a month ends where the counter drops
    (calendar month or any other reset day, whatever the account does)."""
    out, prev = [], None
    for h in hist:
        x = h.get("extra")
        if x is None:
            continue
        if prev is None or x < prev:
            out.append([h["t"], x])
        out[-1][1], prev = max(out[-1][1], x), x
    return out


def overrun_usd(fc):
    """What the part of the week forecast above 100 % would cost in extra credits (billed at API prices), or None."""
    return (fc - 100) / RATE["k"] if fc and fc > 100 and RATE.get("k") else None


# ---------- Limits: forecast + alerts ----------
WINDOWS = (("five_hour", 5 * 3600), ("seven_day", 7 * 86400), ("seven_day_opus", 7 * 86400), ("seven_day_sonnet", 7 * 86400))


def windows(L, now):
    """[(key, utilization %, share of the window elapsed or None, reset time or None, length)]."""
    out = []
    for key, length in WINDOWS:
        w = (L or {}).get(key) or {}
        if w.get("utilization") is None:
            continue
        try:
            reset = datetime.fromisoformat(w["resets_at"]).timestamp()
        except (KeyError, TypeError, ValueError):
            reset = None
        frac = min(max(1 - (reset - now) / length, 0.0), 1.0) if reset else None
        out.append((key, float(w["utilization"]), frac, reset, length))
    return out


def week_forecast(u, frac):
    """Utilization at the reset. Linear from the pace since the window started, or, when the backtest found the weekly
    rhythm more accurate (FC), divided by the share of a usual week that has passed by now. None in the first half day."""
    if not frac or frac < 1 / 14:
        return None
    p = FC.get("prof") if FC.get("method") == "weighted" else None
    if p:
        x = min(frac, 1.0) * 168
        i = int(x)
        a = sum(p[:i]) + (p[i] * (x - i) if i < 168 else 0.0)
        if a >= 0.1:
            return u / a
    return u / frac


def rhythm(cost, ws, since):
    """Weekly rhythm from hourly API cost {hour: $}: (profile = mean share of each of the 168 hours of a limit week,
    {lin, wtd} = mean absolute % error of the final week cost predicted every hour from day 2 to day 6, each week
    against the profile of the others). Uses up to 4 complete limit weeks before ws that lie after since; needs 3."""
    weeks = [w for w in ([cost.get(s + 3600 * h, 0.0) for h in range(168)] for s in
                         (ws - 7 * 86400 * i for i in range(1, 5)) if s >= since) if sum(w) > 0][:4]
    if len(weeks) < 3:
        return None, None
    norm = [[c / sum(w) for c in w] for w in weeks]
    mean = lambda rows: [sum(r[h] for r in rows) / len(rows) for h in range(168)]
    err = {"lin": [], "wtd": []}
    for i, w in enumerate(weeks):
        p = mean([x for j, x in enumerate(norm) if j != i])
        for t in range(24, 145):
            used, a = sum(w[:t]), sum(p[:t])
            lin = used * 168 / t
            err["lin"].append(abs(lin - sum(w)) / sum(w))
            err["wtd"].append(abs((used / a if a >= 0.1 else lin) - sum(w)) / sum(w))
    return mean(norm), {k: sum(v) / len(v) * 100 for k, v in err.items()}


def rate(hist, key, u, start, now):
    """Usage in % per second over the last hour, only from points of this window; None without 10 min of data."""
    pts = [h for h in hist if h["t"] >= max(start, now - 3600) and h.get(key) is not None]
    if not pts or now - pts[0]["t"] < 600:
        return None
    return max((u - pts[0][key]) / (now - pts[0]["t"]), 0.0)


def alerts(L, hist, sent, now, s=None):
    """Due alerts [(tag, title, text)] and the new state. A tag stands for one window; the caller stores it after a
    successful send, so every alert comes at most once per window. s: settings (switches, 5-hour threshold)."""
    s = s or SET
    sent, out, full = {k: v for k, v in sent.items() if k == "extra_used" or k.startswith("extra@") or now - v < 8 * 86400}, [], []
    for key, u, frac, reset, length in windows(L, now):
        name = tr("win." + key)
        if u >= 100:
            full.append(name)
            if length <= 5 * 3600 and reset:
                sent["full:" + key] = reset   # remembered for "free again"
        if not reset or u >= 100:
            continue
        if key == "seven_day" and s["alert_steps"]:
            step = next((x for x in (90, 80) if u >= x), None)   # only the highest step reached, once per week
            if step and not any(f"{key}@{x}:{reset:.0f}" in sent for x in (80, 90) if x >= step):
                out.append((f"{key}@{step}:{reset:.0f}", tr("alert.step", name=name, u=pc(u)),
                            tr("alert.step.text", reset=when(reset, "daytime"), pct=pc((100 - u) / max((reset - now) / 86400, 1)))))
        tag = f"{key}:{reset:.0f}"
        if tag in sent:
            continue
        if length <= 5 * 3600:
            r = rate(hist, key, u, reset - length, now)
            fa = now + (100 - u) / r if r else None
            if s["alert_five"] and fa and fa - now < s["five_min"] * 60 and fa < reset:
                out.append((tag, tr("alert.five", name=name), tr("alert.five.text", u=pc(u), full=hm(fa), reset=hm(reset))))
        else:
            fc = week_forecast(u, frac)
            if s["alert_week"] and fc and fc > 100 and frac >= 1 / 7:
                start = reset - length
                out.append((tag, tr("alert.week", name=name),
                            tr("alert.week.text", u=pc(u), frac=pc(frac * 100), full=when(start + (now - start) * 100 / u, "daytime"),
                               reset=when(reset, "daytime"))))
    for k in [k for k in sent if k.startswith("full:")]:
        tag = f"free:{k[5:]}:{sent[k]:.0f}"
        if s["alert_free"] and 0 <= now - sent[k] < 3600 and tag not in sent:   # within the hour after the reset
            out.append((tag, tr("alert.free", name=tr("win." + k[5:])), tr("alert.free.text", reset=hm(sent[k]))))
    x = (L or {}).get("extra_usage") or {}
    used = x.get("used_credits")
    if x.get("is_enabled") and used is not None:
        prev, dp = sent.get("extra_used"), 10 ** (x.get("decimal_places") or 0)
        if prev is not None and used < prev:   # a new billing month: the steps count again
            sent = {k: v for k, v in sent.items() if not k.startswith("extra@")}
        steps = [float(v) for v in re.findall(r"\d+(?:\.\d+)?", s["extra_steps"])]
        step = max((v for v in steps if used / dp >= v), default=None)   # only the highest step reached, once per month
        if step is not None and not any(f"extra@{v:g}" in sent for v in steps if v >= step):
            out.append((f"extra@{step:g}", tr("alert.extra.step", step=nf(step, 0 if step == int(step) else 2), cur=x.get("currency") or ""),
                        tr("alert.extra.step.text", used=nf(used / dp, 2), limit=nf((x.get("monthly_limit") or 0) / dp, 2), cur=x.get("currency") or "")))
        reset = next((w[3] for w in windows(L, now) if w[3]), 0)  # 5-hour window first, otherwise the week
        tag = f"extra:{reset:.0f}"
        if s["alert_extra"] and prev is not None and used > prev and tag not in sent:  # the state stays old until the alert went out
            out.append((tag, tr("alert.extra"), tr("alert.extra.text", used=nf(used / dp, 2),
                                                     limit=nf((x.get("monthly_limit") or 0) / dp, 2), cur=x.get("currency") or "",
                                                     full=tr("alert.full", names=", ".join(full)) + " " if full else "")))
        else:
            sent["extra_used"] = used
    return out, sent


def hermes_ntfy():
    """(server, topic, token) of the channel Hermes itself sends to (NTFY_* in ~/.hermes/.env), otherwise None."""
    env = {}
    try:
        for line in (HERMES / ".env").read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip().startswith("NTFY_"):
                env[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    if not env.get("NTFY_HOME_CHANNEL"):
        return None
    return (env.get("NTFY_SERVER_URL") or "https://ntfy.sh").rstrip("/"), env["NTFY_HOME_CHANNEL"], env.get("NTFY_TOKEN")


def ntfy_target():
    """(server, topic, token) of the push target chosen in the settings, None while push is off."""
    if SET["push"] == "hermes":
        return hermes_ntfy()
    if SET["push"] == "own" and SET["ntfy_topic"]:
        return SET["ntfy_server"], SET["ntfy_topic"], SET["ntfy_token"] or None
    return None


def send_push(title, text, prio=4, path=""):
    """(sent, the server's reply). Priority 4 = high (limit almost full, week won't last), 3 = normal, 2 = low."""
    t = ntfy_target()
    if not t:
        return False, tr("set.push.state.off")
    server, topic, token = t
    body = {"topic": topic, "title": title, "message": text, "priority": prio, **({"click": SET["url"] + path} if SET["url"] else {})}
    headers = {"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})}
    try:
        with urllib.request.urlopen(urllib.request.Request(server, json.dumps(body).encode(), headers), timeout=15) as r:
            return True, f"{r.status} {r.read(400).decode('utf-8', 'replace').strip()}"
    except urllib.error.HTTPError as err:
        return False, f"{err.code} {err.read(400).decode('utf-8', 'replace').strip()}"
    except OSError as err:
        return False, str(err)


def notify(title, text, prio=4, path=""):
    ok, reply = send_push(title, text, prio, path)
    if not ok:
        print("ntfy:", reply, flush=True)
    return ok


def digest(now, sent, L, get=None, s=None):
    """Sunday-evening summary of the limit week, once per ISO week: (tag, title, text) or None. The comparison is
    against the same stretch of time one week earlier."""
    s, get = s or SET, get or data_for
    dt = datetime.fromtimestamp(now)
    tag = "digest:" + dt.strftime("%G-%V")
    if not s["alert_digest"] or dt.weekday() != 6 or dt.hour < 18 or tag in sent:
        return None
    w, d30 = get("w"), get("30")
    prev = sum(sum(v.values()) for h, v in d30["hours"].items() if w["since"] - 7 * 86400 <= h < now - 7 * 86400)
    delta = tr("alert.digest.delta", d=signed(w["total"], prev)) if prev else ""
    top, wk = ranked(w["comp"]), next((x for x in windows(L, now) if x[0] == "seven_day"), None)
    return tag, tr("alert.digest"), tr("alert.digest.text", u=pc(wk[1]) if wk else "–", reset=when(wk[3], "daytime") if wk and wk[3] else "–",
                                       cost=cash(w["total"]), delta=delta, top=label(top[0][0], w["ttl"])[0] if top else "–")


NOT_CHAT = ("cron", "subagent")   # sources that are no conversation with a person


def watch(d30, now, sent, k, s=None):
    """Chat watch and spike alerts [(tag, title, text, path)] for runs with a step in the last 20 minutes: a long chat
    whose replies have become expensive, and a cron run or a single reply far above its usual cost. k: weekly % per
    API dollar (limit_split); without it both stay silent."""
    s = s or SET
    if not k:
        return []
    med = lambda v: statistics.median(v) if v else 0.0
    chats = [x for x in d30["sess"].values() if x["src"] not in NOT_CHAT]
    usual = med([t[3] for x in chats for t in x["turns"]])
    fresh = med([t[3] for x in chats if x["started"] >= d30["since"] for t in x["turns"][:3]])
    out = []
    for sid, x in d30["sess"].items():
        if now - x["last"] > 1200:
            continue
        title, path = x["title"] or tr("untitled"), "/s/" + quote(sid)
        if x["src"] == "cron":
            runs = [y["cost"] for y in d30["sess"].values() if y["origin"] == x["origin"] and y["started"] < x["started"]]
            m = med(runs) if len(runs) >= 3 else 0.0
            if s["alert_spike"] and x["cost"] * k >= max(3 * m * k, s["spike_pct"]) and f"spike:{sid}" not in sent:
                text = tr("alert.spike.cron", name=x["origin"][5:] or tr("untitled"), x=pct(x["cost"] * k, 100))
                out.append((f"spike:{sid}", tr("alert.spike"), text + (" " + tr("alert.spike.usual", m=pct(m * k, 100)) if m else ""), path))
            continue
        if x["src"] in NOT_CHAT or not x["turns"]:
            continue
        t0, _, n, c = x["turns"][-1]
        if s["alert_spike"] and c * k >= max(3 * usual * k, s["spike_pct"]) and f"spike:{sid}:{t0:.0f}" not in sent:
            out.append((f"spike:{sid}:{t0:.0f}", tr("alert.spike"), tr("alert.spike.turn", title=title, n=n, x=pct(c * k, 100)), path))
        done = x["turns"][:-1][-3:]   # ponytail: the newest turn may still run, it counts once the next message came
        per = sum(t[3] for t in done) / 3 * k if len(done) == 3 else 0.0
        if s["alert_chat"] and per >= s["chat_pct"] and f"chat:{sid}" not in sent:
            out.append((f"chat:{sid}", tr("alert.chat"), tr("alert.chat.text", title=title, x=pct(per, 100), y=pct(fresh * k, 100)), path))
    return out


PRIO = {"free": 3, "seven_day@80": 3, "digest": 2, "chat": 3}   # ntfy priority by alert kind, everything else 4 (high)


def check_alerts():
    f = DATA / "alerts.json"
    try:
        sent = json.loads(f.read_text())
    except (OSError, ValueError):
        sent = {}
    if not STATE["limits"] or quiet_now(time.time()):  # in quiet hours due alerts wait, nothing is marked as sent
        return
    now = time.time()
    msgs, new = alerts(STATE["limits"], limit_history(), sent, now)
    msgs += [x for x in [digest(now, sent, STATE["limits"])] if x]
    if RATE.get("k") and (SET["alert_chat"] or SET["alert_spike"]):
        msgs += watch(data_for("30"), now, sent, RATE["k"])
    for tag, title, text, *path in msgs:
        if notify(title, text, PRIO.get(tag.split(":")[0], 4), *path):
            new[tag] = time.time()
    if new != sent:
        DATA.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(new))


def snap_path():
    h = source_home()
    return DATA / ("snapshot.json" if h == HERMES else f"snapshot-{h.name}.json")


def load_snap():
    try:
        return json.loads(snap_path().read_text())
    except (OSError, ValueError):
        return {}


STATUS_URL = "https://status.claude.com"
STATUS = {}


def refresh_status():
    """Claude's public status page; an incident shows up as a line under the limits."""
    try:
        with urllib.request.urlopen(STATUS_URL + "/api/v2/status.json", timeout=15) as r:
            st = json.loads(r.read())["status"]
        STATUS.clear(); STATUS.update(st)
    except (OSError, ValueError, KeyError):
        STATUS.clear()


def ensure_snap():
    """Measures prompt and tools again when the snapshot of the analysed Hermes is missing or a day old."""
    snap = snap_path()
    if not snap.exists() or time.time() - snap.stat().st_mtime > 86400:
        try:
            subprocess.run([str(HERMES_PY), str(Path(__file__).resolve()), "--snapshot"], cwd=HERMES / "hermes-agent",
                           capture_output=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as e:
            print("snapshot:", e, flush=True)


def background():
    while True:
        ensure_snap()
        refresh_limits(540)
        check_alerts()
        guard()
        refresh_status()
        watch_config()
        if SET["currency"] == "EUR":
            refresh_fx()
        time.sleep(600)


def snapshot():
    """Runs in the Hermes venv: measures system prompt parts and tool schemas as Hermes currently sends them.
    It only builds Hermes' agent object and reads its prompt and tool list; no request goes to a model. The platform
    is the one most sessions come from, because the tool selection depends on it."""
    import logging
    logging.disable(logging.WARNING)
    sys.path.insert(0, str(HERMES / "hermes-agent"))
    os.chdir(HERMES / "hermes-agent")
    os.environ["USAGECAST_ROOT"] = str(HERMES)
    os.environ["HERMES_HOME"] = str(source_home())   # a profile loads its own config, tools and SOUL.md
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
    try:  # ponytail: private Hermes helper; if it moves, the unused-tools tip just loses its command
        from hermes_cli.config import load_config_readonly
        from hermes_cli.tools_config import _get_platform_tools
        from toolsets import resolve_toolset
        on = {k: set(resolve_toolset(k)) for k in _get_platform_tools(load_config_readonly(), platform)}
        sets = {n: sorted(k for k, ts in on.items() if n in ts or n.startswith(f"mcp__{k.replace('-', '_')}__"))
                for n in tools}   # toolsets enabled on this platform that hold the tool
    except Exception:  # noqa: BLE001
        sets = {}
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = snap_path().with_suffix(".tmp")
    tmp.write_text(json.dumps({"prompt": prompt, "tools": tools, "sets": sets, "platform": platform, "at": time.time()}))
    tmp.replace(snap_path())


# ---------- HTML ----------
e = html.escape
STATIC = ROOT / "static"
STATIC_FILES = {"style.css": "text/css; charset=utf-8", "haptics.js": "text/javascript; charset=utf-8", "manifest.webmanifest": "application/manifest+json",
                "icon.svg": "image/svg+xml", "icon-180.png": "image/png", "icon-192.png": "image/png", "icon-512.png": "image/png"}
ICONS = {  # 24 grid, stroke in currentColor
    "/": '<path d="M4 16a8 8 0 1 1 16 0"/><path d="M12 16l3.5-4.5"/>',
    "/history": '<path d="M5 19v-7M10 19V6M15 19v-4M20 19V9"/>',
    "/details": '<path d="M9 7h11M9 12h11M9 17h11M4.5 7h.01M4.5 12h.01M4.5 17h.01"/>',
    "/sessions": '<path d="M20 14.5a2 2 0 0 1-2 2H8.5L4 20V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z"/>',
    "/settings": '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
    "/projects": '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.8l2 2h7.2a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
}
NAV = (("nav.summary", (("/", "nav.overview"), ("/history", "nav.history"))),
       ("nav.breakdown", (("/details", "nav.details"), ("/sessions", "nav.sessions"), ("/projects", "nav.projects"))))
ALIASES = {"/verlauf": "/history", "/projekte": "/projects"}  # paths of the first, German-only version
LOGO = ('<svg viewBox="0 0 24 24" aria-hidden="true"><path class="lt" d="M5 16a7 7 0 0 1 14 0"/>'
        '<path class="la" d="M5 16a7 7 0 0 1 10.5-6.06"/><path class="ln" d="M12 16l2.6-3"/></svg>')
COLORS = 5  # colored components in the stacked history, plus "rest"


def brk(s):
    """Escape and let long names like memory_tencentdb_conversation_search wrap at _ and /."""
    return e(s).replace("_", "_<wbr>").replace("/", "/<wbr>")


def link(path, **q):
    q = urlencode({k: v for k, v in q.items() if v})
    return path + ("?" + q if q else "")


def chip(text, on, href):
    return f'<a href="{href}"{" class=on aria-current=true" if on else ""}>{text}</a>'


def group(k):
    return "tool:skill_view" if k.startswith("skill:") else k


def table(head, rows, cls=(), tcls=""):
    """head: locale keys. cls: CSS classes per column, l = left aligned, o = hidden on phones."""
    c = lambda i: f' class="{cls[i]}"' if i < len(cls) and cls[i] else ""
    if SET["cost_unit"] == "week" and RATE.get("k"):   # ponytail: text replace on money()'s output, a short money() if this breaks
        head = ["th.cost.wk" if x == "th.cost" else x for x in head]
        unit, short = tr("fmt.week", v=""), tr("fmt.pct", v="")
        rows = [[str(x).replace(unit, short) for x in r] for r in rows]
    h = "".join(f"<th{c(i)}>{e(tr(x))}</th>" for i, x in enumerate(head))
    b = "".join("<tr>" + "".join(f"<td{c(i)}>{x}</td>" for i, x in enumerate(r)) + "</tr>" for r in rows) \
        or f'<tr><td colspan="{len(head)}">{tr("nodata")}</td></tr>'
    return f'<div class="scroll"><table{f" class={tcls}" if tcls else ""}><tr>{h}</tr>{b}</table></div>'


def bars(items, tot, ttl, explain=True, n=12, p=None, soft=False, name_of=None):
    if not items:
        return f'<p class="hint">{tr("nodata")}</p>'
    name_of = name_of or (lambda k: label(k, ttl))
    top = items[0][1] or 1
    out = ""
    for k, v in items[:n]:
        name, why = name_of(k)
        head = (f'<div class="row"><span>{brk(name)}</span><span class="v">{money(v) if SET["cost_unit"] == "week" and RATE.get("k") else f"{pct(v, tot)} · {money(v)}"}</span></div>'
                f'<div class="t{" soft" if soft else ""}"><i style="width:{v / top * 100:.1f}%"></i></div>')
        if explain and why and (k in FIXED or not k.startswith("tool:")):   # the sentence for tools would repeat on every tool
            out += f'<details class="bar"><summary>{head}</summary><div class="why">{e(why)}</div></details>'
        else:
            out += f'<div class="bar">{head}</div>'
    rest = sum(v for _, v in items[n:])
    if rest:
        more = f'<a href="{link("/details", p=p)}">{tr("nav.details")}</a>' if p else tr("nav.details")
        out += f'<p class="hint">{tr("bars.rest", n=len(items) - n, p=pct(rest, tot), details=more)}</p>'
    return out


def dur(s):
    """Rough duration: ~59 min, ~1 h 20 min."""
    m = max(round(s / 60), 1)
    return tr("dur.min", m=m) if m < 60 else tr("dur.hm", h=m // 60, m=m % 60)


def until(t):
    if not t:
        return ""
    s = t - time.time()
    if s < 3600:
        return tr("until.min", m=max(int(s // 60), 1))
    if s < 86400:
        return tr("until.hm", h=int(s // 3600), m=int(s % 3600 // 60))
    return tr("until.day", at=when(t, "daytime"))


def meter(u, frac=None, hot=False, left=False):
    """Bar with a mark where the time stands. left: shows what is left, bar and mark both run down."""
    pos = 1 - frac if left and frac is not None else frac
    mark = (f'<b class="now" style="left:{pos * 100:.1f}%" title="{tr("lim.elapsed", p=pc(frac * 100))}"><span class="sr">{tr("lim.elapsed", p=pc(frac * 100))}</span></b>'
            if frac is not None else "")
    w = 100 - u if left else u
    return f'<div class="meter{" hot" if hot else ""}"><i style="width:{min(max(w, 0), 100):.1f}%"></i>{mark}</div>'


def api_summary():
    """Compact JSON for widgets: limits with reset times, the week forecast and this limit week's cost."""
    fresh_limits()
    L, now = STATE["limits"] or {}, time.time()
    out = {"at": round(STATE["ok"]) or None, "limits": {}}
    for key, u, frac, reset, length in windows(L, now):
        out["limits"][key] = {"used": u, "resets_at": round(reset) if reset else None,
                              "elapsed": round(frac, 3) if frac is not None else None}
    w = out["limits"].get("seven_day")
    if w and w["elapsed"] is not None:
        w["forecast"] = week_forecast(w["used"], w["elapsed"])
    x = L.get("extra_usage") or {}
    if x.get("is_enabled"):
        out["extra"] = {k: x.get(k) for k in ("used_credits", "monthly_limit", "currency", "decimal_places")}
        dp, m = 10 ** (x.get("decimal_places") or 0), extra_months(limit_history(400))
        out["extra"].update(used=(x.get("used_credits") or 0) / dp, limit=(x.get("monthly_limit") or 0) / dp,
                            month_start=round(m[-1][0]) if m else None)
        est = overrun_usd((w or {}).get("forecast"))
        if est is not None:
            out["extra"]["estimate_usd"] = round(est, 2)   # credits the week would need at this pace
    d = data_for("w")
    out["week_cost_usd"] = round(d["total"], 2) if d["week"] else None
    return json.dumps(out)


def limits_card(week_cost, split=None):
    fresh_limits()
    L, now = STATE["limits"], time.time()
    if not L:
        return f'<section class="card"><h2>{tr("lim.title")}</h2><p class="hint">{tr("lim.unavailable")}</p></section>'
    hist, left, right = limit_history(), "", ""
    lv = SET["limit_view"] == "left"
    for key, u, frac, reset, length in windows(L, now):
        start = reset - length if reset else now
        if key == "seven_day":
            fc = week_forecast(u, frac)
            if u >= 100:
                txt = tr("lim.full", reset=until(reset))
            elif fc is None:
                txt = tr("lim.young", reset=until(reset))
            elif fc > 100:
                txt = tr("lim.week.over", full=when(start + (now - start) * 100 / u, "daytime"), reset=until(reset))
            else:
                txt = tr("lim.week.lands", fc=tr("lim.left", p=pc(100 - fc)) if lv else pc(fc), frac=pc(frac * 100), reset=until(reset))
            left = (f'<div class="lbl">{tr("lim.week")}</div><div class="big">{nf(100 - u if lv else u)}<span>{tr("lim.unit.left" if lv else "lim.unit")}</span></div>'
                    f'{meter(u, frac, u >= 80 or (fc or 0) > 100, lv)}<p class="fc">{txt}</p>')
            if u < 100 and reset:
                # ponytail: even split of what is left; "left today" would need the limit value at midnight
                left += f'<p class="why">{tr("lim.budget", pct=pc((100 - u) / max((reset - now) / 86400, 1)))}</p>'
            more = ""
            if week_cost is not None:
                more += f'<p class="why">{tr("lim.weekcost", cost=cash(week_cost))}</p>'
            if split and split["hermes"] + split["cc"] + split["rest"] >= 1:
                parts = {k: pc(split[k]) for k in ("hermes", "cc", "rest")}
                extra = "".join(" " + tr("lim.split." + k, p=pc(split[k])) for k in ("before", "gap") if split[k] >= 1)
                more += (f'<p class="why">{tr("lim.split" if CLAUDE.is_dir() or ingest_hosts() else "lim.split.nocc", start=when(split["start"], "daytime"), **parts)}'
                         f'{extra}</p>')
            hosts = ingest_hosts()
            if hosts:
                more += f'<p class="why">{tr("lim.hosts", hosts=", ".join(f"{e(h)} ({when(x['at'], 'daytime')})" for h, x in hosts.items()))}</p>'
            if FC.get("method"):
                gain = pc((1 - FC["wtd"] / FC["lin"]) * 100) if FC["lin"] else pc(0)
                more += f'<p class="why">{tr("lim.fc." + FC["method"], gain=gain, lin=pc(FC["lin"]), wtd=pc(FC["wtd"]))}</p>'
            if RATE.get("k"):
                more += f'<p class="why">{tr("lim.rate", cost=cash(1 / RATE["k"]), n=RATE["hours"])}</p>'
            if more:
                left += f'<details class="more"><summary>{tr("lim.more")}</summary>{more}</details>'
            continue
        hot = u >= 80
        if u >= 100:
            txt = tr("lim.full", reset=until(reset))
        elif length <= 5 * 3600:
            r = rate(hist, key, u, start, now) if reset else None
            fa = now + (100 - u) / r if r else None
            if fa and fa < reset:
                txt, hot = tr("lim.five.full_at", left=dur(fa - now), full=hm(fa), reset=until(reset)), hot or fa - now < 1800
            elif r is not None:
                at = min(u + r * (reset - now), 100)
                txt = tr("lim.five.enough", reset=hm(reset), at=tr("lim.left", p=pc(100 - at)) if lv else pc(at))
            else:
                txt = until(reset)
        else:
            fc = week_forecast(u, frac)
            txt = (tr("lim.forecast", fc=pc(fc)) + " " if fc else "") + until(reset)
        right += (f'<div class="lim"><div class="row"><span>{tr("win." + key)}</span><b>{tr("lim.left", p=pc(100 - u)) if lv else pc(u)}</b></div>'
                  f'{meter(u, frac, hot, lv)}<div class="why">{txt}</div></div>')
    x = L.get("extra_usage") or {}
    if x.get("is_enabled") and x.get("monthly_limit"):
        dp, used = 10 ** (x.get("decimal_places") or 0), x.get("used_credits") or 0
        value = tr("lim.extra.value", used=nf(used / dp, 2), limit=nf(x["monthly_limit"] / dp, 2), cur=e(x.get("currency") or ""))
        wk = next((w for w in windows(L, now) if w[0] == "seven_day"), None)
        fc = week_forecast(wk[1], wk[2]) if wk else None
        est = overrun_usd(fc)
        why = tr("lim.extra.pace", over=pc(fc - 100), cost=cash(est)) if est is not None else tr("lim.extra.why")
        # ponytail: reads the whole limits log on every overview; cache by mtime if it ever gets slow
        past = "".join(f'<p class="why">{tr("lim.extra.month", start=when(t, "day"), used=nf(u, 2), cur=e(x.get("currency") or ""))}</p>'
                       for t, u in reversed(extra_months(limit_history(400))[:-1]))
        past = f'<details class="more"><summary>{tr("lim.extra.past")}</summary>{past}</details>' if past else ""
        right += (f'<div class="lim"><div class="row"><span>{tr("lim.extra")}</span><b>{value}</b></div>'
                  f'{meter(used / x["monthly_limit"] * 100)}<div class="why">{why}</div>{past}</div>')
    spark = ""
    if len(hist) >= 3:
        t0, t1 = hist[0]["t"], hist[-1]["t"]
        line = lambda k: " ".join(f"{(h['t'] - t0) / ((t1 - t0) or 1) * 100:.2f},{100 - min(h.get(k) or 0, 100):.1f}" for h in hist)
        spark = (f'<div class="trend"><svg class="spark" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">'
                 f'<polyline class="f" points="{line("five_hour")}"/><polyline class="w" points="{line("seven_day")}"/></svg>'
                 f'<div class="axis"><span>{tr("lim.trend", at=when(t0, "daytime"))}</span>'
                 f'<span><span class="dot c0"></span>{tr("lim.trend.week")}<span class="dot f"></span>{tr("lim.trend.five")}</span></div></div>')
    left = left or f'<div class="lbl">{tr("lim.week")}</div><p class="hint">{tr("nodata")}</p>'
    stale = now - STATE["ok"] > 1800
    stamp = tr("lim.stale", at=when(STATE["ok"], "daytime")) if stale else tr("lim.stamp", at=hm(STATE["ok"]))
    if STATUS.get("indicator") not in (None, "none"):
        stamp += "<br>" + tr("lim.status", desc=html.escape(STATUS.get("description", "")), url=STATUS_URL)
    return (f'<section class="card hero" aria-label="{tr("lim.title")}"><div>{left}</div><div>{right}'
            f'<p class="why">{stamp}</p></div>{spark}</section>')


def layout(title, active, p, h1, sub, body, tabs=True, keep=None):
    """h1 and sub are already escaped. keep: filters that survive a period switch."""
    nav = "".join(f'<div class="grp">{tr(g)}</div>' + "".join(
        f'<a href="{link(href, p=p)}"{" class=on aria-current=page" if href == active else ""}>'
        f'<svg viewBox="0 0 24 24" aria-hidden="true">{ICONS[href]}</svg>{tr(name)}</a>' for href, name in items) for g, items in NAV)
    on = active == "/settings"
    nav += (f'<a class="end{" on" if on else ""}" href="/settings"{" aria-current=page" if on else ""}>'
            f'<svg viewBox="0 0 24 24" aria-hidden="true">{ICONS["/settings"]}</svg>{tr("nav.settings")}</a>')
    seg = "".join(chip(tr("period." + k), k == p, link(active, p=k, **(keep or {}))) for k in PERIODS)
    a, b = custom(p) or (time.time() - 6 * 86400, time.time() + 1)
    iso = lambda t: datetime.fromtimestamp(t).date().isoformat()
    today, hidden = iso(time.time()), "".join(f'<input type="hidden" name="{e(k)}" value="{e(v)}">' for k, v in (keep or {}).items())
    rng = (f'<details class="range{" on" if custom(p) else ""}"><summary>{e(pname(p)) if custom(p) else tr("range.custom")}</summary>'
           f'<form method="get" action="{active}">{hidden}<input type="date" name="from" value="{iso(a)}" max="{today}" aria-label="{tr("range.from")}" required>'
           f'<span>–</span><input type="date" name="to" value="{iso(b - 3600)}" max="{today}" aria-label="{tr("range.to")}" required>'
           f'<button class="btn primary">{tr("range.go")}</button></form></details>')
    seg = f'<div class="periods"><nav class="seg" aria-label="{tr("aria.period")}">{seg}</nav>{rng}</div>' if tabs else ""
    foot = (tr("foot.limits", at=hm(STATE["ok"]) if STATE["ok"] else "–") + "<br>"
            + f'<a href="/settings#push">{tr("foot.ntfy.on" if ntfy_target() else "foot.ntfy.off")}</a>')
    th = SET["theme"]
    metas = "".join(f'<meta name="theme-color" content="{c}"' + (f' media="(prefers-color-scheme: {m})"' if th == "system" else "") + ">"
                    for m, c in (("light", "#f7f6f4"), ("dark", "#141312")) if th in ("system", m))
    u = urlparse(getattr(_req, "url", "/"))
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    langs = []
    for code in LOC:
        q["lang"] = code
        on = " class=on aria-current=true" if code == lang() else ""
        langs.append(f'<a href="{e(link(u.path, **q))}" hreflang="{code}" lang="{code}"{on}>{LOC[code]["lang.name"]}</a>')
    try:
        v = int(max((STATIC / n).stat().st_mtime for n in ("style.css", "haptics.js")))
    except OSError:
        v = 0
    theme = f' data-theme="{th}"' if th != "system" else ""
    return f"""<!doctype html><html lang="{lang()}"{theme}><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,minimum-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover"><title>{e(title)}</title>
<script>for(const t of["gesturestart","gesturechange"])addEventListener(t,e=>e.preventDefault())</script>
<script type="module" src="/haptics.js?v={v}"></script>
<link rel="stylesheet" href="/style.css?v={v}"><link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/icon-180.png"><link rel="manifest" href="/manifest.webmanifest">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Usagecast">
{metas}</head>
<body><div class="app"><aside><a class="brand" href="{link("/", p=p)}"><span class="logo">{LOGO}</span><span>Usagecast</span></a>
<nav aria-label="{tr("aria.pages")}">{nav}</nav><div class="foot">{foot}</div></aside>
<main><header class="top"><div><h1>{h1}</h1><p class="sub">{sub}</p></div>{seg}</header>
{body}
<footer class="pf"><nav aria-label="{tr("aria.lang")}">{" · ".join(langs)}</nav><a href="{REPO_URL}">Usagecast {VERSION}</a></footer></main></div></body></html>"""


CACHE, LOCK = {}, threading.Lock()


def connect():
    return sqlite3.connect(f"file:{source_home() / 'state.db'}?mode=ro", uri=True, timeout=15)


RANGE_RX = re.compile(r"(\d{4}-\d\d-\d\d)\.\.(\d{4}-\d\d-\d\d)")


def custom(p):
    """(start, end) of a custom period like "2026-10-01..2026-10-07" (both days included), otherwise None."""
    try:
        a, b = sorted(datetime.fromisoformat(x) for x in RANGE_RX.fullmatch(p or "").groups())
    except (AttributeError, ValueError):
        return None
    return a.timestamp(), (b + timedelta(days=1)).timestamp()


def pname(p):
    """Name of a period: "Limit week", "7 days", or the dates of a custom one."""
    if p in PERIODS:
        return tr("period." + p)
    a, b = custom(p)
    return when(a, "day") if b - a <= 86400 + 3600 else f"{when(a, 'day')} – {when(b - 3600, 'day')}"


def since_for(p):
    """(start of the period, whether it really is the limit week)."""
    if p == "w":
        fresh_limits()
        try:
            return datetime.fromisoformat(STATE["limits"]["seven_day"]["resets_at"]).timestamp() - 7 * 86400, True
        except (KeyError, TypeError, ValueError):
            pass
    if custom(p):
        return custom(p)[0], False
    return time.time() - PERIODS[p] * 86400, False


REFRESHING = set()


def compute(p):
    who = SET["source"]
    try:
        since, week = since_for(p)
        d = analyze(connect(), since, load_snap(), cache_ttl(), until=(custom(p) or (0, math.inf))[1])
        d.update(week=week, p=p, at=time.time())
        if SET["source"] != who:
            return d
        if p == "30":
            r = limit_split(limit_history(30), d["hours"], cc_calls(since), since) or {}
            k, (ws, wk) = r.get("k"), since_for("w")
            u, cw = ((STATE["limits"] or {}).get("seven_day") or {}).get("utilization"), sum(
                sum(v.values()) for h, v in d["hours"].items() if h >= ws)
            if k and wk and u and cw:
                k = min(k, u / cw)   # ponytail: the rate drifts; never let Hermes' week cost exceed the measured week
            RATE.clear()
            RATE.update(k=k, hours=r.get("hours", 0))
            if wk:
                hc = defaultdict(float)
                for h, v in d["hours"].items():
                    hc[h] += sum(v.values())
                for t, c in cc_calls(since):
                    hc[t - t % 3600] += c
                prof, bt = rhythm(hc, round(ws / 3600) * 3600, since)
                FC.clear()
                if prof:   # the rhythm goes live only when it beat the straight line by at least 10 % in the backtest
                    FC.update(prof=prof, method="weighted" if bt["wtd"] <= 0.9 * bt["lin"] else "linear", lin=round(bt["lin"], 1),
                              wtd=round(bt["wtd"], 1))
                    keep = {k: FC[k] for k in ("method", "lin", "wtd")}
                    if (read_json(DATA / "forecast.json") or {}).get("method") != keep["method"]:
                        DATA.mkdir(parents=True, exist_ok=True)
                        (DATA / "forecast.json").write_text(json.dumps({**keep, "at": round(time.time())}))
        with LOCK:
            for k in [k for k in CACHE if custom(k) and k != p]:
                CACHE.pop(k)   # ponytail: keep only the latest custom period in memory
            CACHE[p] = (time.time(), d)
        return d
    finally:
        with LOCK:
            REFRESHING.discard(p)


def data_for(p):
    """Fresh cache (< 120 s) as is; a stale one is served at once and recomputed in the background."""
    with LOCK:
        hit = CACHE.get(p)
        if hit and (time.time() - hit[0] < 120 or p in REFRESHING):
            return hit[1]
        if hit:
            REFRESHING.add(p)
    if hit:
        threading.Thread(target=compute, args=(p,), daemon=True).start()
        return hit[1]
    return compute(p)


def warm():
    """Keeps the periods of the overview precomputed, so no page load waits for the analysis."""
    while True:
        for p in dict.fromkeys(("w", "30", SET["period"])):
            try:
                compute(p)
            except Exception as e:  # noqa: BLE001 - keep warming, the page computes on demand anyway
                print("warm:", e, flush=True)
        time.sleep(300)


def period_text(d):
    if custom(d["p"]):
        return pname(d["p"])
    if d["week"]:
        return tr("since.w", at=when(d["since"], "daytime"))
    return tr("since." + (d["p"] if d["p"] in ("1", "30") else "7"))


def window_sum(d, since):
    h0 = since - since % 3600
    return sum(sum(v.values()) for h, v in d["hours"].items() if h >= h0)


def span_sum(d, a, b):
    a = round(a / 3600) * 3600   # limit resets land a few ms off the full hour; the hour buckets start on it
    return sum(sum(v.values()) for h, v in d["hours"].items() if a <= h < b)


def signed(cur, prev):
    """+18 % / −7 % / ±0 %: change against an earlier value."""
    d = abs(cur / prev - 1) * 100
    return ("±" if d < 0.5 else "+" if cur >= prev else "−") + pc(d)


def week_delta(d30, cur, since, now):
    """"+18 % vs. last week" for a stretch since `since`, against the same stretch seven days earlier."""
    prev = span_sum(d30, since - 7 * 86400, now - 7 * 86400)
    return tr("kpi.delta", d=signed(cur, prev)) if prev > 0.01 and since >= now - 7.01 * 86400 else ""


def per_day(d):
    """{day start: [{component: $}, steps, {sessions}]}"""
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
    dl = lambda v, since: f'<small class="d">{x}</small>' if (x := week_delta(d30, v, since, now)) else ""
    kpis = [("main", pname(p), money(d["total"]), tr("kpi.main.sub", tokens=num(d["tokens"]), steps=cnt(d["calls"]))
             + (dl(d["total"], d["since"]) if p in ("w", "7") else ""))]
    kpis += [("", tr("period." + k), money(sums[k]), tr("kpi.api") + (dl(sums[k], now - 7 * 86400) if k == "7" else ""))
             for k in [k for k in ("7", "30", "1") if k != p][:2]]
    kpis += [("", tr("kpi.avg"), money(d30["total"] / max(len(active), 1)), tr("kpi.avg.sub", n=len(active))),
             ("", tr("kpi.sessions"), cnt(len(d["sess"])), tr("kpi.sessions.sub"))]
    kpi_html = "".join(f'<div class="kpi {c}"><span>{l}</span><b>{v}</b><small>{s}</small></div>' for c, l, v, s in kpis)
    tip_html = "".join(f'<div class="tip"><b>{e(t)}</b><p>{txt}</p></div>' for _, t, txt in tips(d, snap)) \
        or f'<p class="hint">{tr("ov.nothing")}</p>'
    where = sorted(d["where"].items(), key=lambda x: -x[1])
    models = sorted(d["models"].items(), key=lambda x: -x[1][2])
    models_html = '<ol class="rank">' + "".join(
        f'<li><span class="n">{i}</span><span class="m">{brk(m)}{"" if known_model(m) else " <small>" + tr("ov.estimated") + "</small>"}</span><span class="v">{money(x[2])}</span><b>{pct(x[2], tot)}</b></li>'
        for i, (m, x) in enumerate(models, 1)) + "</ol>"
    logged = tr("ov.logged", p=pc(d["logged"] / (d["n_steps"] or 1) * 100))
    push = "" if ntfy_target() else f'<p class="hint pushoff">{tr("ov.push", url="/settings#push")}</p>'
    split = limit_split(limit_history(), d30["hours"], cc_calls(w["since"]), w["since"]) if w["week"] else None
    body = f"""{limits_card(w["total"] if w["week"] else None, split)}{guard_note()}{push}
<div class="kpis">{kpi_html}</div>
<div class="grid g2">
<section class="card"><h2>{tr("ov.eats")}</h2>
<p class="hint">{tr("ov.eats.hint")}</p>
{bars(ranked(d["comp"]), tot, ttl, n=10, p=p)}
<p class="hint">{logged}</p></section>
<div class="col"><section class="card"><h2>{tr("ov.where")}</h2>{bars(where, tot, ttl, explain=False, n=8, soft=True, name_of=lambda k: (origin_label(k), ""))}</section>
<section class="card"><h2>{tr("ov.models")}</h2>{models_html}</section></div></div>
<section class="sec"><h2>{tr("ov.save")}</h2>
<p class="hint">{tr("ov.save.hint")}</p>
<div class="tips">{tip_html}</div></section>"""
    return layout("Usagecast", "/", p, tr("ov.h1"), tr("sub.calc", src=source_name(), period=period_text(d), at=hm(d["at"])), body)


WATCH = (("model", "default"), ("model", "provider"), ("agent", "reasoning_effort"), ("prompt_caching", "cache_ttl"),
         ("memory", "provider"))   # config values that change what a step costs, plus the plugin list


def read_json(f):
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return None


def read_jsonl(f):
    try:
        return [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
    except (OSError, ValueError):
        return []


def watch_config():
    """Appends the cost-relevant config to data/config.jsonl whenever it changed (the first line is the baseline)."""
    text = config_text()
    cfg = {f"{s}.{k}": config_value(s, k, text) for s, k in WATCH}
    cfg["plugins"] = ", ".join(plugin_names(text))
    rows = read_jsonl(DATA / "config.jsonl")
    last = rows[-1]["cfg"] if rows else None
    if cfg != last:
        changes = {k: [last.get(k, ""), v] for k, v in cfg.items() if last.get(k, "") != v} if last else {}
        DATA.mkdir(parents=True, exist_ok=True)
        with open(DATA / "config.jsonl", "a") as fh:
            fh.write(json.dumps({"t": round(time.time()), "cfg": cfg, "changes": changes}) + "\n")


def change_text(k, a, b):
    if k == "plugins":
        old, new = set(filter(None, a.split(", "))), set(filter(None, b.split(", ")))
        return "plugins " + " ".join([*(f"+{x}" for x in sorted(new - old)), *(f"−{x}" for x in sorted(old - new))])
    return f"{k.split('.')[-1]} {a or '–'} → {b or '–'}"


def events(since, until=math.inf):
    """[(time, text)]: Hermes updates (pull/merge in the install's git reflog) and the config changes watch_config saw."""
    out = []
    try:
        r = subprocess.run(["git", "-C", str(HERMES / "hermes-agent"), "reflog", "--date=unix", "--format=%gd %gs"],
                           capture_output=True, text=True, timeout=10)
        out += [(int(m[1]), tr("ev.update")) for m in re.finditer(r"@\{(\d+)\} (?:pull|merge|rebase \(finish\))", r.stdout)]
    except (OSError, subprocess.TimeoutExpired):
        pass
    out += [(x["t"], tr("ev.config", what=", ".join(change_text(k, a, b) for k, (a, b) in x["changes"].items())))
            for x in read_jsonl(DATA / "config.jsonl") if x.get("changes")]
    return sorted(x for x in out if since <= x[0] < until)


def per_step(d30, a, b):
    """Average API value per step between a and b; None below 20 steps."""
    a = round(a / 3600) * 3600
    steps = sum(n for h, n in d30["hsteps"].items() if a <= h < b)
    return span_sum(d30, a, b) / steps if steps >= 20 else None


def event_list(d, d30):
    now, rows = time.time(), ""
    for t, text in events(d["since"], min(d["until"], now)):
        a, b = per_step(d30, t - 7 * 86400, t), per_step(d30, t, min(t + 7 * 86400, now))
        cmp = tr("ev.cmp", a=money(a), b=money(b), d=signed(b, a)) if a and b else tr("ev.early")
        rows += f'<li><b>{when(t, "daytime")}</b> {e(text)}<span class="why">{cmp}</span></li>'
    return f'<p class="hint">{tr("ev.hint")}</p><ul class="evs">{rows}</ul>' if rows else ""


def stacked(d):
    """Stacked bars per day (per hour for short periods): the biggest components in color, everything else as rest."""
    hourly, ttl = d["hourly"], d["ttl"]
    top = [k for k, _ in ranked(d["comp"])[:COLORS]]
    cols = defaultdict(lambda: defaultdict(float))
    for h, comp in d["hours"].items():
        b = floor(h, hourly)
        for k, v in comp.items():
            cols[b][group(k) if group(k) in top else "rest"] += v
    keys, t, step = [], floor(d["since"], hourly), 3600 if hourly else 86400
    while t <= min(time.time(), d["until"] - 1):
        keys.append(t)
        t = floor(t + step + (0 if hourly else 7200), hourly)  # +2 h survives daylight saving changes
    if not keys or not d["total"]:
        return f'<p class="hint">{tr("nodata")}</p>'
    names = [label(k, ttl)[0] for k in top] + [tr("rest")]
    tl = lambda k: tr("fmt.hour", h=datetime.fromtimestamp(k).hour) if hourly else when(k, "day")
    sums = [sum(cols[k].values()) for k in keys]
    mx, w, svg = max(sums), 100 / len(keys), ""
    bw = min(w * .66, 4.5)  # no blocks when there are only a few days
    for i, (k, s) in enumerate(zip(keys, sums)):
        y, rects, tip = 100.0, "", f"{tl(k)}: {money(s)}"
        for j, c in enumerate(top + ["rest"]):
            v = cols[k].get(c, 0.0)
            if v > 0:
                y -= v / mx * 96
                rects += f'<rect class="c{j}" x="{i * w + (w - bw) / 2:.2f}" y="{y:.2f}" width="{bw:.2f}" height="{v / mx * 96:.2f}"/>'
                tip += f"\n{names[j]}: {money(v)}"
        svg += f'<g><title>{e(tip)}</title><rect class="hit" x="{i * w:.2f}" y="0" width="{w:.2f}" height="100"/>{rects}</g>'
    step = 3600 if hourly else 86400
    for t, text in events(keys[0], min(d["until"], time.time())):   # dashed line per update / config change
        i = max(bisect.bisect_right(keys, t) - 1, 0)
        x = (i + min((t - keys[i]) / step, 1)) * w
        svg += f'<line class="ev" x1="{x:.2f}" x2="{x:.2f}" y1="0" y2="100"><title>{e(when(t, "daytime") + " · " + text)}</title></line>'
    peak = max(range(len(keys)), key=lambda i: sums[i])
    ax = [tl(keys[i]) for i in (0, len(keys) // 2, -1)]
    legend = "".join(f'<span><span class="dot c{j}"></span>{brk(n)}</span>' for j, n in enumerate(names))
    return (f'<svg class="chart" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" '
            f'aria-label="{tr("hist.per.hour" if hourly else "hist.per.day")}">{svg}</svg>'
            f'<div class="axis"><span>{ax[0]}</span><span>{ax[1]}</span><span>{ax[2]}</span></div>'
            f'<div class="lg">{legend}</div><p class="hint">{tr("hist.peak", at=tl(keys[peak]), v=money(sums[peak]))}</p>')


def heatmap(d):
    heat, wd_tot = defaultdict(float), defaultdict(float)
    for h, comp in d["hours"].items():
        lt = time.localtime(h)
        heat[(lt.tm_wday, lt.tm_hour)] += sum(comp.values())
        wd_tot[lt.tm_wday] += sum(comp.values())
    if not heat:
        return f'<p class="hint">{tr("nodata")}</p>'
    wds, mx, cells = LOC[lang()]["weekdays"], max(heat.values()) or 1, ""
    for wd in range(7):
        cells += f"<span>{wds[wd]}</span>"
        for hr in range(24):
            v = heat.get((wd, hr), 0.0)
            lvl = 1 + min(int(math.sqrt(v / mx) * 4), 3) if v > 0 else 0  # square root, so small values stay visible
            cells += f'<i class="h{lvl}" title="{tr("heat.cell", wd=wds[wd], h=hr, h2=hr + 1, v=money(v))}"></i>'
    cells += "<span></span>" + "".join(f'<span class="hx">{tr("fmt.hour", h=hr)}</span>' for hr in (0, 6, 12, 18))
    (pw, ph), pv = max(heat.items(), key=lambda x: x[1])
    bw = max(wd_tot, key=wd_tot.get)
    scale = "".join(f'<i class="h{i}"></i>' for i in range(5))
    return (f'<div class="heat">{cells}</div><div class="scale">{tr("heat.less")}{scale}{tr("heat.more")}</div>'
            f'<p class="hint">{tr("heat.peak", wd=wds[pw], h=ph, h2=ph + 1, v=money(pv), bwd=wds[bw], bv=money(wd_tot[bw]))}</p>')


CAL = {}   # day_costs() cache


def day_costs():
    """{local date: $} for the last year, cached 10 min. Cheap on purpose: each session's API cost is spread over
    its days by message count (sessions without messages go to their start day)."""
    # ponytail: spread by messages, not by call time; analyze() over a year would take far too long
    if time.time() - CAL.get("at", 0) < 600:
        return CAL["days"]
    since, ttl, c = time.time() - 371 * 86400, cache_ttl(), connect()
    cost, spread, days = defaultdict(float), defaultdict(dict), defaultdict(float)
    for sid, model, i, r, w, o in c.execute("""select session_id, model, input_tokens, cache_read_tokens, cache_write_tokens,
            output_tokens from session_model_usage where model like 'claude%'"""):
        cost[sid] += sum(row_cost(model, i, r, w, o, ttl))
    for sid, day, n in c.execute("""select session_id, date(timestamp, 'unixepoch', 'localtime'), count(*) from messages
            where timestamp >= ? group by 1, 2""", (since,)):
        spread[sid][day] = n
    first = datetime.fromtimestamp(since).date().isoformat()
    start = {sid: day for sid, day in c.execute("select id, date(started_at, 'unixepoch', 'localtime') from sessions") if day and day >= first}
    for sid, v in cost.items():
        sp = spread.get(sid) or ({start[sid]: 1} if sid in start else {})
        n = sum(sp.values())
        for day, k in sp.items():
            days[day] += v * k / n
    CAL.update(at=time.time(), days=days)
    return days


def calendar(days, today=None):
    """GitHub-style year: one column per week, Monday at the top, a month name over its first Monday."""
    today = today or datetime.now().date()
    d = today - timedelta(days=today.weekday() + 52 * 7)
    shown, out, mx = {}, "", max(days.values(), default=0) or 1
    while d <= today:
        if d.weekday() == 0:
            out += f'<span>{LOC[lang()]["months"][d.month - 1] if d.day <= 7 or not out else ""}</span>'
        v = shown[d] = days.get(d.isoformat(), 0.0)
        lvl = 1 + min(int(math.sqrt(v / mx) * 4), 3) if v > 0 else 0   # square root, so small days stay visible
        out += f'<i class="h{lvl}" title="{tr("cal.cell", day=when(datetime(d.year, d.month, d.day).timestamp(), "day"), v=money(v))}"></i>'
        d += timedelta(days=1)
    active = [x for x in shown.items() if x[1] > 0]
    if not active:
        return f'<p class="hint">{tr("nodata")}</p>'
    bd, bv = max(active, key=lambda x: x[1])
    scale = "".join(f'<i class="h{i}"></i>' for i in range(5))
    return (f'<div class="cal" role="img" aria-label="{tr("cal.title")}">{out}</div>'
            f'<div class="scale">{tr("heat.less")}{scale}{tr("heat.more")}</div>'
            f'<p class="hint">{tr("cal.sum", n=len(active), total=len(shown), day=when(datetime(bd.year, bd.month, bd.day).timestamp(), "day"), v=money(bv))}</p>')


def overlay(w, d30):
    """Cumulative API value through the limit week: this week over the three before, all from the same start."""
    now, wk = time.time(), 7 * 86400
    hrs, lines, top = sorted(d30["hours"].items()), [], 0.0
    for k in range(4):
        s, acc, pts = round(w["since"] / 3600) * 3600 - k * wk, 0.0, [(0.0, 0.0)]
        for h, comp in hrs:
            if s <= h < min(s + wk, now):
                acc += sum(comp.values())
                pts.append((min((h + 3600 - s) / wk, 1.0), acc))
        lines.append(pts)
        top = max(top, acc)
    if not top or len(lines[1]) < 2:
        return f'<p class="hint">{tr("nodata")}</p>'
    svg = "".join(f'<polyline class="w{k}" points="{" ".join(f"{x * 100:.2f},{100 - v / top * 96:.2f}" for x, v in pts)}"/>'
                  for k, pts in reversed(list(enumerate(lines))))
    days = "".join(f"<span>{LOC[lang()]['weekdays'][datetime.fromtimestamp(w['since'] + i * 86400).weekday()]}</span>" for i in range(7))
    same = span_sum(d30, w["since"] - wk, now - wk)
    cur = lines[0][-1][1]
    note = tr("ovl.now", cur=money(cur), prev=money(same), d=signed(cur, same)) if same > 0.01 else ""
    return (f'<svg class="ovl" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="{tr("ovl.title")}">{svg}</svg>'
            f'<div class="axis days">{days}</div><div class="lg"><span><span class="dot c0"></span>{tr("ovl.this")}</span>'
            f'<span><span class="dot f"></span>{tr("ovl.before")}</span></div>' + (f'<p class="hint">{note}</p>' if note else ""))


def page_history(p):
    d, d30 = data_for(p), data_for("30")
    tot, ttl = d["total"] or 1, d["ttl"]
    rows = []
    for t, (comp, steps, ss) in sorted(per_day(d).items(), reverse=True):
        c = sum(comp.values())
        if c > 0:
            rows.append((when(t, "day"), money(c), pct(c, tot), cnt(steps), cnt(len(ss)),
                         f'<span class="why">{e(label(max(comp.items(), key=lambda x: x[1])[0], ttl)[0])}</span>'))
    body = f"""<section class="card"><div class="row"><h2>{tr("hist.per.hour" if d["hourly"] else "hist.per.day")}</h2>
<span class="v">{money(d["total"])}</span></div>
<p class="hint">{tr("hist.hint")}</p>
{stacked(d)}{event_list(d, d30)}</section>
<section class="card"><h2>{tr("hist.when")}</h2>
<p class="hint">{tr("hist.when.hint")}</p>{heatmap(d30)}</section>
<section class="card"><h2>{tr("ovl.title")}</h2>
<p class="hint">{tr("ovl.hint")}</p>{overlay(data_for("w"), d30)}</section>
<section class="card"><h2>{tr("cal.title")}</h2>
<p class="hint">{tr("cal.hint")}</p>{calendar(day_costs())}</section>
<section class="sec"><h2>{tr("hist.days")}</h2>
{table(["th.day", "th.cost", "th.share", "th.steps", "th.sessions", "th.top"], rows, ("", "", "o", "", "o", "l o"))}</section>"""
    return layout(tr("nav.history") + " · Usagecast", "/history", p, tr("nav.history"), tr("sub.hermes", src=source_name(), period=period_text(d)), body)


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
    trow = lambda k, v: (brk(tr("det.skill_view") if k == "tool:skill_view" else k[5:]),
                         cnt(skill_uses if k == "tool:skill_view" else uses.get(k, 0)),
                         "–" if k == "tool:skill_view" else avg(k), money(v), pct(v, tot))
    how = tr("det.how.text", ttl=ttl_text(d["ttl"]), p=pc(d["logged"] / (d["n_steps"] or 1) * 100))
    hv = [(f'{brk(tool)}<span class="why">{when(t, "short")}</span>',
           f'<a href="/s/{quote(sid)}?p={quote(p)}">{e((d["sess"].get(sid, {}).get("title") or tr("untitled"))[:70])}</a>',
           num(n), tr("det.heavy.steps", n=cnt(later)), money(c)) for c, sid, tool, n, later, t in d["heavy"]]
    brs = sorted(d["breaks"].items(), key=lambda x: -x[1][1])
    breaks = (f'<section class="sec"><h2>{tr("det.breaks")}</h2><p class="hint">{tr("det.breaks.hint")}</p>'
              + table(["th.cause", "th.breaks", "th.cost", "th.share"],
                      [(f'{tr("brk." + k)}<span class="why">{tr("brk." + k + ".why")}</span>', cnt(n), money(v), pct(v, tot)) for k, (n, v) in brs],
                      ("l", "", "", "o")) + "</section>") if brs else ""
    body = f"""<section><h2>{tr("det.tools")}</h2>
<p class="hint">{tr("det.tools.hint")}</p>
{table(["th.tool", "th.calls", "th.avg_return", "th.cost", "th.share"], [trow(k, v) for k, v in tools], ("", "", "", "", "o"))}</section>
<section class="sec"><h2>{tr("det.heavy")}</h2>
<p class="hint">{tr("det.heavy.hint")}</p>
{table(["th.result", "th.session", "th.size", "th.carried", "th.cost"], hv, ("", "l o", "", "o", ""))}</section>
<section class="sec"><h2>{tr("det.skills")}</h2>
{table(["th.skill", "th.loaded", "th.avg_size", "th.cost", "th.share"], [(brk(k[6:]), cnt(uses.get(k, 0)), avg(k), money(v), pct(v, tot)) for k, v in skills], ("", "", "", "", "o"))}</section>
<section class="sec"><h2>{tr("det.plugins")}</h2>
{table(["th.plugin", "th.messages", "th.avg_tokens", "th.cost", "th.share"], [(brk(seg_label(k[4:])), cnt(sizes.get(k, [0])[0]), avg(k), money(v), pct(v, tot)) for k, v in inj], ("", "", "", "", "o"))}</section>
<section class="sec"><h2>{tr("det.sys")}</h2>
<p class="hint">{tr("det.sys.hint", at=when(snap.get("at", 0), "short"))}</p>
{table(["th.part", "th.tokens_step", "th.cost", "th.share"], [(e(seg_label(k[4:])), num(snap.get("prompt", {}).get(k[4:], 0)), money(v), pct(v, tot)) for k, v in sysp]
       + [(tr("det.schemas_all"), num(st), money(comp.get("schema", 0)), pct(comp.get("schema", 0), tot))], ("", "", "", "o"))}</section>
<section class="sec"><h2>{tr("det.schemas")}</h2>
<p class="hint">{tr("det.schemas.hint")}</p>
{table(["th.tool", "th.tokens_step", "th.calls", "th.cost", "th.share"],
       [(brk(n), num(t), cnt(uses.get("tool:" + n, 0) if n != "skill_view" else skill_uses), money(comp.get("schema", 0) * t / st), pct(comp.get("schema", 0) * t / st, tot)) for n, t in schemas], ("", "", "", "", "o"))}</section>
<section class="sec"><h2>{tr("det.tasks")}</h2>
{table(["th.item", "th.cost", "th.share"], [(e(label(k, d["ttl"])[0]), money(v), pct(v, tot)) for k, v in tasks])}</section>
{breaks}<section class="sec"><h2>{tr("det.how")}</h2>
<p class="hint">{how}</p></section>"""
    return layout(tr("nav.details") + " · Usagecast", "/details", p, tr("nav.details"), tr("sub.details", src=source_name(), period=period_text(d)), body)


def cron_list():
    """Recurring cron jobs of the analysed Hermes that are not finished (dicts from cron/jobs.json); one-shot jobs are
    left out, they end on their own."""
    try:
        jobs = json.loads((source_home() / "cron" / "jobs.json").read_text())
        jobs = jobs.get("jobs", []) if isinstance(jobs, dict) else jobs
    except (OSError, ValueError, AttributeError):
        return []
    return [j for j in jobs if isinstance(j, dict) and isinstance(j.get("id"), str) and j.get("state") != "completed"
            and (j.get("repeat") or {}).get("times") is None]


def cron_costs(d):
    """{job name: [runs, $]} of the cron sessions in an analysis, and the days it covers."""
    g = defaultdict(lambda: [0, 0.0])
    for x in d["sess"].values():
        if x["origin"].startswith("cron:"):
            a = g[x["origin"][5:]]
            a[0] += 1; a[1] += x["cost"]
    return g, max((min(time.time(), d["until"]) - d["since"]) / 86400, 1 / 24)


def guard_state():
    try:
        return json.loads((DATA / "guard.json").read_text())
    except (OSError, ValueError):
        return {}


def guard_plan(L, now, jobs, state, s=None):
    """Budget guard: (pause, resume) job ids. The marked jobs pause while the week forecast is above 100 % (from the
    second day of the week), the week is at 90 % or the 5-hour window at 85 %. Once none of that holds, or the guard is
    off, or a job is no longer marked, the jobs the guard paused itself come back. jobs: {id: state from jobs.json};
    a job the user paused is never "scheduled", so the guard never takes it over."""
    s = s or SET
    if not L:
        return [], []   # limits unknown: change nothing
    w = {k: (u, frac) for k, u, frac, _, _ in windows(L, now)}
    u, frac = w.get("seven_day", (0.0, None))
    tight = s["guard"] and (u >= 90 or w.get("five_hour", (0.0,))[0] >= 85
                            or bool(frac and frac >= 1 / 7 and (week_forecast(u, frac) or 0) > 100))
    mine = [j for j in state.get("paused", []) if jobs.get(j) == "paused"]
    pause = [j for j in s["guard_jobs"] if jobs.get(j) == "scheduled"] if tight else []
    return pause, [j for j in mine if not tight or j not in s["guard_jobs"]]


def guard():
    """Budget guard step of the 10-minute loop: pauses or resumes the marked jobs through the hermes CLI."""
    jobs, state = {j["id"]: j for j in cron_list()}, guard_state()
    pause, resume = guard_plan(STATE["limits"], time.time(), {k: j.get("state") for k, j in jobs.items()}, state)
    held = {j for j in state.get("paused", []) if jobs.get(j, {}).get("state") == "paused"}
    if not pause and not resume and held == set(state.get("paused", [])):
        return
    exe = shutil.which("hermes") or str(HERMES / "hermes-agent" / "venv" / "bin" / "hermes")
    prof = ["-p", SET["source"]] if source_home() != HERMES else []
    done = {"pause": [], "resume": []}
    for act, ids in (("pause", pause), ("resume", resume)):
        for j in ids:
            try:
                r = subprocess.run([exe, *prof, "cron", act, j], capture_output=True, text=True, timeout=120)
                if r.returncode == 0:
                    done[act].append(j)
                else:
                    print("guard:", act, j, r.stderr.strip()[-300:], flush=True)
            except (OSError, subprocess.TimeoutExpired) as err:
                print("guard:", act, j, err, flush=True)
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "guard.json").write_text(json.dumps({"paused": sorted((held - set(done["resume"])) | set(done["pause"]))}))
    for act in ("pause", "resume"):
        if done[act]:
            notify(tr("alert.guard"), tr("alert.guard." + act, names=", ".join(jobs[j].get("name") or j for j in done[act])), 2)


def guard_note():
    """Overview line while the guard holds jobs back."""
    jobs = {j["id"]: j for j in cron_list()}
    held = [jobs[j].get("name") or j for j in guard_state().get("paused", []) if jobs.get(j, {}).get("state") == "paused"]
    return f'<p class="hint">{tr("ov.guard", names=e(", ".join(held)))}</p>' if held else ""


def cron_jobs(d, p):
    try:
        jobs = json.loads((source_home() / "cron" / "jobs.json").read_text())
        jobs = jobs.get("jobs", []) if isinstance(jobs, dict) else jobs
        sched = {j.get("name"): j.get("schedule_display") or (j.get("schedule") or {}).get("display") or "" for j in jobs}
    except (OSError, ValueError, AttributeError):
        sched = {}
    g, days = cron_costs(d)
    rows = sorted(g.items(), key=lambda kv: -kv[1][1])
    s = sum(c for _, (_, c) in rows)
    hint = tr("cron.hint", n=len(rows), cost=money(s), p=pct(s, d["total"]), week=money(s / days * 7))
    return (f'<p class="hint">{hint}</p>'
            + table(["th.cronjob", "th.schedule", "th.runs", "th.per_run", "th.cost", "th.per_week"],
                    [(f'<a href="{link("/sessions", p=p, src="cron", q=n)}">{e(n)}</a>' if n else tr("untitled"),
                      f"<code>{e(sched[n])}</code>" if sched.get(n) else "–", cnt(r), money(c / r), money(c), money(c / days * 7))
                     for n, (r, c) in rows], ("", "l o", "", "o", "", "")))


def page_sessions(p, q):
    d = data_for(p)
    tot = d["total"] or 1
    arg = lambda k: (q.get(k) or [""])[0].strip()
    view, src, proj, term = arg("view"), arg("src"), arg("proj"), arg("q")
    tabs = (f'<nav class="chips tabs" aria-label="{tr("aria.view")}">' + chip(tr("ses.all"), view != "cron", link("/sessions", p=p))
            + chip(tr("ses.cron"), view == "cron", link("/sessions", p=p, view="cron")) + "</nav>")
    sub = tr("sub.hermes", src=source_name(), period=period_text(d))
    if view == "cron":
        return layout(tr("ses.cron") + " · Usagecast", "/sessions", p, tr("nav.sessions"), sub, tabs + cron_jobs(d, p), keep={"view": "cron"})
    sel = [(s, x) for s, x in d["sess"].items() if (not proj or (x["project"] or NO_PROJ) == proj)
           and (not term or term.lower() in (x["title"] or "").lower())]
    counts = Counter(x["src"] for _, x in sel)
    rows = sorted(((s, x) for s, x in sel if not src or x["src"] == src), key=lambda r: -r[1]["cost"])
    chips = chip(tr("chip.all"), not src, link("/sessions", p=p, proj=proj, q=term)) + "".join(
        chip(f"{e(origin_label(k))}<span>{n}</span>", k == src, link("/sessions", p=p, src=k, proj=proj, q=term))
        for k, n in counts.most_common())
    hidden = "".join(f'<input type="hidden" name="{k}" value="{e(v)}">' for k, v in (("p", p), ("src", src), ("proj", proj)) if v)
    search = (f'<form class="search" action="/sessions" role="search">{hidden}<input type="search" name="q" value="{e(term)}" '
              f'placeholder="{tr("ses.search")}" aria-label="{tr("ses.search")}"></form>')
    flt = f'<div class="filters"><nav class="chips" aria-label="{tr("aria.origin")}">{chips}</nav>{search}</div>'
    if proj:
        clear = f'<a href="{link("/sessions", p=p, src=src, q=term)}">{tr("ses.clear")}</a>'
        flt += f'<p class="hint">{tr("ses.only_project", name=e(proj_label(proj)), clear=clear)}</p>'
    s_cost = sum(x["cost"] for _, x in rows)
    trs = [(f'<a href="/s/{quote(s)}?p={p}">{e((x["title"] or tr("untitled"))[:80])}</a>', e(origin_label(x["origin"])),
            e(proj_label(x["project"] or NO_PROJ)) if x["project"] else "–", cnt(x["calls"]), money(x["cost"]), pct(x["cost"], tot),
            f'<span class="why">{e(label(x["top"], d["ttl"])[0])}</span>') for s, x in rows[:200]]
    body = f"""{tabs}{flt}
<p class="hint">{tr("ses.count", n=cnt(len(rows)), cost=money(s_cost), p=pct(s_cost, tot))}</p>
{table(["th.session", "th.origin", "th.project", "th.steps", "th.cost", "th.share", "th.top"], trs, ("", "l o", "l o", "", "", "o", "l o"), "titles")}"""
    return layout(tr("nav.sessions") + " · Usagecast", "/sessions", p, tr("nav.sessions"), sub, body, keep={"src": src, "proj": proj, "q": term})


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
    trs = [(f'<a href="{link("/sessions", p=p, proj=n)}">{e(proj_label(n))}</a><div class="t{" soft" if n in (NO_PROJ, HERMES_PROJ) else ""}">'
            f'<i style="width:{c / (top or 1) * 100:.1f}%"></i></div>', cnt(s), cnt(st), money(c), pct(c, tot),
            f'<span class="why">{e(label(max(comp.items(), key=lambda x: x[1])[0], d["ttl"])[0]) if comp else ""}</span>')
           for n, (s, st, c, comp) in rows]
    body = f"""<p class="hint">{tr("proj.hint", hermes=tr("proj.hermes"))}</p>
{table(["th.project", "th.sessions", "th.steps", "th.cost", "th.share", "th.top"], trs, ("", "", "o", "", "", "l o"), "titles")}"""
    return layout(tr("nav.projects") + " · Usagecast", "/projects", p, tr("nav.projects"), tr("sub.projects", src=source_name(), period=period_text(d)), body)


def page_session(sid, p):
    ttl, c = cache_ttl(), connect()
    d = analyze(c, 0, load_snap(), ttl, sid=sid)
    if not d["sess"]:
        return None
    x = d["sess"][sid]
    tot = d["total"] or 1
    rb = [s for s in d["steps"] if s[4]]
    short = lambda k: k.split(":", 1)[1] if k.startswith(("tool:", "skill:")) else label(k)[0]
    names = lambda keys: ", ".join(f"{short(k)}{f' ×{keys.count(k)}' if keys.count(k) > 1 else ''}" for k in dict.fromkeys(keys))
    one_day = len({datetime.fromtimestamp(s[0]).date() for s in d["steps"]}) == 1
    steps = [(hm(t) if one_day else when(t, "short"), num(ctx), money(cost),
              f'{money(r)} <span class="why">{tr("ses.after_pause") if kind == "rebuild" else tr("ses.no_pause") + " · " + tr("brk." + kind[6:])}</span>'
              if kind else "",
              f'<span class="why">{brk(names(keys))}</span>') for t, ctx, cost, r, kind, keys in d["steps"]]
    sub = " · ".join([f'<a href="{link("/sessions", p=p)}">{tr("ses.back")}</a>', e(origin_label(x["origin"])),
                      tr("ses.project", name=e(proj_label(x["project"] or NO_PROJ))),
                      tr("ses.started", at=when(x["started"] or 0, "datetime")), tr("ses.steps", n=cnt(x["calls"])), money(x["cost"])])
    logged = tr("ses.logged", p=pc(d["logged"] / d["n_steps"] * 100)) if d["n_steps"] else ""
    body = f"""<p class="hint">{tr("ses.resume")} <code class="cmd">hermes --resume {e(sid)}</code></p>
<section class="card"><h2>{tr("ses.eats")}</h2>
{bars(ranked(d["comp"], group_skills=False), tot, ttl, explain=False, n=40)}</section>
<section class="sec"><h2>{tr("ses.steps_h")}</h2>
<p class="hint">{tr("ses.steps.hint", n=len(rb), cost=money(sum(s[3] for s in rb)))} {logged}</p>
{table(["th.time", "th.history", "th.cost", "th.rebuild", "th.tools"], steps, ("", "", "", "", "l"))}</section>"""
    title = x["title"] or tr("untitled")
    return layout(f"{title} · Usagecast", "/sessions", p, e(title), sub, body, tabs=False)


LAST_PUSH = {}   # result of the last test push, shown on the settings page
INGEST_SHOWN = {}   # when the ingest token was created: shown in full for 10 minutes after that
APP_STORE = "https://apps.apple.com/us/app/ntfy/id1625396347"
COPY_JS = ("var i=document.getElementById('ntfy_topic');i.select();document.execCommand('copy');"
           "if(navigator.clipboard)navigator.clipboard.writeText(i.value);this.textContent=this.dataset.done")


def opts(k, items, off=()):
    """Radio buttons as a segmented control."""
    return (f'<span class="opts" role="radiogroup" aria-labelledby="{k}-l">' + "".join(
        f'<label><input type="radio" name="{k}" value="{v}"{" checked" if v == SET[k] else ""}{" disabled" if v in off else ""}>'
        f'<span>{t}</span></label>' for v, t in items) + "</span>")


def select(k, items, cur):
    return f'<select id="{k}" name="{k}">' + "".join(
        f'<option value="{v}"{" selected" if v == cur else ""}>{t}</option>' for v, t in items) + "</select>"


def check(k):
    return f'<input type="checkbox" id="{k}" name="{k}"{" checked" if SET[k] else ""}>'


def field(k, name, ctl, why=""):
    """One settings row: name and explanation left, control right. k: id of the control."""
    why = '<div class="why">' + why + "</div>" if why else ""   # a div: the push steps hold a list
    lab = (f'<label for="{k}" id="{k}-l">{name}</label>' if re.search(rf'<(?:input|select|button)\b[^>]*\bid="{k}"', ctl)
           else f'<span class="fname" id="{k}-l">{name}</span>')
    return f'<div class="field"><div>{lab}{why}</div><div class="ctl">{ctl}</div></div>'


def card(k, title, inner, hint=""):
    hint = '<p class="hint">' + hint + "</p>" if hint else ""
    return f'<section class="card" id="{k}"><h2>{title}</h2>{hint}{inner}</section>'


def page_settings(q):
    s, done, T = SET, (q.get("done") or [""])[0], lambda k: tr("set." + k)
    note = ""
    if done == "test" and time.time() - LAST_PUSH.get("at", 0) < 600:
        note = tr("set.test.ok" if LAST_PUSH["ok"] else "set.test.fail", reply=f'<code>{e(LAST_PUSH["reply"])}</code>')
        fail, code = not LAST_PUSH["ok"] and ntfy_target(), LAST_PUSH["reply"][:3]
        if fail and code in ("401", "403"):
            note += " " + T("test.auth")
        elif fail and not code.isdigit():
            note += " " + T("test.net")
    elif done in ("save", "setup", "newtopic", "useurl"):
        note = T("done." + done)
    note = f'<p class="note" role="status">{note}</p>' if note else ""
    top, push_note = (note, "") if done == "save" else ("", note)
    names = {"ntfy_topic": "topic", "ntfy_server": "server.url", "url": "url", "quiet_from": "quiet.from", "quiet_to": "quiet.to",
             "extra_steps": "a.extra.steps"}
    bad = [T(names[k]) for k in (q.get("bad") or [""])[0].split(",") if k in names]
    if bad:
        top = f'<p class="note" role="alert">{tr("set.bad", fields=", ".join(bad))}</p>'
    if FX.get("rate"):
        fx = tr("set.fx", date=when(datetime.fromisoformat(FX["date"]).timestamp(), "day"), rate=nf(FX["rate"], 4))
    else:
        fx = T("fx.none" if s["currency"] == "EUR" else "fx.usd")
    look = (field("theme", T("theme"), opts("theme", [(v, T("theme." + v)) for v in CHOICES["theme"]]))
            + field("lang", T("lang"), select("lang", [(c, LOC[c]["lang.name"]) for c in LOC], lang()), T("lang.why"))
            + field("currency", T("currency"), select("currency", [(c, T("currency." + c)) for c in CHOICES["currency"]], s["currency"]), fx)
            + field("numbers", T("numbers"), opts("numbers", [(v, T("numbers." + v)) for v in CHOICES["numbers"]]))
            + field("period", T("period"), select("period", [(k, tr("period." + k)) for k in PERIODS], s["period"]), T("period.why")))
    limits = field("limit_view", T("view"), opts("limit_view", [(v, T("view." + v)) for v in CHOICES["limit_view"]]), T("view.why"))
    rate = tr("lim.rate", cost=cash(1 / RATE["k"]), n=RATE["hours"]) if RATE.get("k") else T("cost.none")
    limits += field("cost_unit", T("cost"), opts("cost_unit", [(v, T("cost." + v)) for v in CHOICES["cost_unit"]]), T("cost.why") + " " + rate)
    lo, hi = RANGES["five_min"]
    five = (f'{check("alert_five")}<input type="number" id="five_min" name="five_min" value="{s["five_min"]}" min="{lo}" max="{hi}"'
            f' inputmode="numeric" aria-label="{T("a.five.min")}"><span class="why">{T("min")}</span>')
    quiet = (f'{check("quiet")}<input type="time" id="quiet_from" name="quiet_from" value="{s["quiet_from"]}" aria-label="{T("quiet.from")}">'
             f'<span class="why">–</span><input type="time" id="quiet_to" name="quiet_to" value="{s["quiet_to"]}" aria-label="{T("quiet.to")}">')
    share = lambda k: (f'<input type="number" id="{k}" name="{k}" value="{s[k]}" min="{RANGES[k][0]}" max="{RANGES[k][1]}" step="0.1"'
                       f' inputmode="decimal" aria-label="{T(k)}"><span class="why">{T("pct")}</span>')
    nok = "" if RATE.get("k") else " " + T("a.nok")
    acur = ((STATE["limits"] or {}).get("extra_usage") or {}).get("currency") or ""
    jl, (gc, gdays), held = cron_list(), cron_costs(data_for("30")), guard_state().get("paused", [])
    rows = "".join(f'<div class="job"><input type="checkbox" id="gj_{e(j["id"])}" name="gj_{e(j["id"])}"{" checked" if j["id"] in s["guard_jobs"] else ""}>'
                   f'<label for="gj_{e(j["id"])}">{e(j.get("name") or j["id"])}</label><span class="why">'
                   f'{tr("set.guard.week", cost=money(gc[j.get("name")][1] / gdays * 7)) if j.get("name") in gc else "–"}'
                   f'{" · " + T("guard.held") if j["id"] in held and j.get("state") == "paused" else ""}</span></div>' for j in jl)
    guard_ = field("guard", T("guard.on"), check("guard"), T("guard.on.why")) + (
        field("guard_jobs", T("guard.jobs"), "", T("guard.jobs.why")) + f'<div class="jobs">{rows}</div>' if jl else f'<p class="hint">{T("guard.none")}</p>')
    tk, url = s["ingest_token"], s["url"] or "https://dashboard.example:8443"
    full = done == "ingest" and time.time() - INGEST_SHOWN.get("at", 0) < 600
    if not tk:
        machines = field("ingest", T("ing.token"), f'<button class="btn primary" id="ingest" name="action" value="ingest">{T("ing.create")}</button>', T("ing.why"))
    else:
        machines = field("ingest_tok", T("ing.token"), f'<input id="ingest_tok" value="{e(tk if full else tk[:4] + "…" + tk[-4:])}" readonly class="mono"'
                         f' spellcheck="false">' + (f'<button type="button" class="btn" data-done="{T("copied")}" onclick="{COPY_JS.replace("ntfy_topic", "ingest_tok")}">'
                                                    f'{T("copy")}</button>' if full else "")
                         + f'<button class="btn" name="action" value="ingest" data-q="{e(T("ing.rotate.confirm"))}" onclick="return confirm(this.dataset.q)">'
                         f'{T("ing.rotate")}</button>', T("ing.shown" if full else "ing.masked"))
        cmd = f'curl -fsSO {url}/cc-report.py && python3 cc-report.py --url {url} --token {tk if full else "TOKEN"} --install'
        machines += field("ingest_cmd", T("ing.install"), "", T("ing.install.why")) + f'<p><code class="cmd">{e(cmd)}</code></p>'
        hosts = ingest_hosts()
        lines = "".join(f'<li>{tr("set.ing.host", host=e(h), at=when(x["at"], "daytime"), cost=money(sum(c for t, c in x["hours"] if t >= time.time() - 7 * 86400)))}</li>'
                        for h, x in hosts.items())
        machines += field("ingest_hosts", T("ing.hosts"), "", f'<ul class="steps">{lines}</ul>' if hosts else T("ing.none"))
    ps, main = profiles(), tr("set.src.main", path=e(str(HERMES)))
    source = (field("source", T("src.label"), select("source", [("", main), *((x, e(x)) for x in ps)],
                                                     s["source"] if s["source"] in ps else ""), T("src.why")) if ps
              else field("source", T("src.label"), f'<span class="why">{main}</span>', T("src.none")))
    alerts_ = (field("alert_five", T("a.five"), five, T("a.five.why")) + field("alert_week", T("a.week"), check("alert_week"), T("a.week.why"))
               + field("alert_extra", T("a.extra"), check("alert_extra"), T("a.extra.why"))
               + field("extra_steps", T("a.extra.steps"), f'<input id="extra_steps" name="extra_steps" value="{e(s["extra_steps"])}"'
                       f' inputmode="decimal" placeholder="10, 20" spellcheck="false" autocomplete="off"><span class="why">{e(acur)}</span>',
                       T("a.extra.steps.why"))
               + field("alert_free", T("a.free"), check("alert_free"), T("a.free.why"))
               + field("alert_steps", T("a.steps"), check("alert_steps"), T("a.steps.why"))
               + field("alert_chat", T("a.chat"), check("alert_chat") + share("chat_pct"), T("a.chat.why") + nok)
               + field("alert_spike", T("a.spike"), check("alert_spike") + share("spike_pct"), T("a.spike.why") + nok)
               + field("alert_digest", T("a.digest"), check("alert_digest"), T("a.digest.why")) + field("quiet", T("quiet"), quiet, T("quiet.why")))
    host, own = urlparse(s["ntfy_server"]).netloc, s["ntfy_server"] != "https://ntfy.sh"
    target = ntfy_target()
    state = (T("push.state.off") if not target else T("push.state.hermes") if s["push"] == "hermes"
             else tr("set.push.state.own", server=e(host)))
    push = f'<p class="state"><strong>{state}</strong></p>{push_note}'
    if not s["ntfy_topic"]:
        push += field("setup", T("setup"), f'<button class="btn primary" id="setup" name="action" value="setup">{T("setup.btn")}</button>', T("setup.why"))
    else:
        android = f'ntfy://{host}/{s["ntfy_topic"]}' + ("" if s["ntfy_server"].startswith("https:") else "?secure=false")
        steps = "".join(f"<li>{x}</li>" for x in (tr("set.ios.1", url=APP_STORE), T("ios.2"), T("ios.3")))
        ios = f'{T("ios")}<ol class="steps">{steps}</ol>' + (tr("set.ios.own", server=e(s["ntfy_server"])) if own else "")
        own = (field("ntfy_topic", T("topic"), f'<input id="ntfy_topic" name="ntfy_topic" value="{e(s["ntfy_topic"])}" class="mono" pattern="[A-Za-z0-9_\\-]+" maxlength="64"'
                       f' spellcheck="false" autocomplete="off" autocapitalize="off"><button type="button" class="btn" data-done="{T("copied")}"'
                       f' onclick="{COPY_JS}">{T("copy")}</button>', T("topic.why"))
                 + field("android", T("subscribe"), f'<a class="btn" id="android" href="{e(android)}">{T("android")}</a>', ios)
                 + field("newtopic", T("newtopic"), f'<button class="btn" id="newtopic" name="action" value="newtopic"'
                         f' data-q="{e(T("newtopic.confirm"))}" onclick="return confirm(this.dataset.q)">{T("newtopic.btn")}</button>', T("newtopic.why")))
        push += f'<details class="adv"><summary>{T("own")}</summary>{own}</details>' if s["push"] == "hermes" else own   # Hermes sends, own topic waits
    if target:
        push += field("test", T("test"), f'<button class="btn" id="test" name="action" value="test">{T("test.btn")}</button>', T("test.why"))
    hermes_ok = hermes_ntfy() is not None
    push += field("push", T("via"), opts("push", [(v, T("via." + v)) for v in CHOICES["push"]], () if hermes_ok else ("hermes",)),
                  T("via.why" if hermes_ok else "via.nohermes"))
    token = (f'<input type="password" id="ntfy_token" name="ntfy_token" placeholder="{T("token.saved") if s["ntfy_token"] else ""}"'
             f' autocomplete="new-password">' + (f'<label class="why"><input type="checkbox" name="token_clear"> {T("token.clear")}</label>'
                                                 if s["ntfy_token"] else ""))
    push += (f'<details class="adv"{" open" if own else ""}><summary>{T("server")}</summary>'
             + field("ntfy_server", T("server.url"), f'<input type="url" id="ntfy_server" name="ntfy_server" value="{e(s["ntfy_server"])}" class="wide">',
                     T("server.why"))
             + field("ntfy_token", T("token"), token, T("token.why")) + "</details>")
    push += field("url", T("url"), f'<input type="url" id="url" name="url" value="{e(s["url"])}" class="wide" placeholder="https://">'
                  f'<button class="btn" name="action" value="useurl">{T("useurl")}</button>', T("url.why"))
    body = (f'{top}<form method="post" action="/settings" class="set">'
            '<button class="sr" name="action" value="save" tabindex="-1" aria-hidden="true"></button>'  # Enter in a field = save
            + card("look", T("look"), look) + card("limits", T("limits"), limits) + card("alerts", T("alerts"), alerts_, T("alerts.hint")) + card("guard", T("guard"), guard_, T("guard.hint")) + card("machines", T("ing"), machines, T("ing.hint"))
            + card("push", T("push"), push, T("push.hint")) + card("source", T("src"), source)
            + f'<div class="save"><button class="btn primary" name="action" value="save">{T("save")}</button></div></form>')
    return layout(T("h1") + " · Usagecast", "/settings", s["period"], T("h1"), T("sub"), body, tabs=False)


class Handler(BaseHTTPRequestHandler):
    cookie = None

    def send(self, b, ctype, cache="no-cache"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", cache)
        if self.cookie:
            self.send_header("Set-Cookie", self.cookie)
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        wanted = (q.get("lang") or [""])[0]
        _req.lang, _req.url = pick_lang(wanted, self.headers), self.path
        self.cookie = f"lang={wanted}; Path=/; Max-Age=31536000; SameSite=Lax" if wanted in LOC else None
        if q.get("from") and q.get("to"):
            q["p"] = [f'{q["from"][0]}..{q["to"][0]}']   # the custom-range form
        p = q.get("p", [SET["period"]])[0]
        p = p if p in PERIODS or custom(p) else SET["period"]
        path = ALIASES.get(u.path, u.path)
        name = path.lstrip("/")
        if name in STATIC_FILES:
            try:
                return self.send((STATIC / name).read_bytes(), STATIC_FILES[name], "max-age=86400")
            except OSError:
                return self.send_error(404)
        pages = {"/": page_overview, "/history": page_history, "/details": page_details, "/projects": page_projects}
        try:
            if path in pages:
                body = pages[path](p)
            elif path == "/sessions":
                body = page_sessions(p, q)
            elif path.startswith("/s/"):
                body = page_session(unquote(path[3:]), p)
            elif path == "/settings":
                body = page_settings(q)
            elif path == "/api/summary":
                return self.send(api_summary().encode(), "application/json")
            elif path == "/cc-report.py":
                return self.send((ROOT / "tools" / "cc-report.py").read_bytes(), "text/x-python; charset=utf-8")
            elif path == "/health":
                body = "ok"
            else:
                body = None
        except Exception:
            traceback.print_exc()
            return self.send_error(500)
        if body is None:
            return self.send_error(404)
        self.send(body.encode(), "text/html; charset=utf-8")

    def do_POST(self):
        """Only /settings: saves the form, then runs the button's action (set up, new topic, test, use this address)."""
        _req.lang, _req.url = pick_lang("", self.headers), self.path
        n = self.headers.get("Content-Length", "0")
        if urlparse(self.path).path == "/api/ingest":
            tok = SET["ingest_token"]
            if not tok or not hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + tok).encode()):
                return self.send_error(401)
            if not n.isdigit() or int(n) > 2 * 1024 * 1024:
                return self.send_error(400)
            rep = parse_ingest(self.rfile.read(int(n)))
            if not rep:
                return self.send_error(400)
            save_ingest(*rep)
            return self.send(b'{"ok": true}', "application/json")
        if urlparse(self.path).path != "/settings":
            return self.send_error(404)
        if not same_origin(self.headers) or not n.isdigit() or int(n) > 65536:
            return self.send_error(403)
        form = {k: v[-1] for k, v in parse_qs(self.rfile.read(int(n)).decode("utf-8", "replace"), keep_blank_values=True).items()}
        action = form.get("action", "save")
        s = clean(form, SET)
        bad = [k for k in FORMATS if form.get(k, "").strip().rstrip("/") not in ("", s[k])]   # clean() kept the old value
        if action == "ingest":
            s["ingest_token"] = secrets.token_urlsafe(24)
            INGEST_SHOWN["at"] = time.time()
        if action in ("setup", "newtopic"):
            s["ntfy_topic"] = new_topic()
            s["push"] = "own" if action == "setup" else s["push"]
        elif action == "useurl" and re.fullmatch(URL_RX, self.headers.get("Origin") or ""):
            s["url"] = self.headers["Origin"].rstrip("/")
        save_settings(s)
        _req.lang = s["lang"]
        if s["currency"] == "EUR":
            refresh_fx()
        if action == "test":
            ok, reply = send_push(tr("alert.test"), tr("alert.test.text"), 3)
            LAST_PUSH.update(at=time.time(), ok=ok, reply=reply)
        self.send_response(303)
        self.send_header("Location", f"/settings?done={quote(action)}" + (f"&bad={','.join(bad)}" if bad else "")
                         + ("" if action == "save" or bad else "#machines" if action == "ingest" else "#push"))
        self.send_header("Set-Cookie", f"lang={s['lang']}; Path=/; Max-Age=31536000; SameSite=Lax")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


# ---------- Self-test ----------
def test_db(base=0.0):
    """In-memory Hermes database with one chat session (pause after 1,000 s) and one cron run without history."""
    tc = json.dumps([{"id": "a", "function": {"name": "terminal", "arguments": '{"command": "ls /opt/shop"}'}}])
    msgs = [("user", "hi " * 50, None, None, None, None, base + 0.0, None),
            ("assistant", "", None, None, tc, None, base + 1.0, "think " * 20),
            ("tool", "x" * 700, None, "terminal", None, "a", base + 2.0, None),
            ("assistant", "done", None, None, None, None, base + 3.0, None),
            ("user", "go on", None, None, None, None, base + 1000.0, None),
            ("assistant", "ok", None, None, None, None, base + 1001.0, None)]
    db = sqlite3.connect(":memory:")
    db.executescript("""create table sessions(id, source, title, system_prompt_hash, started_at, git_repo_root);
        create table system_prompts(hash, prompt);
        create table messages(id integer primary key, session_id, role, content, api_content, tool_name, tool_calls,
            tool_call_id, timestamp, reasoning);
        create table session_model_usage(session_id, model, task, api_call_count, input_tokens, cache_read_tokens,
            cache_write_tokens, output_tokens, last_seen);""")
    db.executemany("insert into sessions values(?, ?, ?, null, ?, null)",
                   [("s", "telegram", "Test", base), ("c", "cron", "Daily report · Oct 08 16:31", base)])
    db.executemany("insert into session_model_usage values(?, 'claude-opus-5-5', ?, ?, ?, ?, ?, ?, ?)",
                   [("s", "", 3, 10, 5000, 3000, 400, base + 1001), ("s", "background_review", 1, 0, 2000, 500, 100, base + 1001),
                    ("c", "", 2, 5, 1000, 800, 50, base + 500)])
    db.executemany("""insert into messages(session_id, role, content, api_content, tool_name, tool_calls, tool_call_id,
        timestamp, reasoning) values('s', ?, ?, ?, ?, ?, ?, ?, ?)""", msgs)
    db.commit()  # an open transaction would make backup() wait forever
    return db, msgs


DEMO_TOOLS = {"terminal": (1500, "command", "git -C {p} status"), "read_file": (9000, "path", "{p}/app.py"),
              "search_files": (3000, "path", "{p}"), "patch": (600, "path", "{p}/app.py"), "web_search": (4000, "query", "release notes"),
              "web_extract": (12000, "url", "https://example.com/docs"), "browser_exec": (2500, "code", "page_info()"),
              "session_search": (30000, "query", "last deploy"), "skill_view": (6000, "name", "deploy")}
DEMO_TITLES = ("Fix the checkout bug", "Write release notes", "Plan the week", "Migrate the database", "Review open pull requests",
               "Draft a blog post", "Tidy up the wiki", "Debug the deploy")


def demo_db(now):
    """A month of made-up sessions for --demo: chats, CLI work on three projects, two cron jobs, now and then a pause."""
    rnd, (db, _) = random.Random(7), test_db(now - 30 * 86400)
    for day in range(30, -1, -1):
        d0 = datetime.fromtimestamp(now - day * 86400).replace(hour=0, minute=0, second=0, microsecond=0)
        for _ in range(rnd.randint(1, 3) if d0.weekday() >= 5 else rnd.randint(3, 7)):
            t = d0.timestamp() + rnd.uniform(8, 23) * 3600   # a day person
            if t > now - 1800:
                continue
            kind, proj = rnd.choices(("telegram", "cli", "cron"), (5, 3, 2))[0], rnd.choice(("/opt/shop", "/srv/blog", "/opt/wiki", "/tmp"))
            title = (f'{rnd.choice(("Daily report", "Inbox digest"))} · {datetime.fromtimestamp(t).strftime("%b %d %H:%M")}'
                     if kind == "cron" else rnd.choice(DEMO_TITLES))
            n = rnd.randint(4, 12) if kind == "cron" else rnd.randint(3, 25) if kind == "telegram" else rnd.randint(10, 60)
            sid = datetime.fromtimestamp(t).strftime("%Y%m%d_%H%M%S_") + "%06x" % rnd.getrandbits(24)
            rows = [(sid, "user", "please " * rnd.randint(5, 80), None, None, None, t, None)]
            ctx, prev, calls = 40000 + len(rows[0][2]) / 4, 0, []   # tokens: system prompt + tool schemas + history
            for j in range(n):
                gap = rnd.uniform(400, 2400) if rnd.random() < 0.08 else rnd.uniform(10, 90)
                if t + gap > now - 300:
                    break
                t += gap
                calls.append((ctx, ctx - prev, j == 0 or gap > 300))   # context, new since the last call, cache expired
                prev = ctx
                if j == n - 1:
                    rows.append((sid, "assistant", "Done. " * 20, None, None, None, t, "think " * rnd.randint(10, 200)))
                    break
                tool = rnd.choices(list(DEMO_TOOLS), (30, 22, 10, 12, 6, 3, 6, 1, 4))[0]
                size, arg, val = DEMO_TOOLS[tool]
                call = json.dumps([{"id": f"c{j}", "function": {"name": tool, "arguments": json.dumps({arg: val.format(p=proj)})}}])
                res = "x" * int(size * rnd.uniform(0.2, 2))
                rows += [(sid, "assistant", "", None, call, None, t, "think " * rnd.randint(10, 200)),
                         (sid, "tool", res, tool, None, f"c{j}", t + 2, None)]
                ctx += 400 + len(res) / 4
            if not calls:
                continue
            read, write = sum(0 if cold else c - a for c, a, cold in calls), sum(c if cold else a for c, a, cold in calls)
            db.execute("insert into sessions values(?, ?, ?, null, ?, null)", (sid, kind, title, rows[0][6]))
            db.execute("insert into session_model_usage values(?, 'claude-opus-5-5', '', ?, ?, ?, ?, ?, ?)",
                       (sid, len(calls), 5 * len(calls), round(read), round(write), rnd.randint(150, 600) * len(calls), rows[-1][6]))
            db.executemany("""insert into messages(session_id, role, content, tool_name, tool_calls, tool_call_id, timestamp,
                reasoning) values(?, ?, ?, ?, ?, ?, ?, ?)""", rows)
    db.commit()
    return db


def demo():
    """Usagecast on made-up data (README screenshots, a first look): no Hermes needed, no limit fetch, no alerts."""
    global HERMES, DATA, CLAUDE
    now, tmp = time.time(), Path(tempfile.mkdtemp(prefix="usagecast-demo-"))
    HERMES, DATA, CLAUDE = tmp, tmp / "data", tmp / "claude"
    (tmp / "cron").mkdir(parents=True)
    DATA.mkdir()
    demo_db(now).backup(disk := sqlite3.connect(tmp / "state.db"))
    disk.close()
    (tmp / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
        {"id": "d1", "name": "Daily report", "schedule_display": "0 7 * * *", "state": "scheduled", "repeat": {"times": None}},
        {"id": "d2", "name": "Inbox digest", "schedule_display": "every 360m", "state": "scheduled", "repeat": {"times": None}}]}))
    (DATA / "snapshot.json").write_text(json.dumps({"prompt": {}, "at": now, "tools": {
        "terminal": 1100, "read_file": 480, "search_files": 510, "patch": 560, "web_search": 230, "web_extract": 320, "browser_exec": 1060,
        "session_search": 2000, "skill_view": 270, "memory": 900, "todo": 390, "cronjob": 2800, "delegate_task": 1650}}))
    reset, five = now + 2.6 * 86400, now + 2.2 * 3600
    start = reset - 7 * 86400
    with open(DATA / "limits.jsonl", "w") as f:   # last week ramps to 85 %, this one to 62 % so far; 5-hour windows refill
        for t in range(int(now - 7 * 86400), int(now), 600):
            wk = 62 * ((t - start) / (now - start)) ** 1.15 if t >= start else 85 * (t - start + 7 * 86400) / (7 * 86400)
            ex = 12.4 * (t - now + 7 * 86400) / (4 * 86400) if t < now - 3 * 86400 else 5.01 * (t - now + 3 * 86400) / (3 * 86400)
            f.write(json.dumps({"t": t, "five_hour": round(min(12 * ((t - five) % 18000) / 3600, 100)), "seven_day": round(wk),
                                "extra": round(ex, 2)}) + "\n")
    iso = lambda t: datetime.fromtimestamp(t).astimezone().isoformat()
    STATE.update(limits={"five_hour": {"utilization": 34.0, "resets_at": iso(five)}, "seven_day": {"utilization": 62.0, "resets_at": iso(reset)},
                         "extra_usage": {"is_enabled": True, "used_credits": 501, "monthly_limit": 2500, "decimal_places": 2, "currency": "USD"}},
                 at=1e12, ok=now)   # at far ahead: refresh_limits() never fetches
    SET.clear(); SET.update(DEFAULTS, push="off", ntfy_topic="", url="", guard=True, guard_jobs=["d2"]); FX.clear()
    print(f"usagecast demo on http://127.0.0.1:{PORT} (data in {tmp})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


def selftest():
    global HERMES, DATA, CLAUDE
    SET.clear(); SET.update(DEFAULTS); FX.clear(); FC.clear()   # the installed settings must not change the expected formats
    # Locales: same keys and placeholders everywhere, every literal key used in the code exists
    src = Path(__file__).read_text("utf-8")
    used = set(re.findall(r'tr\(\s*"([\w.:-]+)"(?=\s*[,)])', src)) | set(re.findall(r'"(th\.[\w]+)"', src))  # "x." + k: below
    used |= {f"comp.{k}" for k in FIXED} | {f"comp.{k}.why" for k in FIXED} | {f"tip.tool.{k}" for k in TOOL_HINTS}
    used |= {f"period.{k}" for k in PERIODS} | {f"win.{k}" for k, _ in WINDOWS} | {f"since.{k}" for k in ("w", "1", "7", "30")}
    used |= {f"seg.{k}" for k, _ in HERMES_MARKERS} | {"seg.soul", "seg.misc", "seg.notes", "tip.ttl1h.text", "tip.ttl5m.text"}
    used |= {f"brk.{k}" for k in BREAKS} | {f"brk.{k}.why" for k in BREAKS}
    used |= {"hist.per.hour", "hist.per.day", "ses.after_pause", "ses.no_pause", "foot.ntfy.on", "foot.ntfy.off", "tip.ttl1h", "tip.ttl5m"}
    for code, texts in LOC.items():
        assert set(texts) == set(LOC["en"]), (code, set(texts) ^ set(LOC["en"]))
        for k, v in texts.items():
            if isinstance(v, str) and not k.startswith("fmt."):  # date patterns use different fields per language
                assert set(re.findall(r"{(\w+)", v)) == set(re.findall(r"{(\w+)", LOC["en"][k])), (code, k)
    assert not used - set(LOC["en"]), used - set(LOC["en"])
    assert pick_lang("", {"Accept-Language": "de-DE,de"}) == DEFAULT_LANG and pick_lang("", {"Cookie": "a=1; lang=de"}) == "de"
    assert pick_lang("en", {"Cookie": "lang=de"}) == "en" and pick_lang("xx", {}) == DEFAULT_LANG
    # Number and date formats per language
    _req.lang = "de"
    assert (money(1234.5), pct(1, 8), num(1_234_567), num(5400), cnt(12345)) == ("1.234,50 $", "12,5 %", "1,2 Mio.", "5 Tsd.", "12.345")
    assert when(datetime(2026, 10, 8, 9, 5).timestamp(), "daytime") == "Do 08.10. 09:05"
    _req.lang = "en"
    assert (money(1234.5), pct(1, 8), num(1_234_567), num(5400), money(0.001)) == ("$1,234.50", "12.5%", "1.2M", "5K", "< $0.01")
    assert when(datetime(2026, 10, 8, 9, 5).timestamp(), "daytime") == "Thu Oct 8, 09:05"
    SET.update(currency="EUR", numbers="full"); FX["rate"] = 1.25
    assert (money(10), num(1_234_567)) == ("€8.00", "1,234,567")
    SET.update(currency="USD", numbers="compact"); FX.clear()
    SET["cost_unit"], RATE["k"] = "week", 0.5
    assert (money(4), money(0.1), cash(4), dur(59 * 60), dur(80 * 60)) == ("2.0% of week", "< 0.1% of week", "$4.00", "~59 min", "~1 h 20 min")
    t = table(["th.cost"], [(money(4),)])
    assert "<th>% of week</th>" in t and "<td>2.0%</td>" in t and ">2.0% of week</span>" in bars([("x", 4.0)], 8.0, 300, False), t
    SET["cost_unit"] = "money"; RATE.clear()
    assert (signed(100.3, 100), signed(120, 100), signed(90, 100)) == ("±0%", "+20%", "−10%")
    assert origin("cron", None) == "cron:" and origin_label("cron:") == "Cron: Untitled" and origin_label("cron:a") == "Cron: a"
    assert '<span class="fname" id="theme-l">' in field("theme", "T", opts("theme", [("system", "S")])) and '<label for="x"' in field("x", "X", '<input id="x">')
    # Limit split: the growth of each hour goes to whoever ran then, gaps without readings count for nobody
    vals = [10 + i for i in range(7)] + [16 + i for i in range(1, 7)] + [22 + 0.5 * i for i in range(1, 7)]
    hist = [{"t": 7200 + 600 * i, "seven_day": v} for i, v in enumerate(vals)] + [{"t": 7200 + 600 * 18 + 3600, "seven_day": 27}]
    sp = limit_split(hist, {7200: {"tool:x": 6.0}}, [(10800 + 100, 2.0)], 0)
    assert {k: sp[k] for k in ("hermes", "cc", "rest", "gap", "before", "hours")} == {"hermes": 6, "cc": 6, "rest": 3, "gap": 2, "before": 10, "hours": 3}, sp
    assert sp["k"] == 1.5 and limit_split(hist[:1], {}, [], 0) is None
    assert (signed(12, 10), signed(8, 10)) == ("+20%", "−20%")
    t0 = 1_800_000_000.0
    d30 = {"hours": {t0 - 8 * 86400: {"x": 10.0}, t0 - 3600: {"x": 12.0}}}
    assert week_delta(d30, 12.0, t0 - 86400, t0) == "+20% vs. last week" and week_delta(d30, 12.0, t0 - 20 * 86400, t0) == ""
    assert span_sum({"hours": {3600: {"x": 1.0}}}, 3600.4, 7200) == span_sum({"hours": {3600: {"x": 1.0}}}, 3599.6, 7200) == 1.0  # resets a few ms off
    # Config, markers, prices
    cfg = ("model:\n  default: m\n  provider: anthropic\nmemory:\n\n  provider: memory_tencentdb\n"
           "plugins:\n  enabled:\n    - ponytail\n    - 'superpowers'\n    - platforms/ntfy\n  disabled: []\n")
    assert config_value("model", "provider", cfg) == "anthropic" and config_value("memory", "provider", cfg) == "memory_tencentdb"
    assert plugin_names(cfg) == ["ponytail", "superpowers", "memory_tencentdb"], plugin_names(cfg)
    pm, im = markers(plugin_names(cfg))
    s = segments("hello\n\nPONYTAIL MODE ACTIVE x\n<EXTREMELY_IMPORTANT>\nsuperpowers y\n[Note: model changed]", im)
    assert [l for l, _ in s] == ["misc", "plugin:ponytail", "plugin:superpowers", "notes"], s
    assert segments("me\n# memory-tencentdb\nx", pm)[-1][0] == "plugin:memory_tencentdb"   # - and _ are equivalent
    assert price("claude-opus-5-5")[0] == 4e-6 and price("claude-opus-4-8")[4] == 25e-6 and price("claude-opus-4-1-x")[0] == 15e-6
    assert known_model("claude-opus-5-5") and not known_model("claude-opus-9")
    CACHE["t"] = (0, "stale"); REFRESHING.add("t")
    assert data_for("t") == "stale"; CACHE.pop("t"); REFRESHING.discard("t")
    # Replay and cache
    db, msgs = test_db()
    calls, _ = simulate(msgs, {"schema": 100.0})
    splits = [cache_split(b, n, g, 300) for _, g, b, n, _, _ in calls]
    assert [x[2] > 0 for x in splits] == [False, False, True], splits     # pause > 5 min: rebuild
    assert "tool:terminal" in splits[1][1] and splits[1][0]["schema"] == 100  # result written anew, prefix read
    assert not cache_split(calls[2][2], calls[2][3], calls[2][1], 3600)[2]    # no rebuild with 1 h
    hit, new, lost = cache_split({"schema": 100, "user": 100}, {"schema": 100, "user": 150}, 1, 300, rho=0.5)
    assert hit == {"schema": 100, "user": 0} and new == {"user": 50} and lost == 100  # log: the cache returns the beginning
    snap = {"tools": {"terminal": 100.0}}
    d = analyze(db, -1, snap, 300, logs={})
    real = sum(sum(row_cost("claude-opus-5-5", *r, 300)) for r in [(10, 5000, 3000, 400), (0, 2000, 500, 100), (5, 1000, 800, 50)])
    assert abs(d["total"] - real) < 1e-12, (d["total"], real)               # nothing gets lost, nothing gets added
    assert d["comp"]["rebuild"] > 0 and d["comp"]["task:background_review"] > 0 and d["comp"]["tool:terminal"] > 0
    assert d["where"].keys() == {"telegram", "cron:Daily report"} and d["sess"]["s"]["project"] == "shop"
    late = analyze(db, 999, snap, 300, logs={})    # the period cuts by call time
    early = analyze(db, -1, snap, 300, logs={}, until=999)
    assert abs(early["total"] + late["total"] - d["total"]) < 1e-12 and early["total"] > 0     # until cuts the same way
    a, b = custom("2026-10-07..2026-10-01")
    assert (a, b) == (datetime(2026, 10, 1).timestamp(), datetime(2026, 10, 8).timestamp()) and custom("2026-13-01..2026-10-01") is None and not custom("7")
    assert 0 < late["total"] < d["total"] and late["comp"]["rebuild"] > 0 and "tool:terminal" not in late["uses"]
    logged = analyze(db, -1, snap, 300, logs={"s": ([1.0, 3.0, 1001.0], [(5000, 0), (6000, 0), (6100, 0)])})
    assert logged["comp"]["break"] > 0 and abs(logged["total"] - real) < 1e-12 and logged["logged"] == 3  # break without pause
    assert abs(sum(v for _, v in logged["breaks"].values()) - logged["comp"]["break"]) < 1e-12 and set(logged["breaks"]) <= set(BREAKS)
    R = lambda role, content="": (role, content, None, None, None, None, 0, None)
    rows, ap = [R("user"), R("assistant"), R("tool", "[screenshot] " * 4), R("assistant"), R("user", "next"), R("assistant"), R("tool", "x"), R("assistant")], [1, 3, 5, 7]
    rl = [(100, 0), (200, 150), (300, 100), (250, 40)]
    assert [break_cause(rows, ap, j, rl, 50) for j in (1, 2, 3)] == ["image", "turn", "shrunk"]
    rl[3] = (350, 50)
    assert (break_cause(rows, ap, 3, rl, 50), break_cause(rows, ap, 3, rl, 10), break_cause(rows, ap, 3, rl[:3] + [None], 10)) == ("deep", "other", "sim")
    assert abs(sum(sum(v.values()) for v in d["hours"].values()) - d["total"]) < 1e-12        # the history adds up
    assert sum(d["hsteps"].values()) == d["n_steps"] == 3
    assert [x[1:3] + (x[4],) for x in d["heavy"]] == [("s", "terminal", 1)] and d["heavy"][0][0] > 0   # written by the next call, read by 1 later
    # Projects: majority of paths, Hermes' own folder only without another project, system folders don't count
    rx, repos = project_rx("/h/u", "/h/u/.hermes"), {"code/app": "app", "tool": "tool"}
    assert project_of(['{"command": "cat ~/.hermes/x; cd /opt/shop && ls /opt/shop/src"}'], repos, rx) == "shop"
    assert project_of(["~/.hermes/skills/a", "/h/u/.hermes/b"], repos, rx) == HERMES_PROJ
    assert project_of(["/opt/homebrew/bin/x", "~/.hermes/cache/y", "~/Downloads/z"], repos, rx) is None
    assert project_of(["/h/u/code/app/main.py", "$HOME/tool/x"], repos, rx) in ("app", "tool")
    assert project_of([], repos, rx, "/srv/git/werk") == "werk"
    assert project_of(["~/.hermes/a"] * 12 + ["/opt/shop"] * 2, repos, rx) == HERMES_PROJ  # a side mention doesn't count
    # Forecast: linear since the week started, 5-hour pace only from points of the running window
    assert abs(week_forecast(34, 0.47) - 72.34) < 0.01 and week_forecast(5, 0.05) is None
    hist = [{"t": 1000, "five_hour": 40}, {"t": 2800, "five_hour": 50}]
    assert abs(rate(hist, "five_hour", 60, 0, 4600) - 20 / 3600) < 1e-12 and rate(hist, "five_hour", 60, 3000, 4600) is None
    # Alerts: once per window, extra credits only from the second reading on and only when they rise
    now = 1_800_000_000.0
    iso = lambda t: datetime.fromtimestamp(t).astimezone().isoformat()
    L = {"five_hour": {"utilization": 80, "resets_at": iso(now + 7200)}, "seven_day": {"utilization": 60, "resets_at": iso(now + 3.5 * 86400)},
         "extra_usage": {"is_enabled": True, "used_credits": 100, "monthly_limit": 2500, "decimal_places": 2, "currency": "EUR"}}
    hist = [{"t": now - 3000, "five_hour": 40}]                   # 40 % per 50 min: full in 25 min, the week lands at 120 %
    msgs, st = alerts(L, hist, {}, now)
    assert sorted(t.split(":")[0] for t, _, _ in msgs) == ["five_hour", "seven_day"] and st["extra_used"] == 100, msgs
    sent = {**st, **{t: now for t, _, _ in msgs}}
    assert alerts(L, hist, sent, now + 60)[0] == []
    L["extra_usage"]["used_credits"] = 150
    msgs, st = alerts(L, hist, sent, now + 60)
    assert [t.split(":")[0] for t, _, _ in msgs] == ["extra"] and st["extra_used"] == 100  # state only after sending
    assert alerts(L, hist, {**st, msgs[0][0]: now}, now + 120) == ([], {**st, msgs[0][0]: now, "extra_used": 150})
    kinds = lambda s: [t.split(":")[0] for t, _, _ in alerts(L, hist, {}, now, {**SET, **s})[0]]
    # Free again within the hour after a full 5-hour window; weekly steps: only the highest one, each once
    L2 = {"five_hour": {"utilization": 100, "resets_at": iso(now + 600)}, "seven_day": {"utilization": 85, "resets_at": iso(now + 43200)}}
    m, st = alerts(L2, [], {}, now)
    assert [t.split(":")[0] for t, _, _ in m] == ["seven_day@80"] and st["full:five_hour"] == now + 600, (m, st)
    L2.update(five_hour={"utilization": 0, "resets_at": None}, seven_day={"utilization": 91, "resets_at": iso(now + 43200)})
    sent2 = {**st, m[0][0]: now}
    m2, st2 = alerts(L2, [], sent2, now + 700)
    assert sorted(t.split(":")[0] for t, _, _ in m2) == ["free", "seven_day@90"], m2
    assert alerts(L2, [], {**st2, **{t: now for t, _, _ in m2}}, now + 800)[0] == [] and alerts(L2, [], sent2, now + 4300)[0][0][0].startswith("seven_day@90")
    assert alerts(L2, [], sent2, now + 700, {**SET, "alert_free": False, "alert_steps": False})[0] == []
    sun = datetime(2026, 10, 11, 19, 0).timestamp()   # a Sunday evening
    fake = {"w": {"since": sun - 6 * 86400, "total": 10.0, "comp": {"think": 6.0, "tool:terminal": 4.0}, "ttl": 300},
            "30": {"hours": {sun - 10 * 86400: {"think": 8.0}, sun - 5 * 86400: {"think": 10.0}}}}
    dg = digest(sun, {}, {"seven_day": {"utilization": 70, "resets_at": iso(sun + 86400)}}, fake.get)
    assert dg[0] == "digest:2026-41" and "+25%" in dg[2] and "$10.00" in dg[2] and "70%" in dg[2], dg
    assert digest(sun, {dg[0]: sun}, {}, fake.get) is None and digest(sun - 3600 * 2, {}, {}, fake.get) is None
    assert digest(sun, {}, {}, fake.get, {**SET, "alert_digest": False}) is None
    assert kinds({"alert_five": False}) == kinds({"five_min": 20}) == ["seven_day"] and kinds({"alert_week": False}) == ["five_hour"]
    # Page loads never wait for a limits fetch once there is a reading; only the very first one is waited for
    saved, orig, calls = dict(STATE), refresh_limits, []
    globals()["refresh_limits"] = lambda age: (time.sleep(0.5), calls.append(age))
    STATE.update(limits={"seven_day": {}}, at=0.0)
    t0 = time.time(); fresh_limits(); assert time.time() - t0 < 0.25
    STATE.update(limits=None)
    t0 = time.time(); fresh_limits(); assert time.time() - t0 >= 0.5 and calls
    globals()["refresh_limits"] = orig
    STATE.clear(); STATE.update(saved)
    # Settings: a missing checkbox is off, invalid input keeps the old value, the token stays unless removed
    s = clean({"theme": "dark", "currency": "XXX", "five_min": "3", "ntfy_topic": "bad topic", "quiet_from": "25:00",
               "url": "javascript:x", "ntfy_token": ""}, {**SET, "ntfy_token": "tk"})
    assert (s["theme"], s["currency"], s["five_min"], s["ntfy_topic"], s["quiet_from"], s["url"], s["ntfy_token"], s["alert_five"]) == \
        ("dark", SET["currency"], 5, SET["ntfy_topic"], SET["quiet_from"], SET["url"], "tk", False), s
    assert clean({"token_clear": "1", "ntfy_token": "x"}, {**SET, "ntfy_token": "tk"})["ntfy_token"] == ""
    assert clean({"url": ""}, {**SET, "url": "https://a"})["url"] == "" and clean({"url": "https://a.b:8443/"}, SET)["url"] == "https://a.b:8443"
    qs, at = {**SET, "quiet": True, "quiet_from": "22:00", "quiet_to": "07:00"}, lambda h, m: datetime(2026, 10, 8, h, m).timestamp()
    assert quiet_now(at(23, 0), qs) and quiet_now(at(6, 59), qs) and not quiet_now(at(7, 0), qs) and not quiet_now(at(23, 0), SET)
    assert quiet_now(at(13, 0), {**qs, "quiet_from": "12:00", "quiet_to": "14:00"}) and not quiet_now(at(15, 0), {**qs, "quiet_from": "12:00", "quiet_to": "14:00"})
    assert same_origin({"Sec-Fetch-Site": "same-origin"}) and not same_origin({"Sec-Fetch-Site": "cross-site", "Origin": "http://h", "Host": "h"})
    assert same_origin({"Origin": "http://h:1", "Host": "h:1"}) and not same_origin({"Origin": "http://evil", "Host": "h:1"}) and not same_origin({"Host": "h"})
    assert re.fullmatch(r"usagecast-[0-9a-f]{16}", new_topic())
    # Every page renders in every language without leftover locale keys
    with tempfile.TemporaryDirectory() as tmp:
        HERMES, DATA = Path(tmp), Path(tmp) / "data"
        db, _ = test_db(time.time() - 3600)
        db.backup(disk := sqlite3.connect(Path(tmp) / "state.db"))
        disk.close()
        STATE["at"] = time.time()  # no limit fetch during the test
        # Source: only a profile with its own state.db can be chosen, anything else keeps the old value
        (HERMES / "profiles" / "work").mkdir(parents=True)
        (HERMES / "profiles" / "junk").mkdir()
        db.backup(pdb := sqlite3.connect(HERMES / "profiles" / "work" / "state.db"))
        pdb.close()
        assert profiles() == ["work"] and clean({"source": "work"}, SET)["source"] == "work"
        assert clean({"source": "../x"}, SET)["source"] == clean({"source": "junk"}, SET)["source"] == ""
        SET["source"] = "work"
        assert source_home() == HERMES / "profiles" / "work" and snap_path().name == "snapshot-work.json"
        assert fix("tools", "list") == '<code class="cmd fix">hermes -p work tools list</code>' and "work" in source_name()
        SET["source"] = "gone"
        assert source_home() == HERMES and fix("x") == '<code class="cmd fix">hermes x</code>' and source_name() == "Hermes"
        SET["source"] = ""
        assert idle_sets({"a": 2, "b": 1, "c": 5, "d": 1}, {"a": ["web"], "b": ["web", "search"], "c": ["browser"]}, {"b"}) \
            == [("browser", 5)]
        # Limits card: pace verdict and daily budget with half the week gone
        old, nw, _req.lang = STATE["limits"], time.time(), "en"
        STATE["limits"] = {"seven_day": {"utilization": 60, "resets_at": iso(nw + 3.5 * 86400)},
                           "five_hour": {"utilization": 30, "resets_at": iso(nw + 3600)}}
        STATE["limits"]["extra_usage"] = {"is_enabled": True, "used_credits": 501, "monthly_limit": 2500, "decimal_places": 2, "currency": "EUR"}
        RATE.update(k=0.5, hours=10)
        c = limits_card(None)
        assert "runs ~20% over, about $" in c and "Extra credits this month" in c, c
        RATE.clear()
        assert "Too fast:" in c and tr("lim.budget", pct=pc(40 / 3.5)) in c, c
        STATE["limits"]["seven_day"]["utilization"] = 40
        assert "On track:</strong> about 80%" in limits_card(None)
        STATE["limits"] = old
        cl = Path(tmp) / "claude" / "projects" / "-x"
        cl.mkdir(parents=True)
        msg = {"type": "assistant", "requestId": "r", "timestamp": "2026-10-08T10:00:00Z", "message": {"id": "m", "model": "claude-opus-5-5",
               "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 300, "output_tokens": 50,
                         "cache_creation": {"ephemeral_1h_input_tokens": 100}}}}
        (cl / "s.jsonl").write_text(json.dumps(msg) + "\n" + json.dumps(msg) + "\n" + '{"type": "user"}\n')
        CLAUDE = cl.parent
        pr = price("claude-opus-5-5")
        assert cc_calls(0) == [(datetime(2026, 10, 8, 10, tzinfo=timezone.utc).timestamp(), 10 * pr[0] + 1000 * pr[3] + 200 * pr[1] + 100 * pr[2] + 50 * pr[4])]
        (HERMES / "config.yaml").write_text("model:\n  default: m\nagent:\n  reasoning_effort: high\nplugins:\n  enabled:\n    - a\n")
        watch_config(); watch_config()
        assert events(0) == [] and len(read_jsonl(DATA / "config.jsonl")) == 1   # baseline only
        (HERMES / "config.yaml").write_text("model:\n  default: m\nagent:\n  reasoning_effort: xhigh\nplugins:\n  enabled:\n    - b\n")
        watch_config()
        ev = events(0)
        assert len(ev) == 1 and ev[0][1] == "Config: reasoning_effort high → xhigh, plugins +b −a", ev
        hs = {h: 10 for h in range(0, 40 * 3600, 3600)}
        d30 = {"hours": {h: {"x": 1.0 if h < 20 * 3600 else 2.0} for h in hs}, "hsteps": hs}
        assert per_step(d30, 0, 20 * 3600) == 0.1 and per_step(d30, 20 * 3600, 40 * 3600) == 0.2 and per_step(d30, 0, 3600) is None
        save_settings({**SET, "currency": "EUR"})
        assert load_settings()["currency"] == "EUR" and (DATA / "settings.json").stat().st_mode & 0o777 == 0o600
        SET["currency"] = "USD"
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def post(origin):
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/settings", b"theme=dark&action=save", {"Origin": origin})
            try:
                return urllib.request.urlopen(req, timeout=30).status
            except urllib.error.HTTPError as err:
                return err.code
        assert post("http://evil.example") == 403 and SET["theme"] == "system"
        assert post(f"http://127.0.0.1:{srv.server_port}") == 200 and load_settings()["theme"] == "dark"   # 303 to the page
        r = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/settings", b"quiet_from=25:00&action=save",
                                                          {"Origin": f"http://127.0.0.1:{srv.server_port}"}), timeout=30)
        assert r.url.endswith("bad=quiet_from") and 'role="alert"' in r.read().decode(), r.url
        def ingest(body, tok):
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api/ingest", body, {"Authorization": "Bearer " + tok} if tok else {})
            try:
                return urllib.request.urlopen(req, timeout=30).status
            except urllib.error.HTTPError as err:
                return err.code
        good = json.dumps({"host": "mac-1", "since": 7200, "hours": [[7205, "claude-opus-5-5", 10, 300, 1000, 50, 100]]}).encode()
        assert ingest(good, "tk") == 401 and parse_ingest(good)[0] == "mac-1"   # no token created yet
        SET["ingest_token"] = "tk"
        assert ingest(good, "") == 401 and ingest(good, "tkx") == 401 and ingest(b"{", "tk") == 400
        assert ingest(json.dumps({"host": "../x", "hours": []}).encode(), "tk") == 400
        assert ingest(json.dumps({"host": "a", "hours": [[1, "m", -1, 0, 0, 0]]}).encode(), "tk") == 400
        assert ingest(good, "tk") == 200 and ingest(good, "tk") == 200 and list(ingest_hosts()) == ["mac-1"]
        pr = price("claude-opus-5-5")
        want = 10 * pr[0] + 200 * pr[1] + 100 * pr[2] + 1000 * pr[3] + 50 * pr[4]
        assert [round(c, 6) for t, c in cc_calls(0) if t == 7200] == [round(want, 6)], cc_calls(0)
        assert urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/cc-report.py", timeout=30).read().startswith(b"#!/usr/bin/env python3")
        INGEST_SHOWN["at"] = time.time()
        assert "--token tk --install" in page_settings({"done": ["ingest"]}) and "--token TOKEN" in page_settings({})
        srv.shutdown()
        LAST_PUSH.update(at=time.time(), ok=False, reply="<x>")
        assert abs(sum(day_costs().values()) - real) < 1e-9, (day_costs(), real)   # the calendar loses nothing
        cal = calendar({(datetime.now().date() - timedelta(days=1)).isoformat(): 2.0})
        assert cal.count('class="h4" title') == 1 and cal.count("<i ") >= 365 + 5, cal[:200]
        (HERMES / "cron").mkdir()
        (HERMES / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
            {"id": "j1", "name": "Daily report", "state": "paused", "repeat": {"times": None}},
            {"id": "j2", "name": "Once", "state": "scheduled", "repeat": {"times": 1}}]}))
        (DATA / "guard.json").write_text(json.dumps({"paused": ["j1"]}))
        assert [j["id"] for j in cron_list()] == ["j1"] and "Daily report" in guard_note() and clean({"gj_j1": "on", "gj_j2": "on"}, SET)["guard_jobs"] == ["j1"]
        leftover = re.compile(r"\b(?:th|set|cal|ovl|ev|brk|ov|lim|det|ses|hist|heat|nav|kpi|seg|src|proj|cron|period|since|fmt|num|tip|comp|label|alert|until|sub)\.[a-z_]")
        for code in LOC:
            _req.lang, _req.url = code, "/?p=7"
            SET["ntfy_topic"] = "usagecast-test" if code != "en" else ""   # English: not set up yet, the others: subscribe steps
            cp = f"{datetime.now().date() - timedelta(days=1)}..{datetime.now().date()}"
            for page in [*(f(p) for f in (page_overview, page_history, page_details, page_projects) for p in [*PERIODS, cp]),
                         page_sessions("7", {}), page_sessions("7", {"view": ["cron"]}), page_sessions("7", {"proj": ["shop"]}),
                         page_session("s", "7"), page_settings({"done": ["save"]}), page_settings({"done": ["test"]})]:
                text = re.sub(r"<[^>]+>", " ", page)
                assert not leftover.search(text) and "{" not in text, (code, leftover.search(text), text[:300])
    assert "&lt;x&gt;" in page_settings({"done": ["test"]}) and 'data-theme="dark"' in page_settings({})
    # Extra credits: a drop in the counter starts a new month, steps come once per month and only the highest
    assert extra_months([{"t": i, "extra": v} for i, v in enumerate([None, 1.0, 2.5, 2.5, 0.0, 0.5, 3.0])]) == [[1, 2.5], [4, 3.0]]
    Lx = {"extra_usage": {"is_enabled": True, "used_credits": 2100, "monthly_limit": 2500, "decimal_places": 2, "currency": "EUR"}}
    _req.lang = "en"
    sx, tn = {**DEFAULTS, "extra_steps": "10, 20", "alert_extra": False}, 2e9
    m, st = alerts(Lx, [], {"extra_used": 2100}, tn, sx)
    assert [t for t, _, _ in m] == ["extra@20"] and m[0][1] == "Extra credits passed 20 EUR" and alerts(Lx, [], {**st, "extra@20": tn}, tn, sx)[0] == [], m
    Lx["extra_usage"]["used_credits"] = 100
    m, st = alerts(Lx, [], {"extra_used": 2100, "extra@20": tn - 20 * 86400}, tn, sx)
    assert m == [] and "extra@20" not in st and st["extra_used"] == 100, st
    Lx["extra_usage"]["used_credits"] = 1200
    assert [t for t, _, _ in alerts(Lx, [], st, tn, sx)[0]] == ["extra@10"] and alerts(Lx, [], st, tn, {**sx, "extra_steps": ""})[0] == []
    assert clean({"extra_steps": "10, 20.5"}, DEFAULTS)["extra_steps"] == "10, 20.5" and clean({"extra_steps": "ten"}, DEFAULTS)["extra_steps"] == ""
    RATE["k"] = 0.5
    assert overrun_usd(120) == 40 and overrun_usd(90) is None and overrun_usd(None) is None
    RATE.clear()
    # Budget guard: marked active jobs pause when the week gets tight, only its own come back, unknown limits change nothing
    tg = 2e9
    Lg = lambda wk, five, frac: {"seven_day": {"utilization": wk, "resets_at": iso(tg + (1 - frac) * 7 * 86400)},
                                 "five_hour": {"utilization": five, "resets_at": iso(tg + 3600)}}
    gs, jobs = {**DEFAULTS, "guard": True, "guard_jobs": ["a", "b", "u"]}, {"a": "scheduled", "b": "scheduled", "u": "paused", "c": "scheduled"}
    assert guard_plan(Lg(50, 10, 0.5), tg, jobs, {}, gs) == ([], []) and guard_plan(Lg(60, 10, 0.5), tg, jobs, {}, gs) == (["a", "b"], [])
    assert guard_plan(Lg(12, 10, 0.1), tg, jobs, {}, gs) == ([], []) and guard_plan(Lg(40, 85, 0.5), tg, jobs, {}, gs)[0] == ["a", "b"]
    assert guard_plan(Lg(90, 0, 0.95), tg, jobs, {}, gs)[0] == ["a", "b"] and guard_plan(Lg(60, 10, 0.5), tg, jobs, {}, {**gs, "guard": False}) == ([], [])
    held = {"a": "paused", "b": "paused", "u": "paused", "c": "scheduled"}
    assert guard_plan(Lg(50, 10, 0.5), tg, held, {"paused": ["a", "b"]}, gs) == ([], ["a", "b"])   # room again, u stays the user's
    assert guard_plan(Lg(60, 10, 0.5), tg, held, {"paused": ["a", "b"]}, {**gs, "guard_jobs": ["a"]}) == ([], ["b"])
    assert guard_plan(Lg(60, 10, 0.5), tg, held, {"paused": ["a"]}, {**gs, "guard": False}) == ([], ["a"])
    assert guard_plan(None, tg, held, {"paused": ["a"]}, gs) == ([], []) and guard_plan(Lg(50, 10, 0.5), tg, jobs, {"paused": ["a"]}, gs) == ([], [])
    # Weekly rhythm: a week that spends half its cost in the first day; the weighted forecast sees through it
    ws0, pat = 50 * 7 * 86400, [10.0] * 24 + [10.0 * 24 / 144] * 144
    hc = {ws0 - 7 * 86400 * i + 3600 * h: c * (1 + 0.1 * i) for i in range(1, 5) for h, c in enumerate(pat)}
    prof, bt = rhythm(hc, ws0, ws0 - 30 * 86400)
    assert prof and abs(sum(prof) - 1) < 1e-9 and abs(sum(prof[:24]) - 0.5) < 1e-9 and bt["wtd"] < 1 < bt["lin"], bt
    assert rhythm(hc, ws0, ws0 - 15 * 86400) == (None, None)   # two weeks are not enough
    FC.update(prof=prof, method="weighted")
    assert abs(week_forecast(50, 24 / 168) - 100) < 1e-6 and abs(week_forecast(60, 0.5) - 60 / (0.5 + 0.5 * 60 / 144)) < 1e-6
    FC["method"] = "linear"
    assert abs(week_forecast(50, 0.5) - 100) < 1e-9
    FC.clear()
    # Turns and the chat/spike watch
    assert split_turns([(5, 1.0), (12, 2.0), (13, 1.0), (30, 4.0)], [0, 10, 20]) == [(0, 5, 1, 1.0), (10, 13, 2, 3.0), (20, 30, 1, 4.0)]
    _req.lang, T0, ws = "en", 1e6, {**DEFAULTS, "chat_pct": 3.0, "spike_pct": 3.0}
    ss = lambda src, cost, costs, last, org=None, st=0: dict(src=src, title="T", cost=cost, last=last, origin=org or src, started=st,
                                                         turns=[(T0 + i, T0 + i, 3, c) for i, c in enumerate(costs)])
    d30w = {"since": 0, "sess": {"a": ss("telegram", 16, [1, 1, 1, 4, 4, 4, 1], T0 + 100), "b": ss("cli", 15, [1, 1, 1, 1, 1, 10], T0 + 100),
                                 "old": ss("telegram", 99, [9, 9, 9, 9], T0 - 1300),
                                 **{f"c{i}": ss("cron", 1.0, [], 0, "cron:job", i) for i in range(3)}, "c9": ss("cron", 10.0, [], T0 + 50, "cron:job", 9)}}
    wm = watch(d30w, T0 + 200, {}, 1.0, ws)
    assert [x[0] for x in wm] == ["chat:a", "spike:b:1000005", "spike:c9"] and "4.0%" in wm[0][2] and "~1.0%" in wm[0][2] and wm[0][3] == "/s/a", wm
    assert "~1.0%" in wm[2][2] and "6 steps" not in wm[1][2] and "3 steps" in wm[1][2], wm
    assert watch(d30w, T0 + 200, {x[0]: 1 for x in wm}, 1.0, ws) == [] and watch(d30w, T0 + 200, {}, None, ws) == []
    assert watch(d30w, T0 + 200, {}, 1.0, {**ws, "alert_chat": False, "alert_spike": False}) == []
    assert clean({"chat_pct": "1,5", "spike_pct": "nan"}, DEFAULTS)["chat_pct"] == 1.5 and clean({"spike_pct": "nan"}, DEFAULTS)["spike_pct"] == 3.0
    db, _ = test_db()
    tu = analyze(db, -1, snap, 300, logs={})["sess"]["s"]["turns"]
    assert [(a, b, n) for a, b, n, _ in tu] == [(0.0, 2.0, 2), (1000.0, 1000.0, 1)], tu
    assert VERSION == re.search(r"^## (\S+)", (ROOT / "CHANGELOG.md").read_text(), re.M)[1], "VERSION and CHANGELOG.md disagree"
    assert demo_db(time.time()).execute("select count(*) from sessions where source = 'cron'").fetchone()[0] > 10   # --demo data builds
    print("selftest ok")


if __name__ == "__main__":
    if "--test" in sys.argv:
        selftest()
    elif "--demo" in sys.argv:
        demo()
    elif "--ntfy-test" in sys.argv:
        print("sent" if notify(tr("alert.test"), tr("alert.test.text")) else "not sent: ntfy is not configured or not reachable")
    elif "--snapshot" in sys.argv:
        snapshot()
        sys.stdout.flush()
        os._exit(0)  # AIAgent can leave background threads (MCP) running
    else:
        threading.Thread(target=background, daemon=True).start()
        threading.Thread(target=warm, daemon=True).start()
        print(f"usagecast on http://127.0.0.1:{PORT}", flush=True)
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
