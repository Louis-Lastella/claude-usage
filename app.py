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
import bisect, glob, gzip, html, json, math, os, re, sqlite3, subprocess, sys, tempfile, threading, time, traceback, urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

ROOT = Path(__file__).resolve().parent
HERMES = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
HERMES_PY = HERMES / "hermes-agent" / "venv" / "bin" / "python"
DATA = Path(os.environ.get("USAGECAST_DATA", ROOT / "data"))
PORT = int(os.environ.get("PORT", "7682"))
PUBLIC_URL = os.environ.get("USAGECAST_URL", "")   # dashboard address, opened when an alert is tapped
REPO_URL = "https://github.com/Louis-Lastella/usagecast"
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
    return getattr(_req, "lang", None) or DEFAULT_LANG


def tr(key, **kw):
    """Text for `key` in the current language; falls back to English, then to the key itself."""
    s = LOC[lang()].get(key)
    s = LOC["en"].get(key, key) if s is None else s
    return s.format(**kw) if kw else s


def pick_lang(wanted, headers):
    """?lang= beats the cookie, then USAGECAST_LANG (English). English first: the browser language is not used."""
    if wanted in LOC:
        return wanted
    m = re.search(r"(?:^|;)\s*lang=([a-z]+)", headers.get("Cookie", ""))
    return m.group(1) if m and m.group(1) in LOC else DEFAULT_LANG


def nf(v, digits=0):
    """Number with the language's thousands and decimal separators."""
    return f"{v:,.{digits}f}".translate(str.maketrans(",.", tr("num.sep")))


def money(v):
    return "< " + tr("fmt.money", v=nf(0.01, 2)) if 0 < v < 0.01 else tr("fmt.money", v=nf(v, 2))


def pct(v, tot):
    return tr("fmt.pct", v=nf(v / (tot or 1) * 100, 1))


def pc(u):
    return tr("fmt.pct", v=nf(u))


def num(n):
    n = float(n or 0)
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


def price(model):
    key = max((k for k in PRICES if model.startswith(k)), key=len, default="claude-opus-5-5")
    return [p / 1e6 for p in PRICES[key]]


def row_cost(model, i, r, w, o, ttl):
    """Cost of one usage row in $: (input, cache read, cache write, output)."""
    p = price(model)
    return (i or 0) * p[0], (r or 0) * p[3], (w or 0) * (p[2] if ttl > 300 else p[1]), (o or 0) * p[4]


def config_text():
    try:
        return (HERMES / "config.yaml").read_text()
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
    soul = "soul" if (HERMES / "SOUL.md").is_file() else "base"
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
    """Language-neutral origin id: the source, or cron:<job name> for cron sessions."""
    if src == "cron":
        return "cron:" + ((title or "").split(" · ")[0] or "?")
    return src or "?"


def floor(t, hourly):
    if hourly:
        return t - t % 3600
    return datetime.fromtimestamp(t).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def analyze(c, since, snap, ttl, sid=None, logs=None):
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

    hourly = sid is None and since > time.time() - 2 * 86400
    comp, where = defaultdict(float), defaultdict(float)
    hours = defaultdict(lambda: defaultdict(float))       # hour start -> component -> $
    hsteps, hsess = defaultdict(int), defaultdict(set)    # hour start -> steps, sessions
    models = defaultdict(lambda: [0.0, 0.0, 0.0])        # calls, tokens, $
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
                # for the cache duration tip: what 1 h instead of 5 min (or the other way round) would have changed
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
                       project=project_of((m[4] for m in msgs.get(s, ()) if m[4]), repos, prx, repo_root))
    # 5 min -> 1 h: rebuilds after pauses up to 1 h disappear, every write costs 2/1.25 = 1.6x. The other way round accordingly.
    if ttl <= 300:
        ttl_save, ttl_alt = tot["avoid"] - (tot["writes"] - tot["avoid"]) * 0.6, 3600
    else:
        ttl_save, ttl_alt = tot["writes"] * 0.375 - tot["extra"] * 0.625, 300
    return dict(comp=comp, where=where, models=models, hours=hours, hsteps=hsteps, hsess=hsess, sess=sess, steps=steps,
                uses=uses, sizes=sizes_acc, total=sum(comp.values()), ttl=ttl, ttl_alt=ttl_alt, ttl_save=ttl_save,
                since=since, hourly=hourly, **tot)


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
        return tr("origin.cron", name=k[5:])
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


