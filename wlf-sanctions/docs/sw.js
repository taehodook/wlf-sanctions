/* WLF 제재명단 통합 조회 — 서비스워커
 *
 * 전략이 자산별로 다릅니다. 제재명단은 낡은 값을 보여주는 것이 사고이므로,
 * 데이터(data/**)는 반드시 네트워크를 먼저 시도하고 실패했을 때만 캐시를 씁니다.
 * 화면 껍데기(HTML/아이콘)는 캐시를 먼저 써서 실행을 빠르게 합니다.
 *
 * 배포 경로가 /{계정}/{저장소}/ 하위이므로 모든 경로는 상대경로로 다룹니다.
 */
const VERSION = "wlf-v1";
const SHELL = VERSION + "-shell";
const DATA = VERSION + "-data";

const SHELL_ASSETS = ["./", "./index.html", "./manifest.json", "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(SHELL)
      // 일부 자산이 실패해도 설치는 진행합니다(전체 실패 방지).
      .then((c) => Promise.allSettled(SHELL_ASSETS.map((a) => c.add(a))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== SHELL && k !== DATA).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;   // CDN은 브라우저에 맡깁니다.

  /* 명단 데이터: 네트워크 우선 → 실패 시 캐시(오프라인 열람용) */
  if (url.pathname.includes("/data/")) {
    e.respondWith(
      fetch(req)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(DATA).then((c) => c.put(req, copy));
          }
          return res;
        })
        .catch(() => caches.match(req))
    );
    return;
  }

  /* 그 외(껍데기): 캐시 우선 → 없으면 네트워크 */
  e.respondWith(
    caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      if (res && res.ok && res.type === "basic") {
        const copy = res.clone();
        caches.open(SHELL).then((c) => c.put(req, copy));
      }
      return res;
    }).catch(() => caches.match("./index.html")))
  );
});
