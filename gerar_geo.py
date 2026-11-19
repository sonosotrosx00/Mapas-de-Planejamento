"""Gera a geometria real (lat/lon) de cada setor a partir dos próprios mapas do PDF,
para abrir no Google My Maps com o MESMO desenho do site/PDF.

Como funciona:
  1. Georreferencia cada página de mapa casando os nomes de rua impressos no PDF
     com a base de ruas (cache_cbl/ruas.js) e ajustando as linhas cinzas do PDF às ruas.
  2. Lê os pixels vermelhos (vias atendidas) e azuis (área do setor) do mapa em alta
     (site/mapas/...jpg) e converte para coordenadas.
     - Vias: segue a geometria das ruas da base onde o mapa está vermelho.
     - Área: contorno da mancha azul.
  3. Grava:
     - site/dados.js            -> acrescenta "geo" (centro e zoom) em cada setor
     - geo_setores.json         -> geometria completa (usada pelo gerar_site.py)
     - mymaps_pdf/*.kmz         -> arquivos para importar no Google My Maps
     - mymaps_pdf/mutirao_por_area/*.kmz -> o Mutirão separado por Área
     - mymaps_pdf/relatorio.csv -> qualidade do encaixe de cada setor

Uso:  python gerar_geo.py            (rode depois do gerar_site.py; depois rode gerar_site.py --data)
      python gerar_geo.py --so-kml   (só refaz os arquivos do My Maps)
Requer: pip install pymupdf pillow numpy scipy opencv-python-headless
"""
import csv, json, math, os, re, sys, unicodedata
from xml.sax.saxutils import escape

import cv2
import numpy as np
import pymupdf as fitz
from PIL import Image
from scipy.spatial import cKDTree

ROOT = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(ROOT, "site")
SAIDA = os.path.join(ROOT, "mymaps_pdf")
OPS = [
    ("domiciliar", "Mapa Coleta - Operação.pdf", "Coleta Domiciliar"),
    ("mutirao", "Mapa Mutirão - Operação.pdf", "Mutirão"),
    # ("varricao", "Mapa Varrição - Operação.pdf", "Varrição"),
]
ESCALA_IMG = 4.0          # os JPG são renderizados a 4 px por ponto do PDF
LIMITE_MAPA_Y = 563.0     # abaixo disso é a tarja de informações (pt)

LAT0 = -1.40
KX = 111320 * math.cos(math.radians(LAT0))
KY = 110574.0


def chave(s):
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def amostrar(pts, passo):
    out = []
    for a, b in zip(pts[:-1], pts[1:]):
        L = float(np.hypot(*(b - a)))
        n = max(1, int(L / passo))
        t = np.linspace(0, 1, n, endpoint=False)[:, None]
        out.append(a + (b - a) * t)
    out.append(pts[-1:])
    return np.vstack(out)


class Base:
    """Base de ruas (cache_cbl/ruas.js) em metros locais."""

    def __init__(self):
        s = open(os.path.join(ROOT, "cache_cbl", "ruas.js"), encoding="utf-8").read()
        d = json.loads(s[s.index("{"):s.rindex("}") + 1])
        self.nomes = [chave(n) for n in d["n"]]
        self.segs = []
        for a in d["s"]:
            y, x = a[1], a[2]
            pts = [(x / 1e5 * KX, y / 1e5 * KY)]
            for k in range(3, len(a), 2):
                y += a[k]; x += a[k + 1]
                pts.append((x / 1e5 * KX, y / 1e5 * KY))
            self.segs.append((a[0], np.array(pts)))
        por_nome, todos, dono = {}, [], []
        for i, (ni, pts) in enumerate(self.segs):
            por_nome.setdefault(self.nomes[ni], []).append(amostrar(pts, 8.0))
            sp = amostrar(pts, 2.0)
            todos.append(sp); dono.append(np.full(len(sp), i))
        self.por_nome = {k: np.vstack(v) for k, v in por_nome.items()}
        self.peso_nome = {}
        for k, P in self.por_nome.items():
            celulas = set(map(tuple, np.floor(P / 400).astype(int)))
            grupos, vistos = 0, set()
            for c in celulas:
                if c in vistos:
                    continue
                grupos += 1
                pilha = [c]
                while pilha:
                    x = pilha.pop()
                    if x in vistos:
                        continue
                    vistos.add(x)
                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            v = (x[0] + dx, x[1] + dy)
                            if v in celulas and v not in vistos:
                                pilha.append(v)
            self.peso_nome[k] = 1.0 / grupos
        self.pts = np.vstack(todos)
        self.dono = np.concatenate(dono)
        self.arvore = cKDTree(self.pts)


