# -*- coding: utf-8 -*-
"""
simplificar_revit_ifc.py
------------------------
Alivia IFCs exportados do REVIT cujo peso vem de familias de biblioteca
super-detalhadas (ex.: portas/janelas venezianas da Blocks, em que cada
palheta de 50 x 2 x 0,3 cm vem com ~3.400 triangulos).

O que faz:
  1. procura, dentro de cada geometria em malha (IfcFacetedBrep /
     IfcFaceBasedSurfaceModel / IfcShellBasedSurfaceModel), as pecas soltas
     (componentes conexos) que sao "placas finas" -- palhetas, ripas, laminas;
  2. troca cada uma pela sua caixa orientada exata (8 vertices, 6 faces),
     que na pratica e a mesma peca;
  3. apaga a malha antiga que ficou sem uso e confere a integridade (nenhuma
     referencia #id orfa) e que todos os elementos/GUIDs/propriedades continuam
     la.

NAO mexe em: elementos (IfcDoor, IfcWindow, paredes, lajes...), GUIDs,
pavimentos, aberturas, materiais, estilos/cores, Psets. So a malha das placas
finas muda. Pecas que nao sao placas finas (bacias, torneiras, quadros...)
ficam exatamente como estavam.

Uso:
    python simplificar_revit_ifc.py "C:\\caminho\\ARQUIVO.ifc"   -> ARQUIVO_leve.ifc
    python simplificar_revit_ifc.py "ARQUIVO.ifc" --preview      # so relatorio

Requer: ifcopenshell, numpy, scipy.
"""

import os
import re
import sys
import time
from collections import defaultdict

import numpy as np
import scipy.sparse as sp
import scipy.sparse.csgraph as cg
import ifcopenshell

# ----- criterio de "placa fina" (palheta/ripa/lamina) -----
MIN_FACES_ITEM = 1000   # so analisa geometrias com pelo menos N faces
MIN_TRI_PECA = 500      # peca com menos triangulos ja e leve, nem mexe (so troca
                        # o que e pesado: vidros/molduras de ~50 tri ficam como estao)
ESPESSURA_MAX = 0.02    # m   (placa fina: menor dimensao ate 2 cm)
ALONGAMENTO_MIN = 5.0   # maior dimensao >= 5x a espessura
AREA_MIN, AREA_MAX = 0.90, 1.05  # area da malha / area da caixa (forma de caixa)
DESVIO_MAX = 0.15       # 95% dos vertices a menos de 15% da espessura da face da caixa
SILHUETA_MIN = 0.98     # contorno visto de frente preenche >= 98% do retangulo

REF = re.compile(r'#(\d+)')
HEAD = re.compile(r'^#(\d+)\s*=\s*([A-Z0-9]+)\(')
APAGAVEIS = {"IFCFACE", "IFCFACEOUTERBOUND", "IFCFACEBOUND", "IFCPOLYLOOP",
             "IFCCARTESIANPOINT"}


def mb(caminho):
    try:
        return os.path.getsize(caminho) / 1024.0 / 1024.0
    except OSError:
        return 0.0


def cascas(it):
    if it.is_a('IfcFacetedBrep'):
        return [it.Outer]
    if it.is_a('IfcFaceBasedSurfaceModel'):
        return list(it.FbsmFaces)
    if it.is_a('IfcShellBasedSurfaceModel'):
        return list(it.SbsmBoundary)
    return []


def malha(it):
    """faces (lista), casca de cada face, P (vertices), T (triangulos), TF (face de cada tri)."""
    V, P, T, TF, faces, casca_da_face = {}, [], [], [], [], []
    for ci, s in enumerate(cascas(it)):
        for fc in s.CfsFaces:
            fi = len(faces)
            faces.append(fc)
            casca_da_face.append(ci)
            for b in fc.Bounds:
                idx = []
                for p in b.Bound.Polygon:
                    k = p.id()
                    if k not in V:
                        V[k] = len(P)
                        P.append(p.Coordinates)
                    idx.append(V[k])
                if not b.Orientation:
                    idx = idx[::-1]
                # contorno externo e furos entram na triangulacao em leque so p/
                # medir conectividade/area aproximada; furos sao raros (<0,01%)
                for i in range(1, len(idx) - 1):
                    T.append((idx[0], idx[i], idx[i + 1]))
                    TF.append(fi)
    return (faces, casca_da_face, np.array(P, float).reshape(-1, 3),
            np.array(T, int).reshape(-1, 3), np.array(TF, int))


def componentes(P, T):
    chave = np.round(P, 5)
    u, inv = np.unique(chave, axis=0, return_inverse=True)
    inv = inv.ravel()
    T2 = inv[T]
    n = len(u)
    r = np.concatenate([T2[:, 0], T2[:, 1]])
    c = np.concatenate([T2[:, 1], T2[:, 2]])
    g = sp.coo_matrix((np.ones(len(r)), (r, c)), shape=(n, n))
    nc, lab = cg.connected_components(g, directed=False)
    return nc, lab[T2[:, 0]]


