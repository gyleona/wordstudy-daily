// 单词研究所 Service Worker —— 离线缓存，让"添加到主屏幕"成为真·独立 APP
const CACHE = 'wstudy-v9';
const SHELL = [
  './index.html',
  './manifest.json',
  './sw.js',
  './icon-192.png',
  './icon-512.png',
  './apple-touch-icon.png',
  './logo-word2.svg',
  './words-data.json'
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // 每日词表：先联网，失败回退缓存（离线也能看已缓存内容）
  if (url.pathname.endsWith('words-data.json')) {
    const key = new Request('./words-data.json');
    e.respondWith(
      fetch(req, { cache: 'no-store' }).then(res => {
        caches.open(CACHE).then(c => c.put(key, res.clone())).catch(() => {});
        return res;
      }).catch(() => caches.match(key).then(c => c || Response.error()))
    );
    return;
  }

  // 页面导航：先联网，失败回退缓存的 index.html
  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req).then(res => {
        caches.open(CACHE).then(c => c.put('./index.html', res.clone())).catch(() => {});
        return res;
      }).catch(() => caches.match('./index.html'))
    );
    return;
  }

  // 其它静态资源：缓存优先，未命中再联网并补缓存
  e.respondWith(
    caches.match(req).then(hit => {
      if (hit) return hit;
      return fetch(req).then(res => {
        if (res && res.ok && (res.type === 'basic' || res.type === 'cors')) {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(req, copy)).catch(() => {});
        }
        return res;
      }).catch(() => caches.match('./index.html'));
    })
  );
});