def tips(d, snap):
    """[(weight, title, html text)], heaviest first."""
    tot, out = d["total"] or 1, []
    p = lambda v: pct(v, tot)
    save = d["ttl_save"]
    if save > 0.03 * tot:
        key = "tip.ttl1h" if d["ttl_alt"] > 300 else "tip.ttl5m"
        out.append((save, tr(key), tr(key + ".text", p=p(save))))
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
        out.append((s, tr("tip.unused"), tr("tip.unused.text", n=len(unused), p=p(s), top=top)))
    if d["long"] > 0.2 * tot:
        out.append((d["long"] / 2, tr("tip.long"), tr("tip.long.text", p=p(d["long"]), n=nf(LONG_CTX))))
    think = d["comp"].get("think", 0)
    effort = config_value("agent", "reasoning_effort")
    if think > 0.15 * tot and effort in ("xhigh", "max", "high"):
        out.append((think * 0.3, tr("tip.think"), tr("tip.think.text", p=p(think), effort=html.escape(effort),
                                                       highest=tr("tip.think.highest") if effort in ("xhigh", "max") else "")))
    bg = d["comp"].get("task:background_review", 0)
    if bg > 0.05 * tot:  # agent.log shows the review reads the history with its own prompt, so without cache hits
        out.append((bg, tr("tip.review"), tr("tip.review.text", p=p(bg))))
    tl = sorted(((k, v) for k, v in d["comp"].items() if k.startswith("tool:")), key=lambda x: -x[1])
    if tl and tl[0][1] > 0.08 * tot:
        name = tl[0][0][5:]
        hint = tr("tip.tool." + name) if name in TOOL_HINTS else tr("tip.tool.any", name=html.escape(name))
        out.append((tl[0][1] / 3, tr("tip.tool", name=name), hint + " " + tr("tip.share", p=p(tl[0][1]))))
    for org, v in sorted(d["where"].items(), key=lambda x: -x[1]):
        if org.startswith("cron:") and v > 0.05 * tot:
            out.append((v / 2, tr("tip.cron", name=org[5:]), tr("tip.cron.text", p=p(v))))
    for k, v in d["comp"].items():
        if k.startswith("inj:") and v > 0.03 * tot:
            out.append((v, tr("tip.inj", name=seg_label(k[4:])), tr("tip.inj.text", p=p(v))))
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
        DATA.mkdir(parents=True, exist_ok=True)
        with open(DATA / "limits.jsonl", "a") as f:
            f.write(json.dumps(row) + "\n")


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
    """Utilization at the reset, linear from the pace since the window started. Meaningful after half a day."""
    return u / frac if frac and frac >= 1 / 14 else None


def rate(hist, key, u, start, now):
    """Usage in % per second over the last hour, only from points of this window; None without 10 min of data."""
    pts = [h for h in hist if h["t"] >= max(start, now - 3600) and h.get(key) is not None]
    if not pts or now - pts[0]["t"] < 600:
        return None
    return max((u - pts[0][key]) / (now - pts[0]["t"]), 0.0)


