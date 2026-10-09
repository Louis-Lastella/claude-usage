// Usagecast widget for Scriptable (iOS): weekly limit and 5-hour window on the home screen or the Lock Screen.
// Setup: copy this file into Scriptable, add a Scriptable widget (home screen: small; Lock Screen: circular,
// rectangular or the line above the clock), pick this script and put your dashboard address into "Parameter",
// for example https://my-server.example:8443
// Or write it into ADDRESS below; that one is also used when you run the script inside the app.
const ADDRESS = "";
const base = (args.widgetParameter || ADDRESS).trim().replace(/\/+$/, "");
const family = config.widgetFamily || "small";
const lock = family.startsWith("accessory");
const col = (light, dark) => Color.dynamic(new Color(light), new Color(dark));
const C = {
  bg: col("#ffffff", "#1c1b19"), ink: col("#1c1a18", "#f3f1ee"), mute: col("#67615b", "#a8a29b"),
  accent: col("#ec7a1c", "#f28a3a"), soft: col("#efedea", "#292724"), fill: col("#8f8982", "#7a746e"),
};
// Lock Screen widgets are tinted by the system: plain white at different strengths
const L = { ink: new Color("#ffffff", 1), mute: new Color("#ffffff", 0.6), soft: new Color("#ffffff", 0.25) };

function text(w, s, color, font) {
  const t = w.addText(s);
  t.font = font; t.textColor = color; t.lineLimit = 1; t.minimumScaleFactor = 0.7;
  return t;
}

function bar(w, used, width, hot = used >= 80, h = 6, track_ = C.soft, fill_ = hot ? C.accent : C.fill) {
  const track = w.addStack();
  track.size = new Size(width, h); track.backgroundColor = track_; track.cornerRadius = h / 2;
  const fill = track.addStack();
  fill.size = new Size(Math.max(h, (width * Math.min(used, 100)) / 100), h);
  fill.backgroundColor = fill_; fill.cornerRadius = h / 2;   // orange only when hot (DESIGN.md)
  track.addSpacer();
}

function ring(used) {
  // A ring with the week's percentage inside, drawn as an image (Scriptable has no ring view)
  const n = 58, r = 24, lw = 5, ctx = new DrawContext();
  ctx.size = new Size(n, n); ctx.opaque = false; ctx.respectScreenScale = true;
  const arc = (from, to, color) => {
    const pts = [];
    for (let a = from; a <= to + 1e-9; a += (to - from) / 60 || 1)
      pts.push(new Point(n / 2 + r * Math.sin(a), n / 2 - r * Math.cos(a)));
    const p = new Path(); p.addLines(pts);
    ctx.addPath(p); ctx.setStrokeColor(color); ctx.setLineWidth(lw); ctx.strokePath();
  };
  arc(0, 2 * Math.PI, L.soft);
  if (used > 0) arc(0, (2 * Math.PI * Math.min(used, 100)) / 100, L.ink);
  ctx.setFont(Font.semiboldSystemFont(15)); ctx.setTextColor(L.ink); ctx.setTextAlignedCenter();
  ctx.drawTextInRect(Math.round(used) + "%", new Rect(0, n / 2 - 10, n, 20));
  return ctx.getImage();
}

const hm = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const day = (t) => new Date(t * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });

const w = new ListWidget();
w.url = base;
if (!lock) { w.backgroundColor = C.bg; w.setPadding(14, 14, 14, 14); }
try {
  if (!base) throw new Error(lock ? "No address" : "No address. Put the dashboard URL into the widget's Parameter.");
  const req = new Request(base + "/api/summary");
  const raw = await req.loadString();
  const code = req.response && req.response.statusCode;
  if (code !== 200) throw new Error(lock ? "HTTP " + code : "HTTP " + code + ": not the dashboard. Check address and port.");
  const s = JSON.parse(raw);
  const week = s.limits.seven_day, five = s.limits.five_hour;
  if (family === "accessoryInline") {
    text(w, "Week " + Math.round(week.used) + "%" + (five ? " · 5h " + Math.round(five.used) + "%" : ""), L.ink, Font.systemFont(12));
  } else if (family === "accessoryCircular") {
    w.addImage(ring(week.used)).centerAlignImage();
  } else if (family === "accessoryRectangular") {
    text(w, "Week " + Math.round(week.used) + "%" + (week.resets_at ? " · " + day(week.resets_at) : ""), L.ink, Font.semiboldSystemFont(13));
    w.addSpacer(3);
    bar(w, week.used, 130, false, 4, L.soft, L.ink);
    w.addSpacer(4);
    if (five) text(w, "5h " + Math.round(five.used) + "%" + (five.resets_at ? " · " + hm(five.resets_at) : ""), L.mute, Font.systemFont(12));
  } else {
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
  }
} catch (e) {
  if (lock) {
    text(w, "Usagecast: " + String(e.message || e), L.ink, Font.systemFont(11)).lineLimit = 2;   // short, there is no room
  } else {
    text(w, "Usagecast", C.ink, Font.semiboldSystemFont(13));
    text(w, "Not reachable: " + (base || "-"), C.mute, Font.systemFont(10)).lineLimit = 2;
    w.addSpacer(4);
    text(w, String(e.message || e), C.ink, Font.systemFont(10)).lineLimit = 4;   // the real reason
  }
}
w.refreshAfterDate = new Date(Date.now() + 15 * 60 * 1000);
if (config.runsInWidget) Script.setWidget(w);
else await w.presentSmall();
Script.complete();
