// Service worker: permite instalar o site e abrir sem internet.
// A página e os dados vêm sempre da internet quando há conexão (as mudanças aparecem sozinhas).
// Os mapas ficam guardados no aparelho; cada mapa tem uma "versão" no endereço (?v=...),
// então um mapa gerado de novo é baixado de novo automaticamente.
importScripts("versao.js");
var BASE = "base-" + self.VERSAO_SITE;
var MAPAS = self.CACHE_MAPAS;

var ARQUIVOS_BASE = [
  "./",
  "index.html",
  "versao.js",
  "dados.js",
  "manifest.webmanifest",
  "logo_belem_limpa.png",
  "logo_prefeitura.png",
  "icones/icone-192.png",
  "icones/favicon-64.png",
  "vendor/jspdf.umd.min.js",
  "vendor/jspdf.plugin.autotable.min.js",
  "vendor/leaflet/leaflet.js",
  "vendor/leaflet/leaflet.css"
];

self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(BASE).then(function (c) { return c.addAll(ARQUIVOS_BASE); }));
  self.skipWaiting();
});

self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (nomes) {
    return Promise.all(nomes.filter(function (n) { return n !== BASE && n !== MAPAS; })
      .map(function (n) { return caches.delete(n); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  if (url.origin !== location.origin) return;

  // Mapas (JPG): usa o que já está guardado; senão baixa e guarda.
  if (url.pathname.indexOf("/mapas/") >= 0) {
    e.respondWith(caches.open(MAPAS).then(function (c) {
      return c.match(req).then(function (r) {
        return r || fetch(req).then(function (resp) {
          if (resp.ok) c.put(req, resp.clone());
          return resp;
        });
      });
    }));
    return;
  }

  // Página e dados: tenta a internet primeiro (pega atualizações); sem internet, usa a cópia guardada.
  e.respondWith(fetch(req).then(function (resp) {
    if (resp.ok) { var copia = resp.clone(); caches.open(BASE).then(function (c) { c.put(req, copia); }); }
    return resp;
  }).catch(function () {
    return caches.match(req, { ignoreSearch: true }).then(function (r) { return r || caches.match("index.html"); });
  }));
});