def caixa_orientada(V):
    c = V.mean(0)
    w, E = np.linalg.eigh(np.cov((V - c).T))
    E = E[:, ::-1]  # colunas: eixo maior, medio, menor
    L = (V - c) @ E
    return c, E, L.min(0), L.max(0), L


def e_placa_fina(P, Ti):
    vs = P[np.unique(Ti)]
    if len(vs) < 8:
        return None
    c, E, lo, hi, L = caixa_orientada(vs)
    ext = hi - lo
    if not (ext[2] <= ESPESSURA_MAX and ext[0] >= ALONGAMENTO_MIN * max(ext[2], 1e-9)):
        return None
    area_box = 2 * (ext[0] * ext[1] + ext[1] * ext[2] + ext[0] * ext[2])
    a = P[Ti]
    area = 0.5 * np.linalg.norm(np.cross(a[:, 1] - a[:, 0], a[:, 2] - a[:, 0]), axis=1).sum()
    if not (AREA_MIN <= area / area_box <= AREA_MAX):
        return None
    d = np.minimum(L - lo, hi - L).min(1)
    if np.percentile(d, 95) > DESVIO_MAX * ext[2]:
        return None
    # contorno tem que ser retangular: a silhueta da peca vista de frente precisa
    # preencher o retangulo da caixa (senao um vidro em arco viraria retangulo)
    a2 = (a - c) @ E[:, :2]
    s = 0.5 * ((a2[:, 1, 0] - a2[:, 0, 0]) * (a2[:, 2, 1] - a2[:, 0, 1]) -
               (a2[:, 2, 0] - a2[:, 0, 0]) * (a2[:, 1, 1] - a2[:, 0, 1]))
    silhueta = max(s[s > 0].sum(), -s[s < 0].sum())
    if silhueta < SILHUETA_MIN * ext[0] * ext[1]:
        return None
    return c, E, lo, hi


# faces da caixa em indices de canto (bit0=x, bit1=y, bit2=z no frame local)
QUADS = [(0, 2, 6, 4), (1, 5, 7, 3), (0, 4, 5, 1), (2, 3, 7, 6), (0, 1, 3, 2), (4, 6, 7, 5)]


def criar_caixa(f, c, E, lo, hi):
    cantos = []
    for k in range(8):
        loc = np.array([hi[0] if k & 1 else lo[0],
                        hi[1] if k & 2 else lo[1],
                        hi[2] if k & 4 else lo[2]])
        cantos.append(c + E @ loc)
    pts = [f.createIfcCartesianPoint(tuple(float(x) for x in p)) for p in cantos]
    centro = np.mean(cantos, axis=0)
    faces = []
    for q in QUADS:
        q = list(q)
        a, b, d = cantos[q[0]], cantos[q[1]], cantos[q[2]]
        n = np.cross(b - a, d - a)
        meio = np.mean([cantos[i] for i in q], axis=0)
        if np.dot(n, meio - centro) < 0:  # garante normal para fora
            q = q[::-1]
        loop = f.createIfcPolyLoop([pts[i] for i in q])
        faces.append(f.createIfcFace([f.createIfcFaceOuterBound(loop, True)]))
    return faces


def simplificar(f, preview):
    itens = [it for tipo in ('IfcFacetedBrep', 'IfcFaceBasedSurfaceModel',
                             'IfcShellBasedSurfaceModel') for it in f.by_type(tipo)]
    candidatos = set()   # ids de entidades antigas que podem ter ficado orfas
    relatorio = []
    tri_antes = tri_depois = 0
    for it in itens:
        nfaces = sum(len(s.CfsFaces) for s in cascas(it))
        if nfaces < MIN_FACES_ITEM:
            continue
        faces, casca_da_face, P, T, TF = malha(it)
        if not len(T):
            continue
        nc, tl = componentes(P, T)
        cnt = np.bincount(tl, minlength=nc)
        trocar = {}
        for ci in np.nonzero(cnt >= MIN_TRI_PECA)[0]:
            box = e_placa_fina(P, T[tl == ci])
            if box is not None:
                trocar[ci] = box
        if not trocar:
            continue
        # componente de cada face (pelo 1o triangulo dela)
        comp_face = np.full(len(faces), -1)
        comp_face[TF[::-1]] = tl[::-1]
        tri_rem = int(sum(cnt[ci] for ci in trocar))
        tri_antes += tri_rem
        tri_depois += 12 * len(trocar)
        relatorio.append((it.id(), it.is_a(), nfaces, len(trocar), tri_rem))
        if preview:
            continue
        novas = defaultdict(list)       # casca -> faces novas
        casca_do_comp = {}
        for fi, fc in enumerate(faces):
            ci = comp_face[fi]
            if ci in trocar:
                casca_do_comp.setdefault(ci, casca_da_face[fi])
                candidatos.add(fc.id())
                for b in fc.Bounds:
                    candidatos.add(b.id())
                    candidatos.add(b.Bound.id())
                    candidatos.update(p.id() for p in b.Bound.Polygon)
            else:
                novas[casca_da_face[fi]].append(fc)
        for ci, box in trocar.items():
            novas[casca_do_comp[ci]].extend(criar_caixa(f, *box))
        for k, s in enumerate(cascas(it)):
            s.CfsFaces = novas[k]
    return relatorio, candidatos, tri_antes, tri_depois


