// Instalación como app (PWA)
let deferred;
const btn = document.getElementById("install");
window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); deferred = e; btn.hidden = false; });
btn && btn.addEventListener("click", async () => {
  if (deferred) { deferred.prompt(); await deferred.userChoice; deferred = null; btn.hidden = true; }
  else document.getElementById("ioshint").hidden = false;
});
const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone;
if (isIOS && !standalone && btn) btn.hidden = false;
if ("serviceWorker" in navigator) {
  const root = document.querySelector('link[rel="manifest"]').getAttribute("href").replace("manifest.webmanifest", "");
  navigator.serviceWorker.register(root + "sw.js");
}
