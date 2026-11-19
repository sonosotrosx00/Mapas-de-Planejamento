"""Gera os dados (site/dados.js) e os mapas (site/mapas/) a partir dos PDFs.

Uso:  python gerar_site.py            (dados + mapas; pula imagens que já existem)
      python gerar_site.py --data     (só os dados)
      python gerar_site.py --refazer  (recria todas as imagens)
Depois rode:  python gerar_geo.py     (geometria para o My Maps)

Para cada setor são gerados:
  mapas/<op>/<setor>.jpg    mapa em alta resolução (3368 px) — ampliar e baixar
  mapas/<op>/<setor>-p.jpg  prévia leve (1400 px) — mostrada na página do setor
O PDF do setor é montado no próprio aparelho (mapa + página com as vias).

Requer: pip install pymupdf pillow
"""
import hashlib, json, os, re, sys
import pymupdf as fitz
from PIL import Image

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "site")
ONLY_DATA = "--data" in sys.argv
REFAZER = "--refazer" in sys.argv

OPS = [
    ("domiciliar", "Mapa Coleta - Operação.pdf", {"<": "ti", "=": "tt", "[": "tí", "]": "tí"}),
    ("mutirao", "Mapa Mutirão - Operação.pdf", {"2": "ti", "=": "tt", "[": "tí", "]": "tí"}),
    # Quando o PDF da Varrição estiver pronto, descomente a linha abaixo e rode o script de novo:
    # ("varricao", "Mapa Varrição - Operação.pdf", {"<": "ti", "=": "tt", "[": "tí", "]": "tí"}),
]

# Correções de texto do cabeçalho (o PDF vem sem acento / com erro)
CORRIGE_CAMPO = [
    (r"\bIMPAR\b", "ÍMPAR"),
    (r"\bDIARIA\b", "DIÁRIA"),
    (r"\bOPERACIONAL DO SERVIÇOS\b", "OPERACIONAL DOS SERVIÇOS"),
]

# Nomes que vêm cortados na planilha que gerou o PDF (o resto do texto não existe no PDF).
# Completados a partir das outras colunas da mesma linha. Um nome cortado é trocado pelo
# nome completo que começa igual; se nenhum servir, ganha "…" no fim para indicar o corte.
NOMES_COMPLETOS = [
    "Rua Sem Nome (Entre Passagem Coronel Moisés e Passagem Ana Deusa)",
    "Rua Sem Nome (entre R. Quadra A Morada de Deus I x Pass. Piraiba de Assunção)",
    "Rua Sem Nome (Entre Rua Professor Nelson Ribeiro e Travessa Coronel Luíz Bentes)",
    "Rua Sem Nome (Entre Passagem Praina e R Sem Nome (Entre R Prof Nelson Ribeiro e Tv Coronel Luíz Bentes))",
    "Rua Sem Nome 1 - Praia Grande (entre a Av. Franklin de Menezes x R. Aristides Nobrega)",
    "Rua Sem Nome 2 - Praia Grande (entre a Av. Franklin de Menezes x R. Aristides Nobrega)",
    "Rua Sem Nome 3 - Praia Grande (entre a Av. Franklin de Menezes x R. Aristides Nobrega)",
    "Rua Sem Nome (Entre Canal Agua Cristal e Passagem Elcione Barbalho)",
    "Rua Sem Nome 1 (acesso pela R. Cel. Juvêncio Sarmento, alt. do n° 175)",
    "Rua Sem Nome 2 (acesso pela R. Cel. Juvêncio Sarmento, alt. do n° 175)",
    "Rua Sem Nome (Acesso pela Avenida Engenheiro Fernando Guilhon)",
    "Rua Sem Nome (Entre Passagem Cameta e Rua Professor Nelson Ribeiro)",
    "Rua Sem Nome (Entre Passagem Mena Barreto e Rua Sem Nome 2 (Acesso pela Passagem Mena Barreto))",
    "Rua Sem Nome (Entre R Sem N (Acs pela Est Acampamento) e R Sem N 2 (Acs pela Pass Mena Barreto))",
    "Rua Sem Nome 1 (Entre Alameda Dinan Coelho e Alameda Coronel Ernesto Coelho)",
    "Rua Sem Nome 2 (Entre Alameda Dinan Coelho e Alameda Coronel Ernesto Coelho)",
]


def completa(cel):
    if cel.count("(") == cel.count(")"):
        return cel
    c = [n for n in NOMES_COMPLETOS if n.startswith(cel)]
    if len(c) == 1:
        return c[0]
    return cel.rstrip() + "…"


def fix_line(spans, lig):
    out = ""
    for i, sp in enumerate(spans):
        t = sp["text"]
        if i > 0 and t and t[0] in lig:
            t = lig[t[0]] + t[1:]
        out += t
    return re.sub(r"\s+", " ", out).strip()


