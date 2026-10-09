// Usagecast widget for Scriptable (iOS): weekly limit and 5-hour window on the home screen.
// Setup: copy this file into Scriptable, add a small Scriptable widget, pick this script and put your
// dashboard address into "Parameter", for example https://my-server.example:8443
const base = (args.widgetParameter || "http://127.0.0.1:7681").replace(/\/$/, "");
const col = (light, dark) => Color.dynamic(new Color(light), new Color(dark));
const C = {
  bg: col("#ffffff", "#1c1b19"), ink: col("#1c1a18", "#f3f1ee"), mute: col("#67615b", "#a8a29b"),
  accent: col("#ec7a1c", "#f28a3a"), soft: col("#efedea", "#292724"), fill: col("#8f8982", "#7a746e"),
};

function text(w, s, color, font) {
  const t = w.addText(s);
  t.font = font; t.textColor = color; t.lineLimit = 1; t.minimumScaleFactor = 0.7;
  return t;
}

function bar(w, used, width, hot = used >= 80) {
  const track = w.addStack();
  track.size = new Size(width, 6); track.backgroundColor = C.soft; track.cornerRadius = 3;
  const fill = track.addStack();
  fill.size = new Size(Math.max(6, (width * Math.min(used, 100)) / 100), 6);
  fill.backgroundColor = hot ? C.accent : C.fill; fill.cornerRadius = 3;   // orange only when hot (DESIGN.md)
  track.addSpacer();
}

const hm = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

const w = new ListWidget();
w.backgroundColor = C.bg;
w.setPadding(14, 14, 14, 14);
w.url = base;
try {
  const s = await new Request(base + "/api/summary").loadJSON();
  const week = s.limits.seven_day, five = s.limits.five_hour;
  text(w, "Weekly limit", C.mute, Font.mediumSystemFont(11));
  text(w, Math.round(week.used) + "%", C.ink, Font.semiboldSystemFont(30));
  bar(w, week.used, 120, week.used >= 80 || week.forecast > 100);   // week won't last: hot
  w.addSpacer(4);
  if (week.forecast != null) text(w, "~" + Math.round(week.forecast) + "% at reset", C.mute, Font.systemFont(10));
  w.addSpacer();
  if (five) {
    text(w, "5h " + Math.round(five.used) + "% · reset " + hm(five.resets_at), five.used >= 80 ? C.accent : C.ink,
      Font.mediumSystemFont(11));
    w.addSpacer(3);
    bar(w, five.used, 120);
  }
} catch (e) {
  text(w, "Usagecast", C.ink, Font.semiboldSystemFont(13));
  text(w, "Not reachable: " + base, C.mute, Font.systemFont(10));
}
w.refreshAfterDate = new Date(Date.now() + 15 * 60 * 1000);
if (config.runsInWidget) Script.setWidget(w);
else await w.presentSmall();
Script.complete();
