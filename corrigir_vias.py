"""Corrige erros de digitação/extração nos nomes das vias.
Uso:  python corrigir_vias.py        -> corrige site/dados.js (idempotente)
Também é importado por gerar_site.py (função corrigir_via) para não perder as correções ao regerar."""
import json, re, sys, unicodedata

LIG = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl"}

TROCAS = {
    "Passagem São Marços": "Passagem São Marcos", "Passagem São Março": "Passagem São Marcos",
    "Rua Boa Esperanca": "Rua Boa Esperança", "Rua do Olaria": "Rua da Olaria",
    "Rua da Cosampa": "Rua da Cosanpa", "Rua Paulo Fontelles": "Rua Paulo Fonteles",
    "Rua Paulo Fonteles l": "Rua Paulo Fonteles", "Rua Roso Danin": "Rua Rosso Danin",
    "Rua Douque de Caxias": "Rua Duque de Caxias", "Passagem Oiliveira": "Passagem Oliveira",
    "Passagem Fereira": "Passagem Ferreira", "Passagem Virgillo de Mendonça": "Passagem Virgílio de Mendonça",
    "Passagem Vitório Régia": "Passagem Vitória Régia", "Passagem Angelin": "Passagem Angelim",
    "Passagem Feu Em Deus": "Passagem Fé em Deus", "Passagem Paul Darco": "Passagem Pau D’Arco",
    "Passagem José Miranda Caste Branco": "Passagem José Miranda Castelo Branco",
    "Passagem Waldemar Enrique": "Passagem Valdemar Henrique", "Passagem Sao Thiago": "Passagem São Tiago",
    "Passagem Sâo Luís": "Passagem São Luís", "Passagem Santa María": "Passagem Santa Maria",
    "Passagem São Luis": "Passagem São Luís", "Viela São Luis": "Viela São Luís",
}

def _norm(s):
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()

def corrigir_via(n):
    if not isinstance(n, str):
        return n
    for a, b in LIG.items():
        n = n.replace(a, b)
    n = n.replace("BiZencourt", "Bittencourt").replace("D'", "D’").rstrip("_ ").strip()
    for a, b in (("Passsagem ", "Passagem "), ("Passage ", "Passagem "), ("Massagem ", "Passagem "), ("Vía ", "Via "),
                 ("Primera ", "Primeira "), ("Quadro ", "Quadra "), ("Av. ", "Avenida "), ("Qd ", "Quadra "), ("CJ ", "Conjunto ")):
        if n.startswith(a):
            n = b + n[len(a):]
    n = re.sub(r"\bWe (\d)", r"WE \1", n)
    n = re.sub(r"\bSn (\d)", r"SN \1", n)
    n = re.sub(r"^(Quadra) e$", r"\1 E", n)
    n = re.sub(r"Fé Em Deus", "Fé em Deus", n)
    n = re.sub(r"^(Rua|Viela|Passagem|Travessa|Alameda|Avenida) sem nome", r"\1 Sem Nome", n)
    return TROCAS.get(n, n)

def _canon(nomes):
    """Entre variantes só de acento/caixa, usa a mais acentuada (e, no empate, a mais frequente)."""
    cont = {}
    for n in nomes:
        cont[n] = cont.get(n, 0) + 1
    grupos = {}
    for n in cont:
        grupos.setdefault(_norm(n), []).append(n)
    mapa = {}
    for vs in grupos.values():
        melhor = max(vs, key=lambda v: (sum(ord(c) > 127 for c in v), cont[v]))
        for v in vs:
            mapa[v] = melhor
    return mapa

def corrigir_dados(D):
    celulas = [c for op in D for p in D[op] for v in p["vias"] for c in v]
    for op in D:
        for p in D[op]:
            p["vias"] = [[corrigir_via(c) for c in v] for v in p["vias"]]
    mapa = _canon([c for op in D for p in D[op] for v in p["vias"] for c in v])
    n = 0
    for op in D:
        for p in D[op]:
            novo = [[mapa.get(c, c) for c in v] for v in p["vias"]]
            p["vias"] = novo
    depois = [c for op in D for p in D[op] for v in p["vias"] for c in v]
    return sum(a != b for a, b in zip(celulas, depois))

if __name__ == "__main__":
    arq = sys.argv[1] if len(sys.argv) > 1 else "site/dados.js"
    t = open(arq, encoding="utf-8").read()
    ini = t.index("{"); fim = t.rindex("}") + 1
    D = json.loads(t[ini:fim])
    print("células alteradas:", corrigir_dados(D))
    open(arq, "w", encoding="utf-8").write(t[:ini] + json.dumps(D, ensure_ascii=False, separators=(",", ":")) + t[fim:])