def rotulos(page):
    out = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            t = "".join(s["text"] for s in l["spans"]).strip()
            x0, y0, x1, y1 = l["bbox"]
            if len(t) >= 6 and y1 < LIMITE_MAPA_Y:
                out.append((chave(t), ((x0 + x1) / 2, (y0 + y1) / 2)))
    return out


def linhas_cinza(page):
    am = []
    for d in page.get_drawings():
        c = d.get("color")
        if not c or abs(c[0] - 0.38) > 0.03 or abs(c[1] - 0.38) > 0.03:
            continue
        for it in d["items"]:
            if it[0] == "l":
                a, b = it[1], it[2]
            elif it[0] == "c":
                a, b = it[1], it[4]
            else:
                continue
            L = math.hypot(b.x - a.x, b.y - a.y)
            n = max(1, int(L / 1.5))
            for k in range(n + 1):
                t = k / n
                am.append((a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t))
    return np.array(am)


def escala_da_barra(page):
    """Metros por ponto, lidos da barra de escala de 100 m no canto do mapa."""
    for d in page.get_drawings():
        c, r = d.get("color"), d["rect"]
        if c and abs(c[0] - 0.027) < 0.02 and abs(c[2] - 0.47) < 0.03 and 545 < r.y0 < 556 and r.x0 < 30 and r.width > 10:
            return 100.0 / r.width
    return None


def ajustar(G, base, s, tx, ty):
    """Refina (escala, deslocamento) encaixando as linhas cinzas do PDF nas ruas da base."""
    for lim in (60, 30, 15, 8, 5, 5, 5):
        W = np.column_stack([s * G[:, 0] + tx, -s * G[:, 1] + ty])
        d, idx = base.arvore.query(W)
        ok = d < lim
        n = int(ok.sum())
        if n < 50:
            return None
        M = np.vstack([np.column_stack([G[ok, 0], np.ones(n), np.zeros(n)]),
                       np.column_stack([-G[ok, 1], np.zeros(n), np.ones(n)])])
        alvo = np.concatenate([base.pts[idx[ok], 0], base.pts[idx[ok], 1]])
        s, tx, ty = np.linalg.lstsq(M, alvo, rcond=None)[0]
    W = np.column_stack([s * G[:, 0] + tx, -s * G[:, 1] + ty])
    d, _ = base.arvore.query(W)
    return float(s), float(tx), float(ty), float((d < 4).mean())


def conferir_rotulos(rots, base, s, tx, ty):
    """Fração dos nomes de rua do mapa que caem a menos de 25 m da rua de mesmo nome."""
    bons = 0
    for n, (px, py) in rots:
        P = np.array([s * px + tx, -s * py + ty])
        if np.min(np.hypot(*(base.por_nome[n] - P).T)) < 25:
            bons += 1
    return bons / max(1, len(rots))


