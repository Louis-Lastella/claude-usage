// Usagecast widget for Scriptable (iOS): weekly limit and 5-hour window on the home screen.
// Setup: copy this file into Scriptable, add a small Scriptable widget, pick this script and put your
// dashboard address into "Parameter", for example https://my-server.example:8443
const base = (args.widgetParameter || "http://127.0.0.1:7681").replace(/\/$/, "");
const col = (light, dark) => Color.dynamic(new Color(light), new Color(dark));
const C = {
  bg: col("#f6f1e8", "#1b1713"), ink: col("#2b241d", "#efe6da"), mute: col("#8a7f73", "#7e7368"),
  accent: col("#d9822b", "#e8954a"), soft: col("#e8dfd2", "#352d26"),
};

function text(w, s, color, font) {
  const t = w.addText(s);
  t.font = font; t.textColor = color; t.lineLimit = 1; t.minimumScaleFactor = 0.7;
  return t;
}

function bar(w, used, width) {
  const track = w.addStack();
  track.size = new Size(width, 6); track.backgroundColor = C.soft; track.cornerRadius = 3;
  const fill = track.addStack();
  fill.size = new Size(Math.max(6, (width * Math.min(used, 100)) / 100), 6);
  fill.backgroundColor = C.accent; fill.cornerRadius = 3;
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
  text(w, Math.round(week.used) + "%", C.ink, new Font("Georgia-Bold", 30));
  bar(w, week.used, 120);
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