def alerts(L, hist, sent, now):
    """Due alerts [(tag, title, text)] and the new state. A tag stands for one window; the caller stores it after a
    successful send, so every alert comes at most once per window."""
    sent, out, full = {k: v for k, v in sent.items() if k == "extra_used" or now - v < 8 * 86400}, [], []
    for key, u, frac, reset, length in windows(L, now):
        name = tr("win." + key)
        if u >= 100:
            full.append(name)
        if not reset or u >= 100 or f"{key}:{reset:.0f}" in sent:
            continue
        tag = f"{key}:{reset:.0f}"
        if length <= 5 * 3600:
            r = rate(hist, key, u, reset - length, now)
            fa = now + (100 - u) / r if r else None
            if fa and fa - now < 1800 and fa < reset:
                out.append((tag, tr("alert.five", name=name), tr("alert.five.text", u=pc(u), full=hm(fa), reset=hm(reset))))
        else:
            fc = week_forecast(u, frac)
            if fc and fc > 100 and frac >= 1 / 7:
                start = reset - length
                out.append((tag, tr("alert.week", name=name),
                            tr("alert.week.text", u=pc(u), frac=pc(frac * 100), full=when(start + (now - start) * 100 / u, "daytime"),
                               reset=when(reset, "daytime"))))
    x = (L or {}).get("extra_usage") or {}
    used = x.get("used_credits")
    if x.get("is_enabled") and used is not None:
        prev = sent.get("extra_used")
        reset = next((w[3] for w in windows(L, now) if w[3]), 0)  # 5-hour window first, otherwise the week
        tag = f"extra:{reset:.0f}"
        if prev is not None and used > prev and tag not in sent:  # the state stays old until the alert went out
            dp = 10 ** (x.get("decimal_places") or 0)
            out.append((tag, tr("alert.extra"), tr("alert.extra.text", used=nf(used / dp, 2),
                                                     limit=nf((x.get("monthly_limit") or 0) / dp, 2), cur=x.get("currency") or "",
                                                     full=tr("alert.full", names=", ".join(full)) + " " if full else "")))
        else:
            sent["extra_used"] = used
    return out, sent


def ntfy_target():
    """(server, topic, token) from USAGECAST_NTFY (full URL) or Hermes' ntfy settings in .env, otherwise None."""
    url, token = os.environ.get("USAGECAST_NTFY", ""), None
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
    """Runs in the Hermes venv: measures system prompt parts and tool schemas as Hermes currently sends them.
    It only builds Hermes' agent object and reads its prompt and tool list; no request goes to a model. The platform
    is the one most sessions come from, because the tool selection depends on it."""
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
STATIC = ROOT / "static"
STATIC_FILES = {"style.css": "text/css; charset=utf-8", "manifest.webmanifest": "application/manifest+json",
                "icon.svg": "image/svg+xml", "icon-180.png": "image/png", "icon-192.png": "image/png", "icon-512.png": "image/png"}
