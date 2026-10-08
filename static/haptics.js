// A short vibration when a control is tapped: tabs, period and filter chips, buttons, folds, settings switches.
// iOS Safari has no Vibration API, but its <input type=checkbox switch> vibrates when it is toggled through a label
// the finger really touched (a script .click() stopped counting with iOS 26.5). So on iOS every control gets a
// transparent label on top, tied to a hidden switch: the tap hits the label, Safari vibrates, and because the label
// swallows the tap's normal action, the script clicks the control itself. Same trick as github.com/tijnjh/ios-haptics.
// Other phones use navigator.vibrate where it exists (Android). The system setting for haptics still applies.
const SEL = "nav a, a.btn, button:not(.sr), summary, .set input[type=checkbox], .set input[type=radio]"
const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)

if (!ios) {
  if (navigator.vibrate) addEventListener("click", e => { if (e.isTrusted && e.target.closest(SEL)) navigator.vibrate(8) })
} else {
  // One hidden switch outside every control: its copy of the tap must not bubble through a button (that submitted twice).
  // Hidden and out of the way, since WebKit treats a touchstart on it as handled and would cancel the scroll.
  const sw = document.createElement("input")
  sw.type = "checkbox"
  sw.setAttribute("switch", "")
  sw.id = "haptic"
  sw.tabIndex = -1
  sw.setAttribute("aria-hidden", "true")
  sw.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;margin:0;visibility:hidden"
  sw.addEventListener("click", e => e.stopPropagation())
  document.body.append(sw)
  for (const el of document.querySelectorAll(SEL)) {
    if (el.disabled) continue
    // A radio is covered with its label (the label is the visible segment). Buttons and bare switches get a wrapper and
    // the label goes next to them, not inside: a tap on a label inside a button still submits in Chromium.
    let host = el.type === "radio" ? el.closest("label") : el.matches("a, summary") ? el : null
    if (!host) {
      host = document.createElement("span")
      host.style.display = "inline-flex"
      el.before(host)
      host.append(el)
    }
    if (getComputedStyle(host).position === "static") host.style.position = "relative"
    const tap = document.createElement("label")
    tap.htmlFor = "haptic"
    tap.setAttribute("aria-hidden", "true")
    tap.style.cssText = "position:absolute;inset:0"
    tap.addEventListener("click", e => { e.stopPropagation(); el.click() })
    host.append(tap)
  }
}
