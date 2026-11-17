// @ts-check
// Light, dark, or whatever the system uses. Loaded in <head> without `defer` so a saved choice applies
// before the first paint and the page never flashes the wrong theme; the buttons are wired after load.
// Colors themselves live in styles.css: this file only sets <html data-theme>.
//
// It also marks <html> as running scripts, early, so styles.css shows the theme buttons and keeps room
// for the toolbar from the first paint. Without scripts those controls would do nothing, so they stay hidden.
//
// And it marks <html> as seen once the page is on screen: styles.css holds the masthead's sun still until
// then, so a page opened in a background tab sets its sun when the reader arrives, not before.
"use strict";

{
  const STORAGE_KEY = "register-theme";
  const SYSTEM_THEME = "system";
  const THEMES = [SYSTEM_THEME, "light", "dark"];
  const SCRIPTS_CLASS = "has-scripts";
  const SEEN_CLASS = "is-seen";

  /** @returns {string} the saved theme, or "system" when nothing valid is saved or storage is blocked */
  function readSavedTheme() {
    try {
      const saved = localStorage.getItem(STORAGE_KEY) ?? SYSTEM_THEME;
      return THEMES.includes(saved) ? saved : SYSTEM_THEME;
    } catch {
      return SYSTEM_THEME;
    }
  }

  /** @param {string} theme */
  function saveTheme(theme) {
    try {
      if (theme === SYSTEM_THEME) localStorage.removeItem(STORAGE_KEY);
      else localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // Storage is blocked (private windows, strict settings): the choice lasts until the page reloads.
    }
  }

  /** @param {string} theme */
  function applyTheme(theme) {
    if (theme === SYSTEM_THEME) delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
  }

  /** @param {HTMLElement} control @param {string} theme */
  function markPressed(control, theme) {
    for (const button of control.querySelectorAll("button[data-theme-value]")) {
      if (!(button instanceof HTMLButtonElement)) continue;
      button.setAttribute("aria-pressed", String(button.dataset.themeValue === theme));
    }
  }

  function bindThemeControl() {
    const control = document.getElementById("theme-control");
    if (!control) return;
    markPressed(control, readSavedTheme());
    control.addEventListener("click", (event) => {
      const button = event.target instanceof Element ? event.target.closest("button[data-theme-value]") : null;
      if (!(button instanceof HTMLButtonElement)) return;
      const theme = button.dataset.themeValue ?? SYSTEM_THEME;
      applyTheme(theme);
      saveTheme(theme);
      markPressed(control, theme);
    });
  }

  /** Marks the page as seen the first time it is on screen, then stops listening. */
  function markSeen() {
    if (document.visibilityState !== "visible") return;
    document.documentElement.classList.add(SEEN_CLASS);
    document.removeEventListener("visibilitychange", markSeen);
  }

  document.documentElement.classList.add(SCRIPTS_CLASS);
  document.addEventListener("visibilitychange", markSeen);
  markSeen();
  applyTheme(readSavedTheme());
  document.addEventListener("DOMContentLoaded", bindThemeControl);
}
