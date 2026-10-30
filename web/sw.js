/* MediaHub Service Worker —— 只做「离线壳」缓存，接口永远走网络 */
const CACHE = "mh-shell-v6";
const SHELL = [
  "/",
  "/static/style.css",
  "/static/app.js",
  "/static/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(SHELL).catch(() => {}))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;      // 海报等外链不拦
  if (url.pathname.startsWith("/api/")) return;    // 接口永不缓存

  // 网络优先：拿到新内容就顺手更新缓存，断网时回退缓存
  e.respondWith(
    fetch(req)
      .then((resp) => {
        if (resp && resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
        }
        return resp;
      })
      .catch(() => caches.match(req).then((hit) => hit || caches.match("/")))
  );
});