def lines(page, lig):
    res = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            if abs(l["dir"][0] - 1) > 0.01:
                continue
            s = fix_line(l["spans"], lig)
            if s:
                res.append((l["bbox"], s))
    return res


def header(page, lig):
    ls = lines(page, lig)
    labels = sorted((bb[0], s) for bb, s in ls if 565 < bb[1] < 575)
    vals = [(bb[0], s) for bb, s in ls if bb[1] > 578]
    h = {}
    for x, s in sorted(vals):
        cands = [lab for lx, lab in labels if lx <= x + 10]
        if not cands:
            continue
        lab = cands[-1]
        s = re.sub(r"^(EQUIPE|SEMANA):\s*", "", s)
        h[lab] = (h.get(lab, "") + " " + s).strip()
    for k in h:
        for a, b in CORRIGE_CAMPO:
            h[k] = re.sub(a, b, h[k])
    return h


def _ch(t):
    import unicodedata
    t = unicodedata.normalize("NFD", t).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def bairros(page):
    """Reserva para setor sem geometria: nomes de bairro impressos no mapa (texto grande, cinza claro).
    A fonte do PDF troca "ti" por ":" ("Fá:ma" = Fátima); só ficam nomes que existem na lista do IBGE."""
    oficiais = {}
    for nome, _ in BAIRROS_POL:
        oficiais[_ch(nome)] = nome
        m = re.search(r"\((.+)\)", nome)            # "Montese (Terra Firme)" também vale como "Terra Firme"
        if m:
            oficiais[_ch(m.group(1))] = nome
    linhas = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            sp = [s for s in l["spans"] if s["size"] > 20 and s["color"] == 0xB4B4B4 and s["bbox"][1] < 560]
            if sp:
                t = "".join(s["text"] for s in sp).replace(":", "ti").strip()
                linhas.append((l["bbox"][1], l["bbox"][0], t))
    linhas.sort()
    achados, usados = set(), set()
    for i, (y, x, t) in enumerate(linhas):
        if i in usados:
            continue
        if _ch(t) in oficiais:
            achados.add(oficiais[_ch(t)]); continue
        for j in range(i + 1, len(linhas)):         # nome quebrado em duas linhas
            y2, x2, t2 = linhas[j]
            if j not in usados and 0 < y2 - y < 40 and abs(x2 - x) < 80 and _ch(t + " " + t2) in oficiais:
                achados.add(oficiais[_ch(t + " " + t2)]); usados.add(j); break
    return sorted(achados)


INICIO_COL = {0: 58.5, 1: 286.0, 2: 513.5}


def table(page, lig):
    """Linhas da tabela de vias. Letras que 'vazam' da célula para a coluna vizinha
    (texto contínuo, sem espaço) continuam na célula de origem."""
    rows = {}
    for b in page.get_text("rawdict")["blocks"]:
        for l in b.get("lines", []):
            if abs(l["dir"][0] - 1) > 0.01 or l["bbox"][1] < 25:
                continue
            y = round(l["bbox"][1])
            key = next((k for k in rows if abs(k - y) <= 2), y)
            r = rows.setdefault(key, ["", "", ""])
            col_ant, x1_ant = None, None
            for si, sp in enumerate(l["spans"]):
                for ci, ch in enumerate(sp["chars"]):
                    c = ch["c"]
                    if si > 0 and ci == 0 and c in lig:
                        c = lig[c]
                    x0, x1 = ch["bbox"][0], ch["bbox"][2]
                    col = 0 if x0 < 283 else (1 if x0 < 510 else 2)
                    # cada célula começa exatamente no início da coluna (x = 286,0 / 513,5);
                    # letra fora desse ponto, colada na anterior, é continuação que "vazou"
                    if col_ant is not None and col > col_ant and abs(x0 - INICIO_COL[col]) > 0.3:
                        col = col_ant
                    r[col] += c
                    col_ant, x1_ant = col, x1
    out = []
    for k in sorted(rows):
        r = [re.sub(r"\s+", " ", x.replace("=", "tt").replace("ﬁ", "fi").replace("ﬂ", "fl")).strip() for x in rows[k]]
        r = [completa(x) for x in r]
        if any(r):
            out.append(r)
    return out


def salvar_jpg(pix_img, caminho, largura=None, qualidade=85):
    im = pix_img
    if largura:
        alt = round(im.height * largura / im.width)
        im = im.resize((largura, alt), Image.LANCZOS)
    im.save(caminho, "JPEG", quality=qualidade, optimize=True, progressive=True)


