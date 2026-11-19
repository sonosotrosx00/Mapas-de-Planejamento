"""Reconstrói os mapas de Coleta Domiciliar e Mutirão a partir de:
  - site/dados.js (extraído dos PDFs)
  - Mapa de Localização das Novas Áreas.kml (polígonos oficiais das 10 Áreas)
  - ruas.js do https://mapas.cblplanejamento.com.br/ (malha viária de Belém)

Gera arquivos limpos e prontos para Google My Maps, Google Earth, QGIS e para
importação direta em https://mapas.cblplanejamento.com.br/ e https://mapadeplanejamento.vercel.app/:
  1. mymaps/coleta_por_area/      -> 1 KMZ por Área (fundo da Área + camadas por Frequência/Turno)
  2. mymaps/mutirao_por_area/     -> 1 KMZ por Área (fundo da Área + 1 camada por Equipe)
  3. mymaps/por_setor/coleta/     -> 1 KMZ individual por Setor (fundo da Área + setor em vermelho)
  4. mymaps/por_setor/mutirao/    -> 1 KMZ individual por Setor, organizado por Área
  5. mymaps/geojson_para_sites/   -> GeoJSONs leves (coleta, mutirão e novas áreas)

Uso:
    python gerar_mymaps.py
"""
import csv
import heapq
import json
import math
import os
import re
import shutil
import statistics
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.abspath(__file__))
DADOS_JS = os.path.join(ROOT, "site", "dados.js")
AREAS_KML = os.path.join(ROOT, "Mapa de Localização das Novas Áreas.kml")
CACHE_DIR = os.path.join(ROOT, "cache_cbl")
OUT_DIR = os.path.join(ROOT, "mymaps")

URL_RUAS = "https://mapas.cblplanejamento.com.br/data/ruas.js?v=1118e"

# ---------------------------------------------------------------------------
# 1. Carregamento de dados (dados.js, ruas.js, Novas Áreas.kml)
# ---------------------------------------------------------------------------

def fetch_or_cache(url, fname):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, fname)
    if not os.path.exists(path):
        print(f"Baixando {url} ...")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
    with open(path, "r", encoding="utf-8") as f:
        txt = f.read()
    return json.loads(txt[txt.find("{"):].rstrip(";\n\r "))


def load_dados_js():
    with open(DADOS_JS, "r", encoding="utf-8") as f:
        txt = f.read()
    return json.loads(txt[txt.find("{"):].rstrip(";\n\r "))


def load_areas_kml():
    """Lê os 10 polígonos oficiais de Mapa de Localização das Novas Áreas.kml.
    Retorna dict {int(area): {"name": "AREA_01", "rings": [[(lat, lng), ...], ...]}}.
    """
    root = ET.parse(AREAS_KML).getroot()
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    areas = {}
    for pm in root.findall(".//k:Placemark", ns):
        name_el = pm.find("k:name", ns)
        name = name_el.text.strip() if name_el is not None and name_el.text else ""
        area_num = None
        for d in pm.findall(".//k:Data", ns):
            if d.attrib.get("name") == "Areas_Nova":
                v = d.find("k:value", ns)
                if v is not None and v.text and v.text.strip().isdigit():
                    area_num = int(v.text.strip())
        if area_num is None:
            m = re.search(r"(\d+)", name)
            if m:
                area_num = int(m.group(1))
        if area_num is None:
            continue
        rings = []
        for coord_el in pm.findall(".//k:outerBoundaryIs//k:coordinates", ns):
            if not coord_el.text:
                continue
            ring = []
            for tok in coord_el.text.strip().split():
                parts = tok.split(",")
                if len(parts) >= 2:
                    lng, lat = float(parts[0]), float(parts[1])
                    ring.append((lat, lng))
            if len(ring) >= 3:
                rings.append(ring)
        areas[area_num] = {"name": name or f"AREA_{area_num:02d}", "rings": rings}
    return areas


# ---------------------------------------------------------------------------
# 2. Motor de localização (baseado em localizador.js + desambiguação por setor)
# ---------------------------------------------------------------------------

PREF_RE = re.compile(
    r"^(RUA|R|AVENIDA|AV|TRAVESSA|TV|TRAV|TR|PASSAGEM|PSG|PAS|PASS|PS|ALAMEDA|AL|VILA|VL|"
    r"ESTRADA|EST|RODOVIA|ROD|PRACA|PC|PCA|VIELA|BECO|CONJUNTO|CONJ|CJ|LARGO|LG|AREA|LOTEAMENTO)\b\.?\s*"
)
SUBS = [
    (re.compile(r"\b(DE|DA|DO|DAS|DOS|E)\b"), " "),
    (re.compile(r"\b(DOUTOR|DR)\b"), "DR"),
    (re.compile(r"\b(PROFESSORA?|PROF)\b"), "PROF"),
    (re.compile(r"\b(SENADOR|SEN)\b"), "SEN"),
    (re.compile(r"\b(GENERAL|GAL|GEN)\b"), "GEN"),
    (re.compile(r"\b(PADRE|PE)\b"), "PE"),
    (re.compile(r"\b(SANTO|STO)\b"), "STO"),
    (re.compile(r"\b(SANTA|STA)\b"), "STA"),
    (re.compile(r"\b(SAO|S)\b"), "SAO"),
    (re.compile(r"\b(NOSSA SENHORA|N SRA|NSRA|N S)\b"), "NSRA"),
    (re.compile(r"\b(PRESIDENTE|PRES)\b"), "PRES"),
    (re.compile(r"\b(MARECHAL|MAL)\b"), "MAL"),
    (re.compile(r"\b(CORONEL|CEL)\b"), "CEL"),
    (re.compile(r"\b(CAPITAO|CAP)\b"), "CAP"),
    (re.compile(r"\b(ALMIRANTE|ALM)\b"), "ALM"),
    (re.compile(r"\b(GOVERNADOR|GOV)\b"), "GOV"),
    (re.compile(r"\b(DEPUTADO|DEP)\b"), "DEP"),
    (re.compile(r"\b(ENGENHEIRO|ENG)\b"), "ENG"),
    (re.compile(r"\b(DESEMBARGADOR|DES)\b"), "DES"),
    (re.compile(r"\b(BRIGADEIRO|BRIG)\b"), "BRIG"),
    (re.compile(r"\b(MONSENHOR|MONS)\b"), "MONS"),
    (re.compile(r"\b(MARQUES|MARQ)\b"), "MARQ"),
]
ROM = {"1": "I", "2": "II", "3": "III", "4": "IV", "5": "V", "6": "VI", "7": "VII", "8": "VIII", "9": "IX", "10": "X"}
NON_STREET_RE = re.compile(
    r"^(FIM DO LOGRADOURO|SEM NOME|RUA SEM NOME|PASSAGEM SEM NOME|RETORNO|ROTATORIA|PONTE|ACESSO)\b"
)

