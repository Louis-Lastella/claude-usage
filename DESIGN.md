# Usagecast design

Operate surface: a calm dashboard you glance at several times a day, on a phone (home-screen app) and a laptop.
Light or dark follows the system; both are first-class.

## Look
- Warm stone neutrals, not cream paper and not cool grey. One clean sans (the platform UI face: SF Pro on Apple,
  Segoe UI Variable on Windows) for everything; numbers use tabular figures. No serif, no display font.
- Orange is the only accent and means "look here": the hot limit meter, the main KPI number, the active nav icon,
  chart series 0 and the heatmap ramp. Ordinary bars and meters are neutral (`--fill`, `--soft`); comparison lines
  (earlier weeks) use `--fill`, since `--soft` is too faint for a line.
- Depth is a 1 px ring (`--line`) plus a soft 1-2 px shadow (`--shc`); dark mode leans on the ring.
- Radii: cards 14 px, segmented controls 10/7 px, buttons and inputs 8-9 px, filter chips are pills.

## Tokens (static/style.css, all via `light-dark()`)
| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` / `--side` / `--card` | #f7f6f4 / #f1f0ed / #fff | #141312 / #181716 / #1c1b19 | page, sidebar layer, cards |
| `--ink` / `--ink2` | #1c1a18 / #3d3935 | #f3f1ee / #d8d3cd | text, strong secondary |
| `--mute` / `--faint` | #67615b / #6f6962 | #a8a29b / #8f8982 | secondary, meta (both AA on every surface) |
| `--acc` / `--acc-ink` | #ec7a1c / #ad540c | #f28a3a / #f5a05c | fills / accent text and focus ring |
| `--line` / `--line2` | #e8e6e2 / #d9d5d0 | #2a2825 / #3a3733 | hairlines / control borders |
| `--track` / `--fill` / `--soft` | #efedea / #8f8982 / #cfcac4 | #292724 / #7a746e / #4d4945 | meter track / neutral fill / quiet fill |

## Type scale
h1 28/600 (-0.025em) · section h2 18/600 · card h2 15/600 · body 15 · hint 13.5 · meta 12-13 ·
limit number 80/600 (-0.04em) · KPI 26/600. Labels are sentence case, never uppercase-tracked eyebrows.

## Components
- Segmented control (`.seg`, `.opts`): tinted track, the selected item is a raised surface, not an ink pill.
- Longer explanations fold away: `details.bar` (ranked bars, the bar is the summary) and `details.more`
  (limit card). The verdict and the daily budget stay visible.
- Savings tips are one list card, not a card grid. KPIs are one strip with hairline dividers.
- On/off settings are switches (`.set input[type=checkbox]`), 38x22 px, 46x28 px on phones.
- Every fold (`details.more`, `details.bar`, `details.adv`) shows the same small chevron after its label, never the
  native triangle.
- A command to copy is a `pre.cmd` block (tinted, mono, wraps, a tap selects all); short commands in text stay `code`.
- Lists of things with a value (cron jobs, reporting machines) are `.jobs` rows: name left, value right, no bullets.
- Phone (<= 860 px): the sidebar becomes a bottom tab bar with labels; every control is >= 40-44 px tall.
- Motion: only 150-200 ms state feedback (switches, folds); nothing animates on load and zoom is locked on phones.
  `prefers-reduced-motion` turns it all off.
- Haptics: a tapped control (tab, chip, button, fold, switch) gives a short vibration, `static/haptics.js`. iOS has no
  Vibration API, so a transparent label tied to a hidden `<input type=checkbox switch>` covers each control; Android
  uses `navigator.vibrate`.