# Limites dos bairros (IBGE, Censo 2022) — cache_cbl/bairros_belem.js, do mapadeplanejamento.vercel.app
BAIRROS_POL = []
_arq_b = os.path.join(ROOT, "cache_cbl", "bairros_belem.js")
if os.path.exists(_arq_b):
    _t = open(_arq_b, encoding="utf-8").read()
    for _f in json.loads(_t[_t.index("{"):_t.rindex("}") + 1])["features"]:
        _geo = _f["geometry"]
        _polys = [_geo["coordinates"]] if _geo["type"] == "Polygon" else _geo["coordinates"]
        BAIRROS_POL.append((_f["properties"]["bairro"], [[[(y, x) for x, y in anel] for anel in pol] for pol in _polys]))


def _dentro(pt, anel):
    y, x = pt
    d = False
    j = len(anel) - 1
    for i in range(len(anel)):
        yi, xi = anel[i]; yj, xj = anel[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            d = not d
        j = i
    return d


def bairro_do_ponto(pt):
    for nome, polys in BAIRROS_POL:
        for pol in polys:
            if _dentro(pt, pol[0]) and not any(_dentro(pt, b) for b in pol[1:]):
                return nome
    return None


def bairros_pela_geometria(geo):
    """Bairros onde passam as vias atendidas (pelo menos 10% da extensão ou 300 m)."""
    if not BAIRROS_POL or not geo:
        return None
    ext = {}
    for L in geo.get("linhas", []):
        for a, b in zip(L[:-1], L[1:]):
            m = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            dist = ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 * 111000
            nome = bairro_do_ponto(m)
            if nome:
                ext[nome] = ext.get(nome, 0) + dist
    if not ext:
        for P in geo.get("poligonos", []):
            for pt in P:
                nome = bairro_do_ponto(pt)
                if nome:
                    ext[nome] = ext.get(nome, 0) + 1
    total = sum(ext.values())
    if not total:
        return None
    return sorted(n for n, v in ext.items() if v / total >= 0.10 or (geo.get("linhas") and v >= 300))


# geometria (centro/zoom para o My Maps), se já foi gerada pelo gerar_geo.py
GEO = {}
if os.path.exists(os.path.join(ROOT, "geo_setores.json")):
    GEO = json.load(open(os.path.join(ROOT, "geo_setores.json"), encoding="utf-8"))

data = {}
for op, fname, lig in OPS:
    d = fitz.open(os.path.join(ROOT, fname))
    os.makedirs(os.path.join(OUT, "mapas", op), exist_ok=True)
    plans = []
    i = 0
    while i < d.page_count:
        p = d[i]
        h = header(p, lig)
        assert "SETOR" in h, (fname, i, h)
        vias = []
        j = i + 1
        while j < d.page_count and "VIA ATENDIDA" in d[j].get_text():
            vias += table(d[j], lig)
            j += 1
        setor = h["SETOR"]
        slug = re.sub(r"[^A-Za-z0-9]+", "-", setor).strip("-")
        img = f"mapas/{op}/{slug}.jpg"
        prev = f"mapas/{op}/{slug}-p.jpg"
        if not ONLY_DATA:
            precisa = REFAZER or not os.path.exists(os.path.join(OUT, img)) or not os.path.exists(os.path.join(OUT, prev))
            if precisa:
                pix = p.get_pixmap(matrix=fitz.Matrix(4, 4))
                im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                if REFAZER or not os.path.exists(os.path.join(OUT, img)):
                    salvar_jpg(im, os.path.join(OUT, img))
                salvar_jpg(im, os.path.join(OUT, prev), largura=1400, qualidade=80)
        md5 = hashlib.md5()
        for arq in (img, prev):
            if os.path.exists(os.path.join(OUT, arq)):
                md5.update(open(os.path.join(OUT, arq), "rb").read())
        g = GEO.get(op, {}).get(setor)
        # bairros: pela geometria do setor (limites do IBGE); sem geometria, pelos nomes impressos no mapa
        bai = bairros_pela_geometria(g) or bairros(p)
        plano = {"h": h, "bairros": bai, "vias": vias, "img": img, "prev": prev, "v": md5.hexdigest()[:8]}
        if g and g.get("c"):
            plano["geo"] = {"c": g["c"], "z": g["z"]}
        plans.append(plano)
        print(op, i, setor, len(vias), flush=True)
        i = j
    data[op] = plans

# geometria de cada setor para o mapa interativo do site (carregada só quando usada)
if GEO:
    os.makedirs(os.path.join(OUT, "geo"), exist_ok=True)
    for op, setores in GEO.items():
        arr = lambda L: [[round(a, 5), round(b, 5)] for a, b in L]
        out = {k: {"l": [arr(L) for L in v["linhas"]], "p": [arr(P) for P in v["poligonos"]]} for k, v in setores.items()}
        with open(os.path.join(OUT, "geo", op + ".json"), "w", encoding="utf-8") as f:
            json.dump(out, f, separators=(",", ":"))

with open(os.path.join(OUT, "dados.js"), "w", encoding="utf-8") as f:
    f.write("window.DADOS = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n")
print("OK")