ICONS = {  # 24 grid, stroke in currentColor
    "/": '<path d="M4 16a8 8 0 1 1 16 0"/><path d="M12 16l3.5-4.5"/>',
    "/history": '<path d="M5 19v-7M10 19V6M15 19v-4M20 19V9"/>',
    "/details": '<path d="M9 7h11M9 12h11M9 17h11M4.5 7h.01M4.5 12h.01M4.5 17h.01"/>',
    "/sessions": '<path d="M20 14.5a2 2 0 0 1-2 2H8.5L4 20V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z"/>',
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
        out += (f'<div class="bar"><div class="row"><span>{brk(name)}</span><span class="v">{pct(v, tot)} · {money(v)}</span></div>'
                f'<div class="t{" soft" if soft else ""}"><i style="width:{v / top * 100:.1f}%"></i></div>'
                + (f'<div class="why">{e(why)}</div>' if explain and why and (k in FIXED or not k.startswith("tool:")) else "")
                + "</div>")  # the sentence for tools would otherwise repeat on every tool
    rest = sum(v for _, v in items[n:])
    if rest:
        more = f'<a href="{link("/details", p=p)}">{tr("nav.details")}</a>' if p else tr("nav.details")
        out += f'<p class="hint">{tr("bars.rest", n=len(items) - n, p=pct(rest, tot), details=more)}</p>'
    return out


def until(t):
    if not t:
        return ""
    s = t - time.time()
    if s < 3600:
        return tr("until.min", m=max(int(s // 60), 1))
    if s < 86400:
        return tr("until.hm", h=int(s // 3600), m=int(s % 3600 // 60))
    return tr("until.day", at=when(t, "daytime"))


def meter(u, frac=None, hot=False):
    mark = (f'<b class="now" style="left:{frac * 100:.1f}%" title="{tr("lim.elapsed", p=pc(frac * 100))}"></b>'
            if frac is not None else "")
    return f'<div class="meter{" hot" if hot else ""}"><i style="width:{min(max(u, 0), 100):.1f}%"></i>{mark}</div>'


def limits_card(week_cost):
    refresh_limits(120)
    L, now = STATE["limits"], time.time()
    if not L:
        return f'<section class="card"><h2>{tr("lim.title")}</h2><p class="hint">{tr("lim.unavailable")}</p></section>'
    hist, left, right = limit_history(), "", ""
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
                txt = tr("lim.week.lands", fc=pc(fc), frac=pc(frac * 100), reset=until(reset))
            left = (f'<div class="lbl">{tr("lim.week")}</div><div class="big">{nf(u)}<span>{tr("lim.unit")}</span></div>'
                    f'{meter(u, frac, u >= 80 or (fc or 0) > 100)}<p class="fc">{txt}</p>')
            if week_cost is not None:
                left += f'<p class="why">{tr("lim.weekcost", cost=money(week_cost))}</p>'
            continue
        hot = u >= 80
        if u >= 100:
            txt = tr("lim.full", reset=until(reset))
        elif length <= 5 * 3600:
            r = rate(hist, key, u, start, now) if reset else None
            fa = now + (100 - u) / r if r else None
            if fa and fa < reset:
                txt, hot = tr("lim.five.full_at", full=hm(fa), reset=until(reset)), hot or fa - now < 1800
            elif r is not None:
                txt = tr("lim.five.enough", reset=hm(reset), at=pc(min(u + r * (reset - now), 100)))
            else:
                txt = until(reset)
        else:
            fc = week_forecast(u, frac)
            txt = (tr("lim.forecast", fc=pc(fc)) + " " if fc else "") + until(reset)
        right += (f'<div class="lim"><div class="row"><span>{tr("win." + key)}</span><b>{pc(u)}</b></div>'
                  f'{meter(u, frac, hot)}<div class="why">{txt}</div></div>')
    x = L.get("extra_usage") or {}
    if x.get("is_enabled") and x.get("monthly_limit"):
        dp, used = 10 ** (x.get("decimal_places") or 0), x.get("used_credits") or 0
        value = tr("lim.extra.value", used=nf(used / dp, 2), limit=nf(x["monthly_limit"] / dp, 2), cur=e(x.get("currency") or ""))
        right += (f'<div class="lim"><div class="row"><span>{tr("lim.extra")}</span><b>{value}</b></div>'
                  f'{meter(used / x["monthly_limit"] * 100)}<div class="why">{tr("lim.extra.why")}</div></div>')
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
    return (f'<section class="card hero" aria-label="{tr("lim.title")}"><div>{left}</div><div>{right}'
            f'<p class="why">{stamp}</p></div>{spark}</section>')


def layout(title, active, p, h1, sub, body, tabs=True, keep=None):
    """h1 and sub are already escaped. keep: filters that survive a period switch."""
    nav = "".join(f'<div class="grp">{tr(g)}</div>' + "".join(
        f'<a href="{link(href, p=p)}"{" class=on aria-current=page" if href == active else ""}>'
        f'<svg viewBox="0 0 24 24" aria-hidden="true">{ICONS[href]}</svg>{tr(name)}</a>' for href, name in items) for g, items in NAV)
    seg = "".join(chip(tr("period." + k), k == p, link(active, p=k, **(keep or {}))) for k in PERIODS)
    seg = f'<nav class="seg" aria-label="{tr("aria.period")}">{seg}</nav>' if tabs else ""
    foot = (tr("foot.limits", at=hm(STATE["ok"]) if STATE["ok"] else "–") + "<br>"
            + tr("foot.ntfy.on" if ntfy_target() else "foot.ntfy.off"))
    u = urlparse(getattr(_req, "url", "/"))
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    langs = []
    for code in LOC:
        q["lang"] = code
        on = " class=on aria-current=true" if code == lang() else ""
        langs.append(f'<a href="{e(link(u.path, **q))}" hreflang="{code}" lang="{code}"{on}>{LOC[code]["lang.name"]}</a>')
    try:
        v = int((STATIC / "style.css").stat().st_mtime)
    except OSError:
        v = 0
    return f"""<!doctype html><html lang="{lang()}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>{e(title)}</title>
<link rel="stylesheet" href="/style.css?v={v}"><link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/icon-180.png"><link rel="manifest" href="/manifest.webmanifest">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Usagecast">
<meta name="theme-color" content="#f7f3ec" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#1b1713" media="(prefers-color-scheme: dark)"></head>
<body><div class="app"><aside><a class="brand" href="{link("/", p=p)}"><span class="logo">{LOGO}</span><span>Usagecast</span></a>
<nav aria-label="{tr("aria.pages")}">{nav}</nav><div class="foot">{foot}</div></aside>
<main><header class="top"><div><h1>{h1}</h1><p class="sub">{sub}</p></div>{seg}</header>
{body}
<footer class="pf"><nav aria-label="{tr("aria.lang")}">{" · ".join(langs)}</nav><a href="{REPO_URL}">Usagecast</a></footer></main></div></body></html>"""


CACHE, LOCK = {}, threading.Lock()


def connect():
    return sqlite3.connect(f"file:{HERMES / 'state.db'}?mode=ro", uri=True, timeout=15)


def since_for(p):
    """(start of the period, whether it really is the limit week)."""
    if p == "w":
        refresh_limits(120)
        try:
            return datetime.fromisoformat(STATE["limits"]["seven_day"]["resets_at"]).timestamp() - 7 * 86400, True
        except (KeyError, TypeError, ValueError):
            pass
    return time.time() - PERIODS[p] * 86400, False


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
        return tr("since.w", at=when(d["since"], "daytime"))
    return tr("since." + (d["p"] if d["p"] in ("1", "30") else "7"))


def window_sum(d, since):
    h0 = since - since % 3600
    return sum(sum(v.values()) for h, v in d["hours"].items() if h >= h0)


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
    kpis = [("main", tr("period." + p), money(d["total"]), tr("kpi.main.sub", tokens=num(d["tokens"]), steps=cnt(d["calls"])))]
    kpis += [("", tr("period." + k), money(sums[k]), tr("kpi.api")) for k in [k for k in ("7", "30", "1") if k != p][:2]]
    kpis += [("", tr("kpi.avg"), money(d30["total"] / max(len(active), 1)), tr("kpi.avg.sub", n=len(active))),
             ("", tr("kpi.sessions"), cnt(len(d["sess"])), tr("kpi.sessions.sub"))]
    kpi_html = "".join(f'<div class="kpi {c}"><span>{l}</span><b>{v}</b><small>{s}</small></div>' for c, l, v, s in kpis)
    tip_html = "".join(f'<div class="tip"><b>{e(t)}</b><p>{txt}</p></div>' for _, t, txt in tips(d, snap)) \
        or f'<p class="hint">{tr("ov.nothing")}</p>'
    where = sorted(d["where"].items(), key=lambda x: -x[1])
    models = sorted(d["models"].items(), key=lambda x: -x[1][2])
    models_html = '<ol class="rank">' + "".join(
        f'<li><span class="n">{i}</span><span class="m">{brk(m)}</span><span class="v">{money(x[2])}</span><b>{pct(x[2], tot)}</b></li>'
        for i, (m, x) in enumerate(models, 1)) + "</ol>"
    logged = tr("ov.logged", p=pc(d["logged"] / (d["n_steps"] or 1) * 100))
    body = f"""{limits_card(w["total"] if w["week"] else None)}
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
    return layout("Usagecast", "/", p, tr("ov.h1"), tr("sub.calc", period=period_text(d), at=hm(d["at"])), body)


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
    while t <= time.time():
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
{stacked(d)}</section>
<section class="card"><h2>{tr("hist.when")}</h2>
<p class="hint">{tr("hist.when.hint")}</p>{heatmap(d30)}</section>
<section class="sec"><h2>{tr("hist.days")}</h2>
{table(["th.day", "th.cost", "th.share", "th.steps", "th.sessions", "th.top"], rows, ("", "", "o", "", "o", "l o"))}</section>"""
    return layout(tr("nav.history") + " · Usagecast", "/history", p, tr("nav.history"), tr("sub.hermes", period=period_text(d)), body)


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
    body = f"""<section><h2>{tr("det.tools")}</h2>
<p class="hint">{tr("det.tools.hint")}</p>
{table(["th.tool", "th.calls", "th.avg_return", "th.cost", "th.share"], [trow(k, v) for k, v in tools], ("", "", "", "", "o"))}</section>
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
<section class="sec"><h2>{tr("det.how")}</h2>
<p class="hint">{how}</p></section>"""
    return layout(tr("nav.details") + " · Usagecast", "/details", p, tr("nav.details"), tr("sub.details", period=period_text(d)), body)


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
        if x["origin"].startswith("cron:"):
            a = g[x["origin"][5:]]
            a[0] += 1; a[1] += x["cost"]
    rows = sorted(g.items(), key=lambda kv: -kv[1][1])
    s = sum(c for _, (_, c) in rows)
    hint = tr("cron.hint", n=len(rows), cost=money(s), p=pct(s, d["total"]), week=money(s / days * 7))
    return (f'<p class="hint">{hint}</p>'
            + table(["th.cronjob", "th.schedule", "th.runs", "th.per_run", "th.cost", "th.per_week"],
                    [(f'<a href="{link("/sessions", p=p, src="cron", q=n)}">{e(n)}</a>',
                      f"<code>{e(sched[n])}</code>" if sched.get(n) else "–", cnt(r), money(c / r), money(c), money(c / days * 7))
                     for n, (r, c) in rows], ("", "l o", "", "o", "", "")))


def page_sessions(p, q):
    d = data_for(p)
    tot = d["total"] or 1
    arg = lambda k: (q.get(k) or [""])[0].strip()
    view, src, proj, term = arg("view"), arg("src"), arg("proj"), arg("q")
    tabs = (f'<nav class="chips tabs" aria-label="{tr("aria.view")}">' + chip(tr("ses.all"), view != "cron", link("/sessions", p=p))
            + chip(tr("ses.cron"), view == "cron", link("/sessions", p=p, view="cron")) + "</nav>")
    sub = tr("sub.hermes", period=period_text(d))
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
    return layout(tr("nav.projects") + " · Usagecast", "/projects", p, tr("nav.projects"), tr("sub.projects", period=period_text(d)), body)


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
              f'{money(r)} <span class="why">{tr("ses.after_pause" if kind == "rebuild" else "ses.no_pause")}</span>' if kind else "",
              f'<span class="why">{brk(names(keys))}</span>') for t, ctx, cost, r, kind, keys in d["steps"]]
    sub = " · ".join([f'<a href="{link("/sessions", p=p)}">{tr("ses.back")}</a>', e(origin_label(x["origin"])),
                      tr("ses.project", name=e(proj_label(x["project"] or NO_PROJ))),
                      tr("ses.started", at=when(x["started"] or 0, "datetime")), tr("ses.steps", n=cnt(x["calls"])), money(x["cost"])])
    logged = tr("ses.logged", p=pc(d["logged"] / d["n_steps"] * 100)) if d["n_steps"] else ""
    body = f"""<section class="card"><h2>{tr("ses.eats")}</h2>
{bars(ranked(d["comp"], group_skills=False), tot, ttl, explain=False, n=40)}</section>
<section class="sec"><h2>{tr("ses.steps_h")}</h2>
<p class="hint">{tr("ses.steps.hint", n=len(rb), cost=money(sum(s[3] for s in rb)))} {logged}</p>
{table(["th.time", "th.history", "th.cost", "th.rebuild", "th.tools"], steps, ("", "", "", "", "l"))}</section>"""
    title = x["title"] or tr("untitled")
    return layout(f"{title} · Usagecast", "/sessions", p, e(title), sub, body, tabs=False)


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
        p = q.get("p", [DEFAULT_P])[0]
        p = p if p in PERIODS else DEFAULT_P
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


def selftest():
    global HERMES, DATA
    # Locales: same keys and placeholders everywhere, every literal key used in the code exists
    src = Path(__file__).read_text("utf-8")
    used = set(re.findall(r'tr\(\s*"([\w.:-]+)"(?=\s*[,)])', src)) | set(re.findall(r'"(th\.[\w]+)"', src))  # "x." + k: below
    used |= {f"comp.{k}" for k in FIXED} | {f"comp.{k}.why" for k in FIXED} | {f"tip.tool.{k}" for k in TOOL_HINTS}
    used |= {f"period.{k}" for k in PERIODS} | {f"win.{k}" for k, _ in WINDOWS} | {f"since.{k}" for k in ("w", "1", "7", "30")}
    used |= {f"seg.{k}" for k, _ in HERMES_MARKERS} | {"seg.soul", "seg.misc", "seg.notes", "tip.ttl1h.text", "tip.ttl5m.text"}
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
    assert 0 < late["total"] < d["total"] and late["comp"]["rebuild"] > 0 and "tool:terminal" not in late["uses"]
    logged = analyze(db, -1, snap, 300, logs={"s": ([1.0, 3.0, 1001.0], [(5000, 0), (6000, 0), (6100, 0)])})
    assert logged["comp"]["break"] > 0 and abs(logged["total"] - real) < 1e-12 and logged["logged"] == 3  # break without pause
    assert abs(sum(sum(v.values()) for v in d["hours"].values()) - d["total"]) < 1e-12        # the history adds up
    assert sum(d["hsteps"].values()) == d["n_steps"] == 3
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
    # Every page renders in every language without leftover locale keys
    with tempfile.TemporaryDirectory() as tmp:
        HERMES, DATA = Path(tmp), Path(tmp) / "data"
        db, _ = test_db(time.time() - 3600)
        db.backup(disk := sqlite3.connect(Path(tmp) / "state.db"))
        disk.close()
        STATE["at"] = time.time()  # no limit fetch during the test
        leftover = re.compile(r"\b(?:th|ov|lim|det|ses|hist|heat|nav|kpi|seg|src|proj|cron|period|since|fmt|num|tip|comp|label|alert|until|sub)\.[a-z_]")
        for code in LOC:
            _req.lang, _req.url = code, "/?p=7"
            for page in [*(f(p) for f in (page_overview, page_history, page_details, page_projects) for p in PERIODS),
                         page_sessions("7", {}), page_sessions("7", {"view": ["cron"]}), page_sessions("7", {"proj": ["shop"]}),
                         page_session("s", "7")]:
                text = re.sub(r"<[^>]+>", " ", page)
                assert not leftover.search(text) and "{" not in text, (code, leftover.search(text), text[:300])
    print("selftest ok")


if __name__ == "__main__":
    if "--test" in sys.argv:
        selftest()
    elif "--ntfy-test" in sys.argv:
        print("sent" if notify(tr("alert.test"), tr("alert.test.text")) else "not sent: ntfy is not configured or not reachable")
    elif "--snapshot" in sys.argv:
        snapshot()
        sys.stdout.flush()
        os._exit(0)  # AIAgent can leave background threads (MCP) running
    else:
        threading.Thread(target=background, daemon=True).start()
        print(f"usagecast on http://127.0.0.1:{PORT}", flush=True)
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
