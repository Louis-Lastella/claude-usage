# Usagecast

**English** · [Deutsch](README.de.md)

A self-hosted dashboard that shows what eats your AI usage. Right now it reads
[Hermes Agent](https://github.com/NousResearch/hermes-agent) with Anthropic Claude and breaks the cost down into every
tool, skill, plugin, system prompt part, thinking, background review, cache rebuilds after pauses and cache breaks. On
top of that it shows your subscription limits (5 hours, week, extra credits) with a forecast, sends alerts to your phone
and gives concrete tips from your own data.

Pure Python standard library. It reads `~/.hermes/state.db` and `~/.hermes/logs/agent.log*` read-only. No AI involved:
ranking, breakdown, forecast and tips are fixed calculation rules, and no request ever goes to a model.

It works with any Hermes installation. It reads the active plugins and memory provider from `config.yaml`
(`plugins.enabled`, `memory.provider`) and assigns their parts of the system prompt and of your messages accordingly.
It also detects SOUL.md, Hermes' own notes and where a session came from (Telegram, Discord, Slack, cron jobs ...).

## Pages

| Page | Content |
|---|---|
| `/` | Limits with forecast, key figures, what eats the most, origins, models, tips |
| `/history` | Cost per day by the biggest items, heatmap by weekday and hour, day-by-day table |
| `/details` | Every tool, skill, plugin, system prompt part and tool description on its own |
| `/sessions` | Most expensive sessions with origin filter and search, cron tab with cost per run and per week |
| `/projects` | Cost per project, tap one to see its sessions |

Period with `?p=w` (since the last weekly limit reset, default), `?p=1`, `?p=7` or `?p=30`.

Light and dark mode follow the system setting. On an iPhone, open it in Safari and choose "Add to Home Screen" to start
it like an app.

## Languages

English is the default. Switch to German with the link at the bottom of every page or with `?lang=de`, the choice is
kept in a cookie. `USAGECAST_LANG=de` makes German the default for the dashboard and the alerts.

All text lives in `locales/<code>.json`. For a new language, copy `locales/en.json`, translate the values and run
`python3 app.py --test`. The self-test checks that every language has the same keys and placeholders and that every page
renders without leftover keys.

## How it calculates

1. **Real cost:** `session_model_usage` holds the tokens Anthropic reported for every session (input, cache read, cache
   write, output). With the official API price list (`PRICES` in `app.py`) this gives the exact API value. On a Pro or
   Max plan Anthropic counts the limit with the same weights.
2. **Breakdown:** Every session is replayed step by step. For each API call it is known what was in the prompt (system
   prompt parts, tool descriptions, every tool result, plugin injections, messages, thinking), what was new and what
   came from the cache.
3. **Cache:** Where Hermes' `agent.log` has the call (`in=… cache=…`), the real cache value counts. That also reveals
   cache breaks without a pause. Otherwise the rule applies: after a pause longer than `cache_ttl` (5 min or 1 h) the
   cache is gone.
4. **Calibration:** Sizes come from text length (images as a flat amount) and are scaled to the real token counts per
   session. The sum of all items always equals the real cost exactly (the self-test checks this).
5. **Period:** Cost counts by the time of each single step. A session that started before the period only counts with
   the part that falls into it.

Thinking that Hermes does not store as text is filled in from the difference to the real output and stays in the
history like stored thinking.

**Projects:** Hermes only stores a working folder for terminal sessions. Every other session counts toward the project
whose path shows up most often in its tool calls: folders under `/opt` and `/srv` and Git repos in the home folder (one
level deeper too, like `~/projects/app`). Hermes' own folder only counts when hardly anything else shows up.

**Forecast:** The week is extrapolated linearly from the pace since the last reset, the 5-hour window from the pace of
the last hour.

## Alerts via ntfy

Every 10 minutes the server checks the limits and sends at most one push message per window when

- the weekly forecast is above 100 % (at the earliest one day after the reset),
- the 5-hour window will be full in less than 30 minutes at the pace of the last hour,
- extra credits start being used.

The target comes from Hermes' ntfy settings (`NTFY_HOME_CHANNEL`, `NTFY_SERVER_URL`, `NTFY_TOKEN` in `~/.hermes/.env`)
or from `USAGECAST_NTFY` (full URL with topic). Without either, alerts stay off.

## Running it

```bash
python3 app.py --test        # self-test
python3 app.py --ntfy-test   # send a test alert to ntfy
PORT=7681 python3 app.py     # server on 127.0.0.1:7681
```

| Variable | Meaning |
|---|---|
| `PORT` | Port on 127.0.0.1, default 7682 |
| `HERMES_HOME` | Hermes folder, default `~/.hermes` |
| `USAGECAST_DATA` | Where measurements and the limit history go, default `data/` next to `app.py` |
| `USAGECAST_LANG` | Default language, `en` (default) or `de` |
| `USAGECAST_NTFY` | ntfy target, if not taken from Hermes |
| `USAGECAST_URL` | Address of the dashboard, tapping an alert opens it |

In the background the server measures the system prompt and tool descriptions once a day (`--snapshot`, runs in the
Hermes venv) and fetches the subscription limits every 10 minutes through Hermes' OAuth login. The token never leaves
the Hermes process. Both end up in `data/`.

`usagecast.service` is a template for a systemd user service with the repo in `~/usagecast`.
