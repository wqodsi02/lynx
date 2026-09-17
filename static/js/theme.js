// Gestione tema chiaro/scuro. Applicato su <html data-theme="..."> e persistito
// in localStorage. Lo script inline in <head> (vedi index.html) applica il
// tema salvato PRIMA del paint per evitare il flash bianco/nero all'avvio.
const THEME_KEY = "lynx-theme";

export function getTheme() {
  return localStorage.getItem(THEME_KEY) || "dark";
}

export function setTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem(THEME_KEY, theme);
}

export function toggleTheme() {
  const next = getTheme() === "dark" ? "light" : "dark";
  setTheme(next);
  return next;
}

export function initThemeToggle(switchEl) {
  setTheme(getTheme());
  switchEl.addEventListener("click", () => toggleTheme());
}
