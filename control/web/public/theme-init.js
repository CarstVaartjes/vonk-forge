// Blocking pre-paint theme: applies the owner's stored pick before first paint so a light-mode
// owner never sees the dark default flash. Served as a same-origin file, so it works under any CSP.
// Mirrors src/lib/theme.ts (the storage key and values); with no pick the stylesheet follows the system.
// theme-init.test.ts writes through setTheme, so a renamed key fails the test.
try {
  var pick = localStorage.getItem("vonk-theme");
  if (pick === "light" || pick === "dark") document.documentElement.dataset.theme = pick;
} catch (_) {}
