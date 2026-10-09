# Changelog

Versions follow [Semantic Versioning](https://semver.org).

## 3.0.0 - 2026-10-09

- Alerts when a chat has become expensive (its last three replies above a share of the week, with what a new chat
  costs) and when a cron run or a single reply costs far more than usual.
- Extra credits per month: every limits reading stores them, earlier months fold out under the limits, an estimate of
  what the week would cost in credits when the forecast is above 100 %, alerts at chosen credit steps.
- Budget guard: pauses the cron jobs you mark while the week gets tight and resumes them afterwards.
- Week forecast by weekly rhythm, used when a backtest over the last weeks finds it more accurate than a straight line.
- Lock Screen widgets: circular, rectangular and inline.
- Claude Code from other machines: `POST /api/ingest` and `tools/cc-report.py` (Mac LaunchAgent or Linux crontab).
- MIT license, CI on GitHub Actions, the version in the page footer.

## 2.0.0 - 2026-10-09

- Renamed to Usagecast, English first with a German locale.
- Settings page: push setup with ntfy, euro amounts, theme, costs as money or % of the week, the analysed Hermes
  profile.
- Limits: pace verdicts, daily budget, who used the weekly limit (Hermes, Claude Code, other), status page incidents.
- Alerts: 5-hour window free again, weekly limit at 80 and 90 %, Sunday digest.
- History: activity calendar, custom date range, this week over the three before, Hermes updates and config changes
  marked with a before/after cost per step.
- Details: a likely cause per cache break, the most expensive single tool results.
- `/api/summary` and a Scriptable widget for the iOS home screen.
- New look (stone neutrals, one sans, foldable explanations, bottom tab bar on phones), haptics, `--demo` mode.

## 1.0.0 - 2026-10-08

- First version: a dashboard of what eats the Claude usage in Hermes, with sessions, projects, the subscription limits
  with a forecast and ntfy alerts.