def limpar_orfaos(tmp, saida, candidatos):
    """Passada em texto: remove so entidades candidatas que ninguem mais usa."""
    refs = {}
    with open(tmp, 'r', encoding='utf-8', errors='replace') as fin:
        for linha in fin:
            m = HEAD.match(linha)
            if m:
                eid = int(m.group(1))
                rhs = linha[linha.index('=') + 1:]
                refs[eid] = [int(r) for r in REF.findall(rhs)]
    vivos = set()
    pilha = [e for e in refs if e not in candidatos]
    vivos.update(pilha)
    while pilha:
        e = pilha.pop()
        for r in refs.get(e, ()):
            if r not in vivos:
                vivos.add(r)
                pilha.append(r)
    apagar = set(refs) - vivos
    definidos, referenciados = set(), set()
    with open(tmp, 'r', encoding='utf-8', errors='replace') as fin, \
            open(saida, 'w', encoding='utf-8', newline='') as fout:
        for linha in fin:
            m = HEAD.match(linha)
            if m:
                eid = int(m.group(1))
                if eid in apagar:
                    continue
                definidos.add(eid)
                referenciados.update(refs[eid])
            fout.write(linha)
    return len(apagar), referenciados - definidos


def resumo(caminho):
    g = ifcopenshell.open(caminho)
    prods = {p.GlobalId: p.is_a() for p in g.by_type('IfcRoot')}
    return prods


def main():
    args = sys.argv[1:]
    preview = '--preview' in args
    args = [a for a in args if a != '--preview']
    if not args:
        sys.exit('Uso: python simplificar_revit_ifc.py "caminho\\ARQUIVO.ifc"')
    original = os.path.abspath(args[0])
    if not os.path.isfile(original):
        sys.exit('[ERRO] Arquivo nao encontrado: %s' % original)
    base, ext = os.path.splitext(original)
    saida = base + '_leve' + ext
    tmp = base + '_leve.tmp'

    print('=' * 90)
    print('SIMPLIFICADOR IFC (Revit)')
    print('Arquivo: %s (%.1f MB)' % (original, mb(original)))
    print('=' * 90)
    t0 = time.time()
    print('\n[1/4] Abrindo...')
    f = ifcopenshell.open(original)
    guids_antes = {p.GlobalId: p.is_a() for p in f.by_type('IfcRoot')}
    print('      %d entidades com GUID' % len(guids_antes))

    print('\n[2/4] Procurando palhetas/ripas/laminas super-detalhadas...')
    rel, candidatos, ta, td = simplificar(f, preview)
    donos = defaultdict(list)
    for p in f.by_type('IfcProduct'):
        if p.Representation:
            for r in p.Representation.Representations:
                for it in r.Items:
                    alvo = it.MappingSource.MappedRepresentation.Items if it.is_a('IfcMappedItem') else [it]
                    for x in alvo:
                        donos[x.id()].append(p.Name)
    for iid, tipo, nf, npecas, tri in sorted(rel, key=lambda r: -r[4]):
        nomes = sorted(set(n.rsplit(':', 1)[0] for n in donos.get(iid, []) if n))
        print('      #%-8d %6d pecas -> caixa  (%8d tri a menos)  %s x%d' % (
            iid, npecas, tri - 12 * npecas, ', '.join(nomes)[:60], len(donos.get(iid, []))))
    print('      TOTAL: %d triangulos -> %d (no arquivo; no Revit multiplica por instancia)' % (ta, td))
    if preview:
        print('\n[PREVIEW] nada escrito.')
        return

    print('\n[3/4] Gravando e limpando malha sem uso...')
    f.write(tmp)
    del f
    napagar, orfas = limpar_orfaos(tmp, saida, candidatos)
    os.remove(tmp)
    print('      %d entidades antigas removidas' % napagar)
    if orfas:
        print('[FALHA] %d referencias orfas! ex: %s' % (len(orfas), sorted(orfas)[:10]))
        sys.exit(1)

    print('\n[4/4] Conferindo o arquivo final...')
    g = ifcopenshell.open(saida)
    guids_depois = {p.GlobalId: p.is_a() for p in g.by_type('IfcRoot')}
    if guids_depois != guids_antes:
        falt = set(guids_antes) - set(guids_depois)
        print('[FALHA] %d GUIDs diferentes (ex: %s)' % (len(falt), list(falt)[:5]))
        sys.exit(1)
    print('      integridade OK, %d GUIDs identicos ao original' % len(guids_depois))
    print('\n' + '=' * 90)
    print('Original : %.1f MB' % mb(original))
    print('Final    : %.1f MB  (%s)' % (mb(saida), os.path.basename(saida)))
    print('Reducao  : %.1f%%   tempo: %.0fs' % (100 * (1 - mb(saida) / mb(original)), time.time() - t0))
    print('=' * 90)


if __name__ == '__main__':
    main()
