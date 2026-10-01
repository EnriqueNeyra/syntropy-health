// Applies the saved (or system) color theme and the accent color before first paint. Inside the iPhone app's Syntropy
// tab the page runs in embedded mode: it follows the phone's appearance and the app provides the outer navigation. In
// the Mac and Windows apps it runs in the desktop shell, which draws the window around it.
(function () {
  var ua = navigator.userAgent;
  var root = document.documentElement;
  var embedded = /SyntropyHealthApp\//.test(ua);
  var saved = null, accent = null;
  try { saved = localStorage.getItem("syntropy-theme"); accent = localStorage.getItem("syntropy-accent"); } catch (e) {}
  var dark = saved && !embedded ? saved === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  root.setAttribute("data-theme", dark ? "dark" : "light");
  // The accent is chosen once for every device (it's stored on the server); this is the last one seen here.
  if (accent && accent !== "heart") root.setAttribute("data-accent", accent);
  if (embedded) root.setAttribute("data-embedded", "ios");
  // Newer iPhone apps draw native navigation around the page (tab bar, title bars, back buttons); the page then shows
  // only its content and hands navigation between sections to the app.
  if (embedded && /SyntropyNav\/\d/.test(ua)) root.setAttribute("data-native-nav", "");
  var shell = /SyntropyHealthDesktop\/[^ ]* \((Mac|Windows)\)/.exec(ua);
  if (shell) root.setAttribute("data-shell", shell[1].toLowerCase());
  // Follow the device's appearance unless one was chosen here (Settings → General).
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function (e) {
    var chosen = null;
    try { chosen = localStorage.getItem("syntropy-theme"); } catch (err) {}
    if (!embedded && chosen) return;
    var apply = function () { root.setAttribute("data-theme", e.matches ? "dark" : "light"); };
    // A cross-fade rather than a snap (see themeTransition in app.js).
    if (document.startViewTransition && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      root.setAttribute("data-vt", "theme");
      document.startViewTransition(apply).finished.then(function () { root.removeAttribute("data-vt"); }, function () {});
    } else apply();
  });
})();