KY = 110574.0
KX = 111320.0 * math.cos(-1.4 * math.pi / 180.0)


def tira(s):
    s = unicodedata.normalize("NFD", str(s or ""))
    return "".join(c for c in s if unicodedata.category(c) != "Mn").upper()


def limpa(s):
    s = tira(s)
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"\s-\s.*$", " ", s)
    s = re.sub(r"-(CANTEIRO|CANAL|ORLA).*$", " ", s)
    s = s.replace("CANTEIRO CENTRAL", " ")
    s = re.sub(r"[.,\-()\"'’`]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for _ in range(2):
        s = PREF_RE.sub("", s).strip()
    for rx, rep in SUBS:
        s = rx.sub(rep, s)
    return re.sub(r"\s+", " ", s).strip()


def chave(s):
    tokens = [ROM.get(t, t) for t in limpa(s).split()]
    s = " ".join(tokens)
    s = s.replace("PH", "F").replace("Y", "I").replace("W", "V").replace("Z", "S").replace("CH", "X")
    s = re.sub(r"(.)\1+", r"\1", s)
    s = re.sub(r"\bH", "", s)
    return s.strip()


def ordena(s):
    return " ".join(sorted(s.split()))


def lev(a, b):
    if a == b:
        return 0
    m, n = len(a), len(b)
    if not m:
        return n
    if not n:
        return m
    p = list(range(n + 1))
    for i in range(1, m + 1):
        c = [i] + [0] * n
        ai = a[i - 1]
        for j in range(1, n + 1):
            cost = 0 if ai == b[j - 1] else 1
            c[j] = min(p[j] + 1, c[j - 1] + 1, p[j - 1] + cost)
        p = c
    return p[n]


def dist(a, b):
    return math.hypot((a[0] - b[0]) * KY, (a[1] - b[1]) * KX)


def dist_pt_seg(p, a, b):
    ax, ay = a[1] * KX, a[0] * KY
    bx, by = b[1] * KX, b[0] * KY
    px, py = p[1] * KX, p[0] * KY
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = ((px - ax) * dx + (py - ay) * dy) / L if L else 0.0
    t = max(0.0, min(1.0, t))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def compr(pts):
    return sum(dist(pts[i - 1], pts[i]) for i in range(1, len(pts)))


def kn(p):
    return (round(p[0] * 1e5), round(p[1] * 1e5))


def ponto_no_poligono(pt, rings):
    y, x = pt
    dentro = False
    for anel in rings:
        n = len(anel)
        j = n - 1
        for i in range(n):
            yi, xi = anel[i]
            yj, xj = anel[j]
            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
                dentro = not dentro
            j = i
    return dentro


class Localizador:
    def __init__(self, ruas_data, areas_kml):
        self.nomes = ruas_data["n"]
        self.segs = []
        self.seg_len = []
        self.seg_mid = []
        for a in ruas_data["s"]:
            ni = a[0]
            y, x = a[1], a[2]
            pts = [(y / 1e5, x / 1e5)]
            for k in range(3, len(a), 2):
                y += a[k]
                x += a[k + 1]
                pts.append((y / 1e5, x / 1e5))
            self.segs.append((ni, pts))
            self.seg_len.append(compr(pts))
            self.seg_mid.append(pts[len(pts) // 2])

        self.nome_chave = [chave(n) for n in self.nomes]
        self.por_chave = defaultdict(list)
        for idx, (ni, _) in enumerate(self.segs):
            k = self.nome_chave[ni]
            if k:
                self.por_chave[k].append(idx)

        self.chaves = [(k, ordena(k), set(k.split())) for k in self.por_chave]
        self.grade = defaultdict(list)
        for idx, (_, pts) in enumerate(self.segs):
            vis = set()
            for p in pts:
                c = (math.floor(p[0] / 0.002), math.floor(p[1] / 0.002))
                if c not in vis:
                    vis.add(c)
                    self.grade[c].append(idx)

        self.area_rings = {a: info["rings"] for a, info in areas_kml.items()}
        self.cache_rua = {}

    def vizinhos(self, pontos):
        out = set()
        for p in pontos:
            a = math.floor(p[0] / 0.002)
            b = math.floor(p[1] / 0.002)
            for i in (-1, 0, 1):
                for j in (-1, 0, 1):
                    lst = self.grade.get((a + i, b + j))
                    if lst:
                        out.update(lst)
        return out

    def similar(self, q, k):
        if q == k:
            return 1.0
        tq = [t for t in q.split() if t]
        tk = k.split()
        if not tq or not tk:
            return 0.0
        comuns = sum(
            1 for x in tq if any(x == y or (len(x) >= 5 and len(y) >= 5 and lev(x, y) <= 1) for y in tk)
        )
        cont = comuns / len(tq)
        oq, ok = ordena(q), ordena(k)
        lv = 1.0 - lev(oq, ok) / max(len(oq), len(ok))
        return max(lv, cont * 0.95)

    def achar_rua(self, nome):
        k = chave(nome)
        if not k:
            return None
        if k in self.cache_rua:
            return self.cache_rua[k]
        res = None
        if k in self.por_chave:
            res = (self.por_chave[k], k, "exata")
        if res is None:
            o = ordena(k)
            best, bd = None, 10**9
            lo = len(o)
            for ck, co, _ in self.chaves:
                if abs(len(co) - lo) > 4:
                    continue
                d = lev(o, co)
                if d < bd:
                    bd = d
                    best = (ck, co)
            if best and bd / max(lo, len(best[1])) <= 0.14:
                res = (self.por_chave[best[0]], best[0], "aproximada")
        if res is None:
            tq = k.split()
            if len(tq) >= 2 or len(k) >= 7:
                cands = [ck for ck, _, ct in self.chaves if all(x in ct for x in tq)]
                if cands:
                    segs = []
                    for ck in cands:
                        segs.extend(self.por_chave[ck])
                    res = (segs, cands[0], "aproximada")
        self.cache_rua[k] = res
        return res

    def build_graph(self, segs):
        nos = {}
        adj = defaultdict(list)
        for si in segs:
            pts = self.segs[si][1]
            a, b = kn(pts[0]), kn(pts[-1])
            nos[a] = pts[0]
            nos[b] = pts[-1]
            L = self.seg_len[si]
            adj[a].append((b, si, L))
            adj[b].append((a, si, L))
        return nos, adj

    def agrupar_trechos(self, segs, adj):
        comp = []
        visto = set()
        for si in segs:
            if si in visto:
                continue
            pts = self.segs[si][1]
            pilha = [kn(pts[0])]
            vis = set(pilha)
            ss = set()
            while pilha:
                u = pilha.pop()
                for to, esi, _ in adj[u]:
                    ss.add(esi)
                    if to not in vis:
                        vis.add(to)
                        pilha.append(to)
            visto.update(ss)
            comp.append(list(ss))

        c_pts = [[p for si in ss for p in (self.segs[si][1][0], self.segs[si][1][-1])] for ss in comp]
        pai = list(range(len(comp)))

        def raiz(i):
            while pai[i] != i:
                pai[i] = pai[pai[i]]
                i = pai[i]
            return i

        for i in range(len(comp)):
            for j in range(i + 1, len(comp)):
                if raiz(i) == raiz(j):
                    continue
                if any(dist(a, b) < 60.0 for a in c_pts[i] for b in c_pts[j]):
                    pai[raiz(j)] = raiz(i)

        grupos = defaultdict(list)
        for i, ss in enumerate(comp):
            grupos[raiz(i)].extend(ss)

        out = []
        for ss in grupos.values():
            L = sum(self.seg_len[si] for si in ss)
            lats = [self.seg_mid[si][0] for si in ss]
            lngs = [self.seg_mid[si][1] for si in ss]
            out.append({"segs": ss, "L": L, "center": (sum(lats) / len(lats), sum(lngs) / len(lngs))})
        return out

    def cruzamento(self, nos, segs_cruz, segs_main):
        if not segs_cruz:
            return None
        y0 = x0 = 1e9
        y1 = x1 = -1e9
        for si in segs_cruz:
            for p in self.segs[si][1]:
                if p[0] < y0: y0 = p[0]
                if p[0] > y1: y1 = p[0]
                if p[1] < x0: x0 = p[1]
                if p[1] > x1: x1 = p[1]
        m = 0.0006
        y0 -= m; y1 += m; x0 -= m; x1 += m

        bd = 1e9
        bp = None
        for sm in segs_main:
            pm = self.segs[sm][1]
            if not any(y0 <= p[0] <= y1 and x0 <= p[1] <= x1 for p in pm):
                continue
            for sc in segs_cruz:
                pc = self.segs[sc][1]
                for p in pm:
                    if not (y0 <= p[0] <= y1 and x0 <= p[1] <= x1):
                        continue
                    for i in range(1, len(pc)):
                        d = dist_pt_seg(p, pc[i - 1], pc[i])
                        if d < bd:
                            bd = d; bp = p
                for p in pc:
                    for i in range(1, len(pm)):
                        d = dist_pt_seg(p, pm[i - 1], pm[i])
                        if d < bd:
                            bd = d; bp = p
        if bp is None or bd > 55.0:
            return None
        bn = min(nos.items(), key=lambda kv: dist(kv[1], bp))[0]
        return (bn, bd)

    def localizar_via(self, via, de, ate, area=None, centroid=None):
        R = self.achar_rua(via)
        if not R:
            return {"ok": False, "ids": [], "tipo": "nao_encontrada"}
        all_segs = R[0]
        segs = all_segs

        if area and area in self.area_rings:
            rings = self.area_rings[area]
            dentro = [si for si in all_segs if ponto_no_poligono(self.seg_mid[si], rings)]
            if dentro:
                segs = dentro

        res = self._localizar_em_segs(segs, via, de, ate, area, centroid)
        if res["tipo"] != "trecho" and segs is not all_segs:
            res2 = self._localizar_em_segs(all_segs, via, de, ate, area, centroid)
            if res2["tipo"] == "trecho":
                return res2
        return res

    def _localizar_em_segs(self, segs, via, de, ate, area, centroid):
        nos, adj = self.build_graph(segs)
        all_pts = [p for si in segs for p in self.segs[si][1]]
        perto = self.vizinhos(all_pts)
        proprios = set(segs)
        via_k = chave(via)

        def transversal(nome):
            if not nome:
                return None
            if NON_STREET_RE.match(tira(nome).strip()):
                return None
            q = chave(nome)
            if not q or q == via_k:
                return None
            por_nome = defaultdict(list)
            for si in perto:
                if si in proprios:
                    continue
                por_nome[self.segs[si][0]].append(si)
            cands = []
            for ni, lst in por_nome.items():
                s = self.similar(q, self.nome_chave[ni])
                if s >= 0.62:
                    cands.append((s, lst))
            cands.sort(key=lambda x: -x[0])
            for _, lst in cands[:4]:
                c = self.cruzamento(nos, lst, segs)
                if c:
                    return c
            X = self.achar_rua(nome)
            if X:
                near_x = [si for si in X[0] if si in perto]
                if near_x:
                    return self.cruzamento(nos, near_x, segs)
            return None

        ca = transversal(de)
        cb = transversal(ate)

        if ca and cb and ca[0] != cb[0]:
            dist_map = {ca[0]: 0.0}
            prev = {}
            pq = [(0.0, ca[0])]
            feito = set()
            while pq:
                d, u = heapq.heappop(pq)
                if u in feito:
                    continue
                feito.add(u)
                if u == cb[0]:
                    break
                for to, si, L in adj[u]:
                    nd = d + L
                    if nd < dist_map.get(to, 1e18):
                        dist_map[to] = nd
                        prev[to] = (u, si)
                        heapq.heappush(pq, (nd, to))
            if cb[0] in dist_map and dist_map[cb[0]] < 15000.0:
                lst = []
                v = cb[0]
                while v != ca[0]:
                    u, si = prev[v]
                    lst.append(si)
                    v = u
                return {"ok": True, "ids": lst, "tipo": "trecho"}

        c1 = ca or cb
        if c1:
            vis = {c1[0]}
            pilha = [c1[0]]
            ss = set()
            while pilha:
                u = pilha.pop()
                for to, si, _ in adj[u]:
                    ss.add(si)
                    if to not in vis:
                        vis.add(to)
                        pilha.append(to)
            return {"ok": True, "ids": list(ss), "tipo": "cruzamento_unico"}

        grupos = self.agrupar_trechos(segs, adj)
        if not grupos:
            return {"ok": False, "ids": [], "tipo": "vazio"}
        if len(grupos) == 1:
            return {"ok": True, "ids": grupos[0]["segs"], "tipo": "rua_unica"}

        if centroid is not None:
            best = min(grupos, key=lambda g: dist(g["center"], centroid))
            return {"ok": True, "ids": best["segs"], "tipo": "rua_centroide"}

        if area and area in self.area_rings:
            rings = self.area_rings[area]
            best = max(
                grupos,
                key=lambda g: sum(self.seg_len[si] for si in g["segs"] if ponto_no_poligono(self.seg_mid[si], rings)),
            )
            if any(ponto_no_poligono(self.seg_mid[si], rings) for si in best["segs"]):
                return {"ok": True, "ids": best["segs"], "tipo": "rua_area"}

        best = max(grupos, key=lambda g: g["L"])
        return {"ok": True, "ids": best["segs"], "tipo": "rua_maior"}

    def area_dos_segs(self, seg_ids):
        if not seg_ids:
            return None
        cont = defaultdict(float)
        for si in seg_ids:
            m = self.seg_mid[si]
            for a, rings in self.area_rings.items():
                if ponto_no_poligono(m, rings):
                    cont[a] += self.seg_len[si]
                    break
        if cont:
            return max(cont.items(), key=lambda kv: kv[1])[0]
        lats = [self.seg_mid[si][0] for si in seg_ids]
        lngs = [self.seg_mid[si][1] for si in seg_ids]
        c = (statistics.median(lats), statistics.median(lngs))
        best_a, best_d = None, 1e18
        for a, rings in self.area_rings.items():
            for anel in rings:
                for p in anel[::5]:
                    d = dist(c, p)
                    if d < best_d:
                        best_d = d; best_a = a
        return best_a

    def localizar_setor(self, plan, area_hint=None):
        vias = plan.get("vias", [])
        pass1 = []
        hi_pts = []
        for v in vias:
            via = v[0] if len(v) > 0 else ""
            de = v[1] if len(v) > 1 else ""
            ate = v[2] if len(v) > 2 else ""
            r = self.localizar_via(via, de, ate, area=area_hint, centroid=None)
            pass1.append((via, de, ate, r))
            if r["ok"] and r["tipo"] in ("trecho", "cruzamento_unico", "rua_unica"):
                for si in r["ids"]:
                    hi_pts.append(self.seg_mid[si])

        centroid = None
        if hi_pts:
            centroid = (
                statistics.median(p[0] for p in hi_pts),
                statistics.median(p[1] for p in hi_pts),
            )

        final_ids = set()
        unmatched = []
        for via, de, ate, r in pass1:
            if r["ok"] and r["tipo"] in ("rua_maior", "rua_area", "rua_centroide") and centroid is not None:
                r = self.localizar_via(via, de, ate, area=area_hint, centroid=centroid)

            if not r["ok"]:
                unmatched.append((via, de, ate))
                continue

            if centroid is not None and len(hi_pts) >= 5 and r["tipo"] not in ("trecho",):
                c_via = (
                    statistics.median(self.seg_mid[si][0] for si in r["ids"]),
                    statistics.median(self.seg_mid[si][1] for si in r["ids"]),
                )
                if dist(c_via, centroid) > 4500.0:
                    unmatched.append((via, de, ate))
                    continue

            final_ids.update(r["ids"])

        return sorted(final_ids), unmatched


def merge_segments(loc, seg_ids):
    if not seg_ids:
        return []
    unused = set(seg_ids)
    node_to_segs = defaultdict(list)
    seg_pts = {}
    for si in seg_ids:
        pts = loc.segs[si][1]
        seg_pts[si] = pts
        node_to_segs[kn(pts[0])].append(si)
        node_to_segs[kn(pts[-1])].append(si)

    polylines = []
    while unused:
        start = next(iter(unused))
        unused.remove(start)
        chain = list(seg_pts[start])
        while True:
            end_k = kn(chain[-1])
            nxt = next((s for s in node_to_segs[end_k] if s in unused), None)
            if nxt is None:
                break
            unused.remove(nxt)
            pts = seg_pts[nxt]
            if kn(pts[0]) == end_k:
                chain.extend(pts[1:])
            else:
                chain.extend(reversed(pts[:-1]))
        while True:
            start_k = kn(chain[0])
            prv = next((s for s in node_to_segs[start_k] if s in unused), None)
            if prv is None:
                break
            unused.remove(prv)
            pts = seg_pts[prv]
            if kn(pts[-1]) == start_k:
                chain = list(pts[:-1]) + chain
            else:
                chain = list(reversed(pts[1:])) + chain
        polylines.append(chain)
    return polylines


# ---------------------------------------------------------------------------
# 3. Geração de KML/KMZ (com suporte a polígonos de fundo + linhas) e GeoJSON
# ---------------------------------------------------------------------------

def hex_to_kml(rrggbb, alpha="ff"):
    return f"{alpha}{rrggbb[4:6]}{rrggbb[2:4]}{rrggbb[0:2]}"


PALETTE_HEX = [
    "dc2626",  # vermelho
    "2563eb",  # azul
    "16a34a",  # verde
    "9333ea",  # roxo
    "ea580c",  # laranja
    "0891b2",  # ciano
    "db2777",  # rosa
    "ca8a04",  # âmbar
    "4f46e5",  # índigo
    "0d9488",  # teal
]
RED_PDF_KML = hex_to_kml("dc2626", "ff")
AREA_LINE_KML = hex_to_kml("1e3a8a", "ff")   # azul-marinho contorno
AREA_FILL_KML = hex_to_kml("3b82f6", "28")   # azul bem translúcido (16% opacidade)


def area_bg_folder(areas_kml, area_nums):
    """Cria a camada de fundo com os polígonos oficiais das áreas indicadas."""
    pms = []
    for a in area_nums:
        if a not in areas_kml:
            continue
        info = areas_kml[a]
        pms.append({
            "type": "polygon",
            "name": f"Área {a:02d} ({info['name']})",
            "line_color": AREA_LINE_KML,
            "fill_color": AREA_FILL_KML,
            "width": 2.5,
            "data": [("Área", f"Área {a:02d}"), ("Código", info["name"])],
            "description": f"Limite oficial da <b>Área {a:02d}</b> (Novas Áreas).",
            "rings": info["rings"],
        })
    label = f"Fundo · Área {area_nums[0]:02d}" if len(area_nums) == 1 else "Fundo · Novas Áreas (01 a 10)"
    return {"name": label, "placemarks": pms}


def build_kmz(out_path, doc_name, doc_desc, folders):
    assert len(folders) <= 10, f"Limite do Google My Maps (10 camadas) excedido: {len(folders)} em {out_path}"

    styles = {}
    style_xml = []

    def style_line(color, width):
        key = ("L", color, width)
        if key not in styles:
            sid = f"stl_{len(styles)}"
            styles[key] = sid
            style_xml.append(
                f'  <Style id="{sid}">\n'
                f'    <LineStyle><color>{color}</color><width>{width}</width></LineStyle>\n'
                f'  </Style>'
            )
        return styles[key]

    def style_poly(line_color, fill_color, width):
        key = ("P", line_color, fill_color, width)
        if key not in styles:
            sid = f"stp_{len(styles)}"
            styles[key] = sid
            style_xml.append(
                f'  <Style id="{sid}">\n'
                f'    <LineStyle><color>{line_color}</color><width>{width}</width></LineStyle>\n'
                f'    <PolyStyle><color>{fill_color}</color></PolyStyle>\n'
                f'  </Style>'
            )
        return styles[key]

    folder_xml = []
    for fld in folders:
        pms_xml = []
        for pm in fld["placemarks"]:
            ext_rows = [
                f'          <Data name="{escape(str(k))}"><value>{escape(str(v))}</value></Data>'
                for k, v in pm.get("data", [])
            ]
            if pm.get("type") == "polygon":
                rings = pm.get("rings", [])
                if not rings:
                    continue
                sid = style_poly(pm["line_color"], pm["fill_color"], pm.get("width", 2.5))
                polys_xml = []
                for ring in rings:
                    pts = list(ring)
                    if pts[0] != pts[-1]:
                        pts.append(pts[0])
                    coords = " ".join(f"{lng:.5f},{lat:.5f},0" for lat, lng in pts)
                    polys_xml.append(
                        "          <Polygon><outerBoundaryIs><LinearRing>"
                        f"<coordinates>{coords}</coordinates>"
                        "</LinearRing></outerBoundaryIs></Polygon>"
                    )
                geom_xml = (
                    polys_xml[0].strip()
                    if len(polys_xml) == 1
                    else "<MultiGeometry>\n" + "\n".join(polys_xml) + "\n        </MultiGeometry>"
                )
            else:
                pls = pm.get("polylines", [])
                if not pls:
                    continue
                sid = style_line(pm["color"], pm.get("width", 4.0))
                lines_xml = []
                for pl in pls:
                    coords = " ".join(f"{lng:.5f},{lat:.5f},0" for lat, lng in pl)
                    lines_xml.append(
                        f'          <LineString><tessellate>1</tessellate><coordinates>{coords}</coordinates></LineString>'
                    )
                geom_xml = (
                    lines_xml[0].strip()
                    if len(lines_xml) == 1
                    else "<MultiGeometry>\n" + "\n".join(lines_xml) + "\n        </MultiGeometry>"
                )

            pms_xml.append(
                f'      <Placemark>\n'
                f'        <name>{escape(pm["name"])}</name>\n'
                f'        <description><![CDATA[{pm["description"]}]]></description>\n'
                f'        <styleUrl>#{sid}</styleUrl>\n'
                f'        <ExtendedData>\n' + "\n".join(ext_rows) + "\n        </ExtendedData>\n"
                f'        {geom_xml}\n'
                f'      </Placemark>'
            )
        folder_xml.append(
            f'    <Folder>\n'
            f'      <name>{escape(fld["name"])}</name>\n'
            + "\n".join(pms_xml)
            + "\n    </Folder>"
        )

    kml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2">\n'
        '  <Document>\n'
        f'    <name>{escape(doc_name)}</name>\n'
        f'    <description>{escape(doc_desc)}</description>\n'
        + "\n".join(style_xml) + "\n"
        + "\n".join(folder_xml) + "\n"
        '  </Document>\n'
        '</kml>\n'
    )

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr("doc.kml", kml.encode("utf-8"))


def save_geojson(path, features):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fc = {"type": "FeatureCollection", "features": features}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 4. Execução principal
# ---------------------------------------------------------------------------

DOM_LAYER_NAMES = {
    "01": "Alternada Ímpar · Diurno (01.xx)",
    "02": "Alternada Par · Diurno (02.xx)",
    "03": "Alternada Ímpar · Noturno (03.xx)",
    "04": "Alternada Par · Noturno (04.xx)",
    "05": "Diária · Noturno (05.xx)",
    "06": "Diária · Diurno (06.xx)",
}


def vias_resumo(vias, max_vias=30):
    nomes = []
    seen = set()
    for v in vias:
        if v and v[0] and v[0] not in seen:
            seen.add(v[0])
            nomes.append(v[0])
    if len(nomes) > max_vias:
        return ", ".join(nomes[:max_vias]) + f" (+{len(nomes) - max_vias} vias)"
    return ", ".join(nomes)


def main():
    if os.path.exists(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    os.makedirs(OUT_DIR, exist_ok=True)

    ruas = fetch_or_cache(URL_RUAS, "ruas.js")
    areas_kml = load_areas_kml()
    dados = load_dados_js()

    loc = Localizador(ruas, areas_kml)
    print(f"Base carregada: {len(loc.nomes)} vias, {len(loc.segs)} segmentos, {len(areas_kml)} áreas oficiais do KML.")

    report_rows = []

    # =======================================================================
    # 1) COLETA DOMICILIAR -> por Área + por Setor + GeoJSON
    # =======================================================================
    coleta_by_area = defaultdict(lambda: defaultdict(list))
    coleta_geojson = []
    dom_tot_vias = dom_ok_vias = dom_ok_setores = 0

    for idx, plan in enumerate(dados["domiciliar"]):
        h = plan["h"]
        setor = h["SETOR"]
        pref = setor.split(".")[0]
        slug = re.sub(r"[^A-Za-z0-9]+", "-", setor).strip("-")
        vias = plan["vias"]

        seg_ids, unmatched = loc.localizar_setor(plan, area_hint=None)
        polylines = merge_segments(loc, seg_ids)
        ext_m = round(sum(loc.seg_len[si] for si in seg_ids))
        area_det = loc.area_dos_segs(seg_ids) or 1

        n_tot = len(vias)
        n_ok = n_tot - len(unmatched)
        dom_tot_vias += n_tot
        dom_ok_vias += n_ok
        if seg_ids:
            dom_ok_setores += 1

        resumo = vias_resumo(vias)
        data_fields = [
            ("Setor", setor),
            ("Área", f"Área {area_det:02d}"),
            ("Frequência", h.get("FREQUÊNCIA", "")),
            ("Dias", h.get("DIAS DE SERVIÇO", "")),
            ("Horário", h.get("HORÁRIO", "")),
            ("Trechos", f"{n_ok}/{n_tot}"),
            ("Extensão (m)", ext_m),
            ("Vias", resumo),
        ]
        desc = (
            f"<b>Setor {setor}</b> · Área {area_det:02d}<br>"
            f"<b>Frequência:</b> {h.get('FREQUÊNCIA', '')} ({h.get('DIAS DE SERVIÇO', '')})<br>"
            f"<b>Horário:</b> {h.get('HORÁRIO', '')}<br>"
            f"<b>Trechos localizados:</b> {n_ok}/{n_tot} (~{ext_m} m)"
        )
        color_hex = PALETTE_HEX[idx % len(PALETTE_HEX)]

        # Para o KMZ por Área
        coleta_by_area[area_det][pref].append({
            "type": "line",
            "name": f"Setor {setor}",
            "color": hex_to_kml(color_hex),
            "width": 4.5,
            "data": data_fields,
            "description": desc,
            "polylines": polylines,
        })

        # KMZ individual do Setor (igual ao PDF: fundo da Área + setor em vermelho)
        pm_single = {
            "type": "line",
            "name": f"Setor {setor} · {h.get('FREQUÊNCIA', '')}",
            "color": RED_PDF_KML,
            "width": 5.0,
            "data": data_fields,
            "description": desc,
            "polylines": polylines,
        }
        build_kmz(
            os.path.join(OUT_DIR, "por_setor", "coleta", f"Area_{area_det:02d}", f"Setor_{slug}.kmz"),
            f"Coleta Domiciliar · Setor {setor}",
            f"Setor {setor} — {h.get('FREQUÊNCIA', '')} · {h.get('HORÁRIO', '')}",
            [
                area_bg_folder(areas_kml, [area_det]),
                {"name": f"Setor {setor}", "placemarks": [pm_single]},
            ],
        )

        # GeoJSON para os sites (mapas.cblplanejamento.com.br / mapadeplanejamento.vercel.app)
        if polylines:
            coleta_geojson.append({
                "type": "Feature",
                "properties": {
                    "nome": f"Setor {setor}",
                    "operacao": "Coleta Domiciliar",
                    "setor": setor,
                    "area": area_det,
                    "frequencia": h.get("FREQUÊNCIA", ""),
                    "dias": h.get("DIAS DE SERVIÇO", ""),
                    "horario": h.get("HORÁRIO", ""),
                    "trechos_ok": n_ok,
                    "trechos_total": n_tot,
                    "extensao_m": ext_m,
                    "cor": "#" + color_hex,
                },
                "geometry": {
                    "type": "MultiLineString",
                    "coordinates": [[[round(lng, 5), round(lat, 5)] for lat, lng in pl] for pl in polylines],
                },
            })

        report_rows.append([
            "domiciliar", setor, f"Área {area_det:02d}", f"Grupo {pref}", n_tot, n_ok,
            len(unmatched), ext_m, "; ".join(u[0] for u in unmatched[:10])
        ])

    # Grava 1 KMZ de Coleta por Área
    for area_num in range(1, 11):
        prefs = coleta_by_area.get(area_num, {})
        folders = [area_bg_folder(areas_kml, [area_num])]
        for pref in sorted(prefs.keys()):
            folders.append({"name": DOM_LAYER_NAMES.get(pref, f"Setores {pref}"), "placemarks": prefs[pref]})
        if len(folders) > 1:
            build_kmz(
                os.path.join(OUT_DIR, "coleta_por_area", f"Coleta_Area_{area_num:02d}.kmz"),
                f"Coleta Domiciliar · Área {area_num:02d}",
                f"Setores de Coleta Domiciliar na Área {area_num:02d}, com fundo oficial Novas Áreas.",
                folders,
            )

    print(f"Coleta Domiciliar: {dom_ok_setores}/{len(dados['domiciliar'])} setores | {dom_ok_vias}/{dom_tot_vias} trechos ({dom_ok_vias/dom_tot_vias*100:.1f}%)")

    # =======================================================================
    # 2) MUTIRÃO -> por Área (camadas por Equipe) + por Setor + GeoJSON
    # =======================================================================
    mut_by_area = defaultdict(lambda: defaultdict(list))
    mut_geojson = []
    mut_tot_vias = mut_ok_vias = mut_ok_setores = 0

    for idx, plan in enumerate(dados["mutirao"]):
        h = plan["h"]
        setor = h["SETOR"]
        slug = re.sub(r"[^A-Za-z0-9]+", "-", setor).strip("-")
        area_raw = str(h.get("ÁREA", "")).strip()
        is_avulso = area_raw == "999"
        area_key = 999 if is_avulso else int(area_raw)
        area_hint = area_key if area_key in areas_kml else None
        equipe = str(h.get("EQUIPE", "")).strip() or "Sem Equipe"
        vias = plan["vias"]

        seg_ids, unmatched = loc.localizar_setor(plan, area_hint=area_hint)
        polylines = merge_segments(loc, seg_ids)
        ext_m = round(sum(loc.seg_len[si] for si in seg_ids))
        area_geo = area_hint or loc.area_dos_segs(seg_ids) or 1

        n_tot = len(vias)
        n_ok = n_tot - len(unmatched)
        mut_tot_vias += n_tot
        mut_ok_vias += n_ok
        if seg_ids:
            mut_ok_setores += 1

        resumo = vias_resumo(vias)
        data_fields = [
            ("Setor", setor),
            ("Área", "Área 999 (Avulso)" if is_avulso else f"Área {area_key:02d}"),
            ("Equipe", equipe),
            ("Frequência", h.get("FREQUÊNCIA", "")),
            ("Semana", h.get("SEMANA DE EXECUÇÃO", "")),
            ("Turno", h.get("TURNO", "")),
            ("Horário", h.get("HORÁRIO", "")),
            ("Trechos", f"{n_ok}/{n_tot}"),
            ("Extensão (m)", ext_m),
            ("Vias", resumo),
        ]
        desc = (
            f"<b>{setor}</b> · {'Área 999 (Avulso)' if is_avulso else f'Área {area_key:02d}'} · Equipe {equipe}<br>"
            f"<b>Frequência:</b> {h.get('FREQUÊNCIA', '')} · <b>Semana:</b> {h.get('SEMANA DE EXECUÇÃO', '')}<br>"
            f"<b>Turno:</b> {h.get('TURNO', '')} ({h.get('HORÁRIO', '')})<br>"
            f"<b>Trechos localizados:</b> {n_ok}/{n_tot} (~{ext_m} m)"
        )
        color_hex = PALETTE_HEX[idx % len(PALETTE_HEX)]

        mut_by_area[area_key][equipe].append({
            "type": "line",
            "name": f"{setor} · Sem.{h.get('SEMANA DE EXECUÇÃO', '')}",
            "color": hex_to_kml(color_hex),
            "width": 4.5,
            "data": data_fields,
            "description": desc,
            "polylines": polylines,
        })

        # KMZ individual do Setor (igual ao PDF)
        pm_single = {
            "type": "line",
            "name": f"{setor} · Eq.{equipe} · Sem.{h.get('SEMANA DE EXECUÇÃO', '')}",
            "color": RED_PDF_KML,
            "width": 5.0,
            "data": data_fields,
            "description": desc,
            "polylines": polylines,
        }
        area_folder_name = "Area_999_Avulsos" if is_avulso else f"Area_{area_key:02d}"
        bg_areas = list(range(1, 11)) if is_avulso else [area_key]
        build_kmz(
            os.path.join(OUT_DIR, "por_setor", "mutirao", area_folder_name, f"{slug}.kmz"),
            f"Mutirão · {setor}",
            f"Setor {setor} — Área {area_raw} · Equipe {equipe} · Semana {h.get('SEMANA DE EXECUÇÃO', '')}",
            [
                area_bg_folder(areas_kml, bg_areas),
                {"name": f"{setor} (Eq.{equipe})", "placemarks": [pm_single]},
            ],
        )

        if polylines:
            mut_geojson.append({
                "type": "Feature",
                "properties": {
                    "nome": setor,
                    "operacao": "Mutirão",
                    "setor": setor,
                    "area": area_key,
                    "area_geo": area_geo,
                    "equipe": equipe,
                    "frequencia": h.get("FREQUÊNCIA", ""),
                    "semana": h.get("SEMANA DE EXECUÇÃO", ""),
                    "turno": h.get("TURNO", ""),
                    "horario": h.get("HORÁRIO", ""),
                    "trechos_ok": n_ok,
                    "trechos_total": n_tot,
                    "extensao_m": ext_m,
                    "cor": "#" + color_hex,
                },
                "geometry": {
                    "type": "MultiLineString",
                    "coordinates": [[[round(lng, 5), round(lat, 5)] for lat, lng in pl] for pl in polylines],
                },
            })

        report_rows.append([
            "mutirao", setor, f"Área {area_raw}", f"Equipe {equipe}", n_tot, n_ok,
            len(unmatched), ext_m, "; ".join(u[0] for u in unmatched[:10])
        ])

    # Grava 1 KMZ de Mutirão por Área (camada de fundo + 1 camada por Equipe)
    for area_key in sorted(mut_by_area.keys()):
        eq_map = mut_by_area[area_key]
        is_av = area_key == 999
        bg_areas = list(range(1, 11)) if is_av else [area_key]
        folders = [area_bg_folder(areas_kml, bg_areas)]
        for eq in sorted(eq_map.keys(), key=lambda x: int(x) if x.isdigit() else 9999):
            folders.append({"name": f"Equipe {eq}", "placemarks": eq_map[eq]})
        fname = "Mutirao_Area_999_Avulsos.kmz" if is_av else f"Mutirao_Area_{area_key:02d}.kmz"
        title = "Mutirão · Área 999 (Avulsos / Sábado)" if is_av else f"Mutirão · Área {area_key:02d}"
        build_kmz(
            os.path.join(OUT_DIR, "mutirao_por_area", fname),
            title,
            f"{title}, organizado por Equipe com fundo oficial Novas Áreas.",
            folders,
        )

    print(f"Mutirão: {mut_ok_setores}/{len(dados['mutirao'])} setores | {mut_ok_vias}/{mut_tot_vias} trechos ({mut_ok_vias/mut_tot_vias*100:.1f}%)")

    # =======================================================================
    # 3) GeoJSONs prontos para mapas.cblplanejamento.com.br e mapadeplanejamento.vercel.app
    # =======================================================================
    areas_geojson = []
    for a in sorted(areas_kml.keys()):
        info = areas_kml[a]
        polys = []
        for ring in info["rings"]:
            pts = [[round(lng, 5), round(lat, 5)] for lat, lng in ring]
            if pts[0] != pts[-1]:
                pts.append(pts[0])
            polys.append([pts])
        areas_geojson.append({
            "type": "Feature",
            "properties": {"nome": f"Área {a:02d}", "codigo": info["name"], "area": a},
            "geometry": {
                "type": "Polygon" if len(polys) == 1 else "MultiPolygon",
                "coordinates": polys[0] if len(polys) == 1 else polys,
            },
        })

    gdir = os.path.join(OUT_DIR, "geojson_para_sites")
    save_geojson(os.path.join(gdir, "novas_areas.geojson"), areas_geojson)
    save_geojson(os.path.join(gdir, "coleta_domiciliar.geojson"), coleta_geojson)
    save_geojson(os.path.join(gdir, "mutirao.geojson"), mut_geojson)

    # Relatório CSV
    csv_path = os.path.join(OUT_DIR, "relatorio_reconstrucao.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["operacao", "setor", "area", "grupo_ou_equipe", "trechos_total", "trechos_ok", "trechos_nao_encontrados", "extensao_m", "amostra_nao_encontradas"])
        w.writerows(report_rows)

    print("Concluído com sucesso! Tudo salvo em:", OUT_DIR)


if __name__ == "__main__":
    main()