def georref(page, base):
    """Devolve (s, tx, ty, encaixe, rotulos_ok): mundo = (s*x + tx, -s*y + ty), x/y em pontos do PDF."""
    rots = [(n, p) for n, p in rotulos(page) if n in base.por_nome]
    G = linhas_cinza(page)
    if len(rots) < 3 or len(G) < 50:
        return None
    s0 = escala_da_barra(page)
    escalas = s0 * np.linspace(0.96, 1.04, 9) if s0 else np.exp(np.linspace(math.log(0.6), math.log(8), 160))
    candidatos = []
    for s in escalas:
        votos = {}
        for n, (px, py) in rots:
            T = base.por_nome[n] - np.array([s * px, -s * py])
            for bx, by in np.unique(np.floor(T / 30).astype(np.int64), axis=0):
                votos.setdefault((int(bx), int(by)), set()).add(n)
        for k, v in votos.items():
            peso = sum(base.peso_nome[n] for n in v)
            candidatos.append((peso, float(s), (k[0] + .5) * 30, (k[1] + .5) * 30))
    candidatos.sort(reverse=True)
    escolhidos = []
    for c in candidatos:
        if all(abs(c[2] - e[2]) > 90 or abs(c[3] - e[3]) > 90 for e in escolhidos):
            escolhidos.append(c)
        if len(escolhidos) >= 12:
            break
    melhor = None
    for _, s, tx, ty in escolhidos:
        r = ajustar(G, base, s, tx, ty)
        if r and (s0 is None or abs(r[0] / s0 - 1) < 0.08):
            nota = r[3] + conferir_rotulos(rots, base, *r[:3])
            if not melhor or nota > melhor[0]:
                melhor = (nota, r)
    if not melhor:
        return None
    s, tx, ty, enc = melhor[1]
    return s, tx, ty, enc, conferir_rotulos(rots, base, s, tx, ty)


def latlon(X, Y):
    return round(float(Y) / KY, 6), round(float(X) / KX, 6)


def mascaras(jpg):
    a = np.asarray(Image.open(jpg).convert("RGB")).astype(np.int16)
    R, G, B = a[..., 0], a[..., 1], a[..., 2]
    corte = int(LIMITE_MAPA_Y * ESCALA_IMG)
    vermelho = (R - G > 30) & (R - B > 8) & (R > 150)
    azul = (B - R > 25) & (B > 190) & (R > 120)
    vermelho[corte:] = False
    azul[corte:] = False
    return vermelho.astype(np.uint8), azul.astype(np.uint8)


def afinar(m):
    """Esqueleto de 1 pixel (Zhang-Suen) de uma máscara 0/1."""
    m = (m > 0).astype(np.uint8)
    m = np.pad(m, 1)
    while True:
        mudou = False
        for passo in (0, 1):
            P = [np.roll(np.roll(m, -dy, 0), -dx, 1) for dy, dx in
                 ((-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1))]
            B = sum(P)
            A = sum(((P[i] == 0) & (P[(i + 1) % 8] == 1)).astype(np.uint8) for i in range(8))
            if passo == 0:
                c = (P[0] * P[2] * P[4] == 0) & (P[2] * P[4] * P[6] == 0)
            else:
                c = (P[0] * P[2] * P[6] == 0) & (P[0] * P[4] * P[6] == 0)
            tira = (m == 1) & (B >= 2) & (B <= 6) & (A == 1) & c
            if tira.any():
                m[tira] = 0
                mudou = True
        if not mudou:
            return m[1:-1, 1:-1]


