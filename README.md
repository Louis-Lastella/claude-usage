# Usagecast

[![test](https://github.com/Louis-Lastella/usagecast/actions/workflows/test.yml/badge.svg)](https://github.com/Louis-Lastella/usagecast/actions/workflows/test.yml)

**English** · [Deutsch](README.de.md)

A self-hosted dashboard that shows what eats your AI usage. Right now it reads
[Hermes Agent](https://github.com/NousResearch/hermes-agent) with Anthropic Claude and breaks the cost down into every
tool, skill, plugin, system prompt part, thinking, background review, cache rebuilds after pauses and cache breaks. On
top of that it shows your subscription limits (5 hours, week, extra credits) with a forecast, sends alerts to your phone
and gives concrete tips from your own data, with the `hermes` command that applies them.

Pure Python standard library. It reads `~/.hermes/state.db` and `~/.hermes/logs/agent.log*` read-only. No AI involved:
ranking, breakdown, forecast and tips are fixed calculation rules, and no request ever goes to a model.

It works with any Hermes installation. It reads the active plugins and memory provider from `config.yaml`
(`plugins.enabled`, `memory.provider`) and assigns their parts of the system prompt and of your messages accordingly.
It also detects SOUL.md, Hermes' own notes and where a session came from (Telegram, Discord, Slack, cron jobs ...).

![Overview: weekly limit with pace and daily budget, 5-hour window, key figures](docs/overview.png)

Try it without Hermes: `python3 app.py --demo` starts it on a month of made-up data.

## Pages

| Page | Content |
|---|---|
| `/` | Limits with pace, daily budget and forecast, key figures with the change against last week, what eats the most, origins, models, tips |
| `/history` | Cost per day by the biggest items with markers for Hermes updates and config changes, this week laid over the three before, heatmap by weekday and hour, activity calendar, day-by-day table |
| `/details` | Every tool, skill, plugin, system prompt part and tool description on its own, cache breaks with their likely cause, the ten most expensive single tool results |
| `/sessions` | Most expensive sessions with origin filter and search, cron tab with cost per run and per week. A session page shows every step and the `hermes --resume <id>` command |
| `/projects` | Cost per project, tap one to see its sessions |
| `/settings` | Appearance, limits display, alerts and push setup |
| `/api/summary` | Limits, forecast and this limit week's cost as JSON, for widgets |

Period with `?p=w` (since the last weekly limit reset, default), `?p=1`, `?p=7`, `?p=30` or a custom range like
`?p=2026-10-01..2026-10-07` (the "Custom" button).

![History: cost per day by the biggest items and a heatmap by weekday and hour](docs/history.png)

Light and dark mode follow the system setting. On an iPhone, open it in Safari and choose "Add to Home Screen" to start
it like an app.

<img src="docs/phone-dark.png" width="260" alt="Overview on a phone in dark mode, with the tab bar at the bottom">

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

**Forecast:** The 5-hour window is extrapolated from the pace of the last hour. For the week Usagecast backtests two
methods on your last complete limit weeks: a straight line from the pace since the reset, and your weekly rhythm (which
share of a usual week's cost has passed by this hour). The rhythm is used when it is at least 10 % more accurate; the
fold under the weekly limit names the method and both errors (result in `data/forecast.json`).

**Pace and budget:** A mark on each limit bar shows how much of the window has passed. Ahead of it means "Too fast"
with the time the limit will be full, behind it "On track" with the expected value at the reset. The daily budget is
what is left of the week divided by the days until the reset. When Anthropic's status page reports an incident, a line
under the limits says so.

**Limit split:** From the limit readings every 10 minutes and the cost per hour, Usagecast estimates how much API value
one percent of your week is. With that it splits the week into Hermes, Claude Code (sessions in `~/.claude/projects` on
the same machine or reported by other machines, see below) and the rest (claude.ai, the apps). Costs can be shown as a share of the week instead
of money.

**Prices:** A model missing from the price list is priced like Claude Opus and marked "estimated price".

## Settings

`/settings` sets the appearance (light, dark or system, language, USD or EUR at the ECB daily rate, compact or full
numbers, default period), the limits display (used or left, costs as money or as % of the week) and every alert on its
own with its threshold and quiet hours. The values go to `data/settings.json`, readable only by its owner; environment
variables only set the defaults. The form saves only when it was sent from Usagecast's own page. If Hermes has profiles (`hermes profile create`), the settings also choose which one is analysed; the limits stay
the same, they belong to the account.

## Alerts via ntfy

Every 10 minutes the server checks the limits and sends at most one push message per window when

- the 5-hour window will be full soon at the pace of the last hour (30 minutes by default),
- the weekly forecast is above 100 % (at the earliest one day after the reset),
- the week passes 80 % and 90 %,
- the 5-hour window is free again after it was full,
- extra credits start being used, and when the month's credits pass one of your steps (e.g. 10 and 20, off by default),
- a chat has become expensive: its last three replies cost at least 0.5 % of the week each (a new chat starts far
  lower; 0.5 % is what the most expensive 5 % of replies cost on the author's setup),
- a single run is far off: a cron run or one reply costs three times its usual amount and at least 3 % of the week.

The last two need a few days of limit readings first (they work in % of the week). Tapping them opens the session.

**Budget guard** (off by default): on `/settings` you mark recurring cron jobs that may wait. While the week gets tight
(forecast above 100 % from the second day, the week at 90 % or the 5-hour window at 85 %) Usagecast pauses them with
`hermes cron pause` and resumes them once there is room again, each time with a quiet push. Jobs you paused yourself
are never touched; the overview says which jobs are held back.

On Sunday evening a quiet digest follows: where the week stands, the biggest item and the change against last week.

**Setup in a minute:** On `/settings`, "Set up push" creates your own random topic on [ntfy.sh](https://ntfy.sh). On
Android one button opens the ntfy app with the subscription; on an iPhone you copy the topic and follow the three steps
shown there. "Send test" shows the server's reply right away. Your own ntfy server with an access token works too, and
so does Hermes' ntfy channel (`NTFY_HOME_CHANNEL`, `NTFY_SERVER_URL`, `NTFY_TOKEN` in `~/.hermes/.env`).
`USAGECAST_NTFY` (full URL with topic) sets a default without the settings page.

## Widgets (iPhone home screen and Lock Screen)

`widget/usagecast-widget.js` is a [Scriptable](https://scriptable.app) script that shows the weekly limit and the 5-hour
window. Copy it into a new script in Scriptable, add a small Scriptable widget, pick the script and put your dashboard
address into "Parameter". It reads `/api/summary`, so the phone has to reach the dashboard (for example over Tailscale).

The same script works on the Lock Screen (iOS 16 or later): long-press the Lock Screen, tap "Customize", pick the Lock
Screen, tap the widget area (or the line above the clock), add Scriptable, then tap the new widget, choose the script and
enter the address as "Parameter". Circular shows the week as a ring, rectangular the week with its reset, a thin bar and
the 5-hour window, the line above the clock "Week 65% · 5h 6%". iOS tints them to match the clock.

## Claude Code on other machines

Claude Code on a laptop uses the same limits but writes its transcripts there. `tools/cc-report.py` (standard library,
Python 3.9 or later) sums its token counts per hour and model and sends only those numbers to the dashboard every 10
minutes; the limit split then counts them as Claude Code. On `/settings` under "Other machines": create a token, then
run the command shown there on the other machine, for example

```sh
curl -fsSO https://dashboard.example:8443/cc-report.py
python3 cc-report.py --url https://dashboard.example:8443 --token TOKEN --install
```

On a Mac this loads a LaunchAgent (`dev.usagecast.report`, every 10 minutes); on Linux it prints a crontab line.
`--dry-run` shows what would be sent. Settings live in `~/.config/usagecast/report.json` (mode 600). The endpoint is
`POST /api/ingest` with `Authorization: Bearer <token>`; a new token on `/settings` locks out the old one.

## Running it

```bash
python3 app.py --test        # self-test
python3 app.py --ntfy-test   # send a test alert to ntfy
python3 app.py --demo        # made-up data, reads nothing from Hermes, sends no alerts
PORT=7681 python3 app.py     # server on 127.0.0.1:7681
```

| Variable | Meaning |
|---|---|
| `PORT` | Port on 127.0.0.1, default 7682 |
| `HERMES_HOME` | Hermes folder, default `~/.hermes` |
| `USAGECAST_DATA` | Where measurements and the limit history go, default `data/` next to `app.py` |
| `USAGECAST_LANG` | Default language, `en` (default) or `de` |
| `USAGECAST_NTFY` | ntfy target, if not taken from Hermes |
| `USAGECAST_URL` | Default for the dashboard address in the settings, tapping an alert opens it |

In the background the server measures the system prompt and tool descriptions once a day (`--snapshot`, runs in the
Hermes venv) and fetches the subscription limits every 10 minutes through Hermes' OAuth login. The token never leaves
the Hermes process. Both end up in `data/`.

`usagecast.service` is a template for a systemd user service with the repo in `~/usagecast`.

## License

MIT, see [LICENSE](LICENSE).