def tracar(esq, min_px=40):
    """Transforma o esqueleto em linhas (lista de arrays de pixels x,y)."""
    ys, xs = np.nonzero(esq)
    pts = set(zip(xs.tolist(), ys.tolist()))
    viz = lambda p: [(p[0] + dx, p[1] + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                     if (dx or dy) and (p[0] + dx, p[1] + dy) in pts]
    grau = {p: len(viz(p)) for p in pts}
    usados = set()
    linhas = []
    inicios = [p for p in pts if grau[p] != 2] + list(pts)
    for p0 in inicios:
        for q in viz(p0):
            if (p0, q) in usados:
                continue
            cam = [p0, q]
            usados.add((p0, q)); usados.add((q, p0))
            ant, at = p0, q
            while grau.get(at) == 2:
                prox = [r for r in viz(at) if r != ant and (at, r) not in usados]
                if not prox:
                    break
                usados.add((at, prox[0])); usados.add((prox[0], at))
                ant, at = at, prox[0]
                cam.append(at)
            if len(cam) >= min_px:
                linhas.append(np.array(cam, dtype=np.float32))
    return linhas


def extrair(jpg, T, base):
    s, tx, ty = T[:3]
    verm, azul = mascaras(jpg)
    H, W = verm.shape
    verm_d = cv2.dilate(verm, np.ones((5, 5), np.uint8))

    def para_px(P):
        return (P[:, 0] - tx) / s * ESCALA_IMG, -(P[:, 1] - ty) / s * ESCALA_IMG

    # Vias: trechos de rua da base que passam sobre o vermelho
    x0w, y1w = tx, ty                      # canto superior esquerdo (pt 0,0)
    x1w, y0w = s * (W / ESCALA_IMG) + tx, -s * (H / ESCALA_IMG) + ty
    dentro = np.where((base.pts[:, 0] >= x0w) & (base.pts[:, 0] <= x1w) &
                      (base.pts[:, 1] >= y0w) & (base.pts[:, 1] <= y1w))[0]
    linhas = []
    coberto = np.zeros_like(verm)
    for seg in np.unique(base.dono[dentro]):
        P = amostrar(base.segs[seg][1], 2.0)
        px, py = para_px(P)
        ok = (px >= 0) & (px < W) & (py >= 0) & (py < H)
        on = np.zeros(len(P), bool)
        on[ok] = verm_d[py[ok].astype(int), px[ok].astype(int)] > 0
        # junta sequências contínuas sobre o vermelho (mín. 12 m)
        i = 0
        while i < len(P):
            if not on[i]:
                i += 1; continue
            j = i
            while j + 1 < len(P) and on[j + 1]:
                j += 1
            if (j - i) * 2.0 >= 12:
                trecho = P[i:j + 1]
                linhas.append(trecho)
                qx, qy = para_px(trecho)
                cv2.polylines(coberto, [np.column_stack([qx, qy]).astype(np.int32)], False, 1, thickness=26)
            i = j + 1
    # vermelho que não caiu em nenhuma rua da base (rua nova/fora da base): vetoriza o próprio traço
    sobra = verm & (1 - cv2.dilate(coberto, np.ones((9, 9), np.uint8)))
    sobra = cv2.morphologyEx(sobra, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    if sobra.sum() > 400:
        n, rot, est, _ = cv2.connectedComponentsWithStats(sobra, 8)
        for k in range(1, n):
            x, y, w, h, area = est[k]
            if area < 400 or max(w, h) < 50:
                continue
            esq = afinar(rot[y:y + h, x:x + w] == k)
            for L in tracar(esq):
                L = L + np.array([x, y], dtype=np.float32)
                Xw = L[:, 0] / ESCALA_IMG * s + tx
                Yw = -L[:, 1] / ESCALA_IMG * s + ty
                linhas.append(np.column_stack([Xw, Yw]))
                cv2.polylines(coberto, [L.astype(np.int32)], False, 1, thickness=26)
    cobertura = float((coberto & verm).sum() / max(1, verm.sum()))

    # Área: contorno da mancha azul (o vermelho por cima também conta)
    poligonos = []
    if azul.sum() > 2000:
        m = (azul > 0).astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((31, 31), np.uint8))
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
        cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in cs:
            if cv2.contourArea(c) < 20000:
                continue
            c = cv2.approxPolyDP(c, 3.0, True).reshape(-1, 2).astype(float)
            Xw = c[:, 0] / ESCALA_IMG * s + tx
            Yw = -c[:, 1] / ESCALA_IMG * s + ty
            poligonos.append(np.column_stack([Xw, Yw]))

    # simplifica as linhas (tolerância ~1,5 m)
    simples = []
    for L in linhas:
        c = cv2.approxPolyDP(L.astype(np.float32).reshape(-1, 1, 2), 1.5, False).reshape(-1, 2)
        simples.append(c)
    return simples, poligonos, cobertura


def zoom_para(larg_m, alt_m):
    # nível de zoom do Google que cabe a área numa tela de celular (~360x600 px)
    m_por_px = max(larg_m / 340, alt_m / 520, 1e-6)
    z = math.log2(156543.03 * math.cos(math.radians(LAT0)) / m_por_px)
    return int(max(13, min(18, math.floor(z))))


def kml(nome_doc, setores, op):
    linhas = ['<?xml version="1.0" encoding="UTF-8"?>',
              '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
              f"<name>{escape(nome_doc)}</name>",
              '<Style id="via"><LineStyle><color>ff2a2ae0</color><width>4</width></LineStyle></Style>',
              '<Style id="area"><LineStyle><color>ffb4783c</color><width>1.5</width></LineStyle>'
              '<PolyStyle><color>66e6b48c</color></PolyStyle></Style>']
    for st in setores:
        h = st["h"]
        desc = [f"{k.title()}: {v}" for k, v in h.items() if k not in ("PLANO",)]
        desc_txt = escape("\n".join(desc))
        nome = escape(("Setor " if op == "domiciliar" else "") + h["SETOR"])
        for poly in st["poligonos"]:
            coords = " ".join(f"{lon},{lat},0" for lat, lon in poly + poly[:1])
            linhas.append(f"<Placemark><name>{nome} (área)</name><description>{desc_txt}</description>"
                          f"<styleUrl>#area</styleUrl><Polygon><outerBoundaryIs><LinearRing><coordinates>{coords}"
                          f"</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>")
        if st["linhas"]:
            partes = "".join("<LineString><tessellate>1</tessellate><coordinates>" +
                             " ".join(f"{lon},{lat},0" for lat, lon in L) +
                             "</coordinates></LineString>" for L in st["linhas"])
            linhas.append(f"<Placemark><name>{nome}</name><description>{desc_txt}</description>"
                          f"<styleUrl>#via</styleUrl><MultiGeometry>{partes}</MultiGeometry></Placemark>")
    linhas.append("</Document></kml>")
    return "\n".join(linhas)


def salvar_kmz(caminho, nome_doc, setores, op):
    import zipfile
    with zipfile.ZipFile(caminho, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml(nome_doc, setores, op))


def exportar_mymaps():
    """Gera os KMZ para o Google My Maps a partir de dados.js + geo_setores.json."""
    import glob
    s = open(os.path.join(SITE, "dados.js"), encoding="utf-8").read()
    D = json.loads(s[len("window.DADOS = "):-2])
    geo = json.load(open(os.path.join(ROOT, "geo_setores.json"), encoding="utf-8"))
    os.makedirs(SAIDA, exist_ok=True)
    for velho in glob.glob(os.path.join(SAIDA, "*.km[lz]")) + glob.glob(os.path.join(SAIDA, "*", "*.km[lz]")):
        os.remove(velho)
    for op, _, nome_doc in OPS:
        if op not in D:
            continue
        setores = []
        for p in D[op]:
            g = geo.get(op, {}).get(p["h"]["SETOR"])
            if g and (g["linhas"] or g["poligonos"]):
                setores.append({"h": p["h"], "linhas": g["linhas"], "poligonos": g["poligonos"]})
        arq = "MyMaps_" + chave(nome_doc).replace(" ", "_").title() + ".kmz"
        salvar_kmz(os.path.join(SAIDA, arq), nome_doc, setores, op)
        print(arq, len(setores), "setores", os.path.getsize(os.path.join(SAIDA, arq)) // 1024, "KB")
        if op == "mutirao":
            pasta = os.path.join(SAIDA, "mutirao_por_area")
            os.makedirs(pasta, exist_ok=True)
            areas = sorted({st["h"].get("ÁREA") for st in setores}, key=lambda a: int(a))
            for a in areas:
                sub = [st for st in setores if st["h"].get("ÁREA") == a]
                salvar_kmz(os.path.join(pasta, f"Mutirao_Area_{int(a):02d}.kmz"), f"Mutirão — Área {a}", sub, op)


def main():
    s = open(os.path.join(SITE, "dados.js"), encoding="utf-8").read()
    D = json.loads(s[len("window.DADOS = "):-2])
    base = Base()
    os.makedirs(SAIDA, exist_ok=True)
    geo_todos = {}
    rel = []
    for op, arquivo, nome_doc in OPS:
        if op not in D:
            continue
        doc = fitz.open(os.path.join(ROOT, arquivo))
        paginas_mapa = [i for i in range(doc.page_count) if "VIA ATENDIDA" not in doc[i].get_text()]
        assert len(paginas_mapa) == len(D[op]), (op, len(paginas_mapa), len(D[op]))
        setores = []
        geo_todos[op] = {}
        for p, i in zip(D[op], paginas_mapa):
            setor = p["h"]["SETOR"]
            page = doc[i]
            assert setor in page.get_text(), (op, setor, i)
            T = georref(page, base)
            if not T:
                rel.append([op, setor, "", "", "", "", "não georreferenciado"])
                print(op, setor, "FALHOU", flush=True)
                continue
            if T[4] < 0.6:
                rel.append([op, setor, f"{T[4]:.0%}", f"{T[3]:.0%}", "", 0, "não georreferenciado (nomes não conferem)"])
                print(op, setor, f"nomes={T[4]:.0%} NAO CONFERE", flush=True)
                continue
            linhas, polys, cob = extrair(os.path.join(SITE, p["img"]), T, base)
            ll_linhas = [[latlon(x, y) for x, y in L] for L in linhas]
            ll_polys = [[latlon(x, y) for x, y in P] for P in polys]
            todos = [q for L in linhas for q in L] + [q for P in polys for q in P]
            if todos:
                A = np.array(todos)
                (xa, ya), (xb, yb) = A.min(0), A.max(0)
                centro = latlon((xa + xb) / 2, (ya + yb) / 2)
                z = zoom_para(xb - xa, yb - ya)
            else:
                centro, z = None, None
            st = {"h": p["h"], "linhas": ll_linhas, "poligonos": ll_polys}
            setores.append(st)
            geo_todos[op][setor] = {"c": centro, "z": z, "linhas": ll_linhas, "poligonos": ll_polys}
            p["geo"] = {"c": centro, "z": z} if centro else None
            situ = "ok" if T[4] >= 0.8 and cob >= 0.8 else "conferir"
            rel.append([op, setor, f"{T[4]:.0%}", f"{T[3]:.0%}", f"{cob:.0%}", len(ll_linhas), situ])
            print(op, setor, f"nomes={T[4]:.0%} encaixe={T[3]:.0%} vias_cobertas={cob:.0%}", situ, flush=True)

    with open(os.path.join(ROOT, "geo_setores.json"), "w", encoding="utf-8") as f:
        json.dump(geo_todos, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(SITE, "dados.js"), "w", encoding="utf-8") as f:
        f.write("window.DADOS = " + json.dumps(D, ensure_ascii=False, separators=(",", ":")) + ";\n")
    with open(os.path.join(SAIDA, "relatorio.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["operacao", "setor", "nomes_de_rua_conferem", "linhas_do_pdf_na_base", "vermelho_coberto", "qtd_linhas", "situacao"])
        w.writerows(rel)
    exportar_mymaps()
    print("OK")


if __name__ == "__main__":
    if "--so-kml" in sys.argv:
        exportar_mymaps()
    else:
        main()
