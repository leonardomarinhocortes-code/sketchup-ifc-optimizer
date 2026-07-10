# -*- coding: utf-8 -*-
"""
limpar_ifc.py
-------------
Limpa um IFC exportado do SketchUp para importar no Revit, gerando UM unico
arquivo de saida: "<nome>_limpo.ifc".

Faz, numa so operacao:
  1. remove o entourage (moveis, vegetacao, carros, decoracao, lixo disperso) e
     toda a geometria desses objetos, corrigindo as relacoes
     (IfcRelContainedInSpatialStructure / IfcRelDefinesByProperties);
  2. deduplica os IfcCartesianPoint identicos (lossless: mesma malha);
  3. verifica que nao sobrou nenhuma referencia #id orfa.

O entourage e removido ANTES do dedup (no arquivo original cada objeto ocupa um
bloco de ids contiguos e autossuficientes; depois do dedup os pontos ficam
compartilhados e apagar um objeto derrubaria geometria de outro). As duas etapas
acontecem na mesma passada de escrita, entao o intermediario nunca vai pro disco.

PRESERVA: IfcBeam, IfcRailing, IfcDoor, pavimentos, propriedades e os proxies
arquitetonicos grandes (rampa, paredes, terreno).

Uso:
    python limpar_ifc.py "C:\\caminho\\ARQUIVO.ifc"      -> ARQUIVO_limpo.ifc
    python limpar_ifc.py "ARQUIVO.ifc" --preview          # so relatorio, nao escreve
    python limpar_ifc.py "ARQUIVO.ifc" --guids guids.txt  # lista manual de GUIDs

(normalmente voce nem chama isso direto: arrasta o .ifc sobre processar_ifc.bat)

100% CPython 3, so biblioteca padrao, em streaming: aguenta arquivos de +1 GB.
"""

import sys
import os
import re

# ----- parametros da regra automatica de entourage -----
MIN_COPIAS = 3         # familia repetida com pelo menos N copias
MAXDIM_REPETIDO = 4.5  # m  (repetidos pequenos/medios = entourage; pega o painel 4m x4)
MIN_FACES_DENSO = 30000
MAXDIM_DENSO = 2.5     # m  (objeto pequeno e muito detalhado = decoracao)
MAXDIM_MANTER = 3.0    # m  (proxies menores que isto sao removidos = moveis/objetos)
MAXDIM_LIXO = 150.0    # m  (bbox absurdo = geometria dispersa/"lixo" do SketchUp:
                       #     faces/vertices soltos espalhados por centenas de metros
                       #     ou quilometros. Nenhum elemento real de um predio chega
                       #     perto disso, entao e seguro remover.)

TOL_DECIMAIS = 6       # casas decimais p/ fundir vertices (mm). 6 = nanometro.

REF = re.compile(r'#(\d+)')
NUM = re.compile(r'[-+]?[\d.]+(?:[eE][-+]?\d+)?')
LISTA_REFS = re.compile(r'\(\s*#\d+(?:\s*,\s*#\d+)*\s*\)')
HEAD = re.compile(r'^#(\d+)\s*=\s*([A-Z0-9]+)\(')
PT_DEF = re.compile(r'^#(\d+)\s*=\s*IFCCARTESIANPOINT\(\((.*?)\)\)\s*;', re.IGNORECASE)

# tipos que sao geometria "interna" de um produto (deletaveis junto com o bloco)
GEOM = {
    "IFCCARTESIANPOINT", "IFCDIRECTION", "IFCLOCALPLACEMENT", "IFCAXIS2PLACEMENT3D",
    "IFCPRODUCTDEFINITIONSHAPE", "IFCSHAPEREPRESENTATION", "IFCFACETEDBREP",
    "IFCCLOSEDSHELL", "IFCFACE", "IFCFACEOUTERBOUND", "IFCFACEBOUND", "IFCPOLYLOOP",
    "IFCSTYLEDITEM", "IFCPRESENTATIONSTYLEASSIGNMENT", "IFCSURFACESTYLE",
    "IFCSURFACESTYLESHADING", "IFCSURFACESTYLERENDERING", "IFCCOLOURRGB",
}

REL_EDIT = ("IFCRELCONTAINEDINSPATIALSTRUCTURE", "IFCRELDEFINESBYPROPERTIES")


def mb(caminho):
    """Tamanho do arquivo em MB (0.0 se nao existir)."""
    try:
        return os.path.getsize(caminho) / 1024.0 / 1024.0
    except OSError:
        return 0.0


# ---------------------------------------------------------------- passada 1
def analisar(caminho):
    """Estrutura, faces por proxy, bbox por proxy, repeticoes. So leitura."""
    proxy_repr, proxy_guid = {}, {}
    proddef_reps, shaperep_breps, brep_shell, shell_faces = {}, {}, {}, {}

    bbox = {}
    cur_proxy = None            # id do proxy do bloco atual (ou None se nao-proxy)
    primeiro_pt_apos_proxy = {}  # proxy_id -> id do 1o cartesianpoint (= placement)

    with open(caminho, "r", encoding="utf-8", errors="replace") as f:
        for linha in f:
            m = HEAD.match(linha)
            if not m:
                continue
            eid = int(m.group(1))
            tipo = m.group(2)
            corpo = linha[m.end() - 1:]  # a partir do '('

            if tipo == "IFCBUILDINGELEMENTPROXY":
                cur_proxy = eid
                refs = REF.findall(corpo)
                g = re.search(r"'([^']*)'", linha)
                proxy_guid[eid] = g.group(1) if g else ""
                if len(refs) >= 3:
                    proxy_repr[eid] = int(refs[2])
                bbox[eid] = [1e30, 1e30, 1e30, -1e30, -1e30, -1e30]
            elif tipo not in GEOM:
                cur_proxy = None  # outro produto/estrutura quebra o bloco de proxy

            if tipo == "IFCPRODUCTDEFINITIONSHAPE":
                proddef_reps[eid] = [int(r) for r in REF.findall(corpo)]
            elif tipo == "IFCSHAPEREPRESENTATION":
                refs = [int(r) for r in REF.findall(corpo)]
                shaperep_breps[eid] = refs[1:] if refs else []
            elif tipo == "IFCFACETEDBREP":
                refs = REF.findall(corpo)
                if refs:
                    brep_shell[eid] = int(refs[0])
            elif tipo == "IFCCLOSEDSHELL":
                shell_faces[eid] = len(REF.findall(corpo))
            elif tipo == "IFCCARTESIANPOINT" and cur_proxy is not None:
                if cur_proxy not in primeiro_pt_apos_proxy:
                    # 1o ponto do bloco = ponto de placement (origem mundo) -> ignora
                    primeiro_pt_apos_proxy[cur_proxy] = eid
                    continue
                nums = NUM.findall(corpo)
                if len(nums) >= 3:
                    x, y, z = float(nums[0]), float(nums[1]), float(nums[2])
                    b = bbox[cur_proxy]
                    if x < b[0]: b[0] = x
                    if y < b[1]: b[1] = y
                    if z < b[2]: b[2] = z
                    if x > b[3]: b[3] = x
                    if y > b[4]: b[4] = y
                    if z > b[5]: b[5] = z

    # faces por proxy
    faces = {}
    for pid, rid in proxy_repr.items():
        f = 0
        for srep in proddef_reps.get(rid, []):
            for brep in shaperep_breps.get(srep, []):
                sh = brep_shell.get(brep)
                if sh is not None:
                    f += shell_faces.get(sh, 0)
        faces[pid] = f

    # dimensoes (m)
    dims = {}
    for pid, b in bbox.items():
        if b[3] < b[0]:
            dims[pid] = (0.0, 0.0, 0.0)
        else:
            dims[pid] = ((b[3]-b[0])/1000.0, (b[4]-b[1])/1000.0, (b[5]-b[2])/1000.0)

    # grupos por nº de faces (componentes repetidos)
    grupos = {}
    for pid, f in faces.items():
        grupos.setdefault(f, []).append(pid)
    copias = {pid: len(grupos[faces[pid]]) for pid in faces}

    return proxy_guid, faces, dims, copias


def escolher_auto(proxy_guid, faces, dims, copias):
    """Aplica a regra automatica. Retorna (set ids remover, lista detalhada)."""
    remover = set()
    detalhe = []
    for pid, f in faces.items():
        dx, dy, dz = dims[pid]
        maxd = max(dx, dy, dz)
        n = copias[pid]
        motivo = None
        # (z) bbox absurdo = geometria dispersa/lixo (faces soltas a centenas de metros)
        if maxd >= MAXDIM_LIXO:
            motivo = "bbox absurdo (%.0fm) - geometria dispersa/lixo" % maxd
        # (a) repetido pequeno (maxd==0 = nao medido, tratamos como pequeno se repetido)
        elif n >= MIN_COPIAS and (maxd <= MAXDIM_REPETIDO):
            motivo = "repetido x%d, pequeno (%.1fm)" % (n, maxd)
        # (b) denso pequeno
        elif f >= MIN_FACES_DENSO and maxd <= MAXDIM_DENSO:
            motivo = "denso (%d faces) e pequeno (%.1fm)" % (f, maxd)
        # (c) qualquer proxy pequeno (movel/objeto) abaixo do limite arquitetonico
        elif maxd <= MAXDIM_MANTER:
            motivo = "pequeno (%.1fm) - movel/objeto" % maxd
        if motivo:
            remover.add(pid)
            detalhe.append((pid, proxy_guid[pid], f, n, (dx, dy, dz), motivo))
    detalhe.sort(key=lambda r: r[2], reverse=True)
    return remover, detalhe


# ------------------------------------------------- streaming "sem entourage"
def iter_mantidas(caminho, remover):
    """
    Percorre o IFC e devolve (linha, tipo) apenas das linhas que sobrevivem a
    remocao do entourage -- isto e, o arquivo intermediario, so que sem tocar o
    disco. Um proxy alvo leva junto todo o bloco de geometria que o segue.
    """
    cur_proxy_deletado = False
    with open(caminho, "r", encoding="utf-8", errors="replace") as f:
        for linha in f:
            m = HEAD.match(linha)
            if not m:
                yield linha, None
                continue
            tipo = m.group(2)

            if tipo == "IFCBUILDINGELEMENTPROXY":
                cur_proxy_deletado = int(m.group(1)) in remover
                if cur_proxy_deletado:
                    continue
            elif tipo not in GEOM:
                cur_proxy_deletado = False
            elif cur_proxy_deletado:
                continue  # geometria de um proxy deletado
            yield linha, tipo


def chave_coord(corpo):
    nums = NUM.findall(corpo)
    if len(nums) < 3:
        return None
    return "%.*f,%.*f,%.*f" % (
        TOL_DECIMAIS, float(nums[0]),
        TOL_DECIMAIS, float(nums[1]),
        TOL_DECIMAIS, float(nums[2]),
    )


# ---------------------------------------------------------------- passada 2
def mapear_pontos(caminho, remover):
    """
    Mapeia IfcCartesianPoint duplicados -> id canonico, considerando apenas as
    linhas que sobrevivem ao entourage (senao o canonico poderia ser um ponto
    que vai ser deletado, deixando referencia orfa).
    """
    coord_canon = {}   # chave_coord -> id canonico
    remap = {}         # id duplicado -> id canonico
    total_pts = 0
    for linha, tipo in iter_mantidas(caminho, remover):
        if tipo != "IFCCARTESIANPOINT":
            continue
        m = PT_DEF.match(linha)
        if not m:
            continue
        total_pts += 1
        chave = chave_coord(m.group(2))
        if chave is None:
            continue
        eid = int(m.group(1))
        canon = coord_canon.get(chave)
        if canon is None:
            coord_canon[chave] = eid   # primeiro = canonico
        else:
            remap[eid] = canon         # duplicado -> aponta p/ canonico
    return remap, total_pts, len(coord_canon)


# ---------------------------------------------------------------- passada 3
def filtra_lista(linha, remover):
    """Remove ids deletados da 1a lista (#..,#..) da linha (relacao)."""
    mm = LISTA_REFS.search(linha)
    if not mm:
        return linha, 0
    ids = [int(x) for x in REF.findall(mm.group(0))]
    keep = [i for i in ids if i not in remover]
    if len(keep) == len(ids):
        return linha, 0
    if not keep:
        return None, len(ids)  # lista vazia -> dropar a relacao
    nova = "(" + ", ".join("#%d" % i for i in keep) + ")"
    return linha[:mm.start()] + nova + linha[mm.end():], len(ids) - len(keep)


def escrever(caminho, saida, remover, remap):
    """
    Escreve o IFC final: sem os proxies de entourage, com as relacoes corrigidas
    e com os pontos duplicados fundidos. Coleta os ids definidos e referenciados
    para a verificacao de integridade.
    """
    def sub_ref(mo):
        c = remap.get(int(mo.group(1)))
        return ("#%d" % c) if c is not None else mo.group(0)

    definidos = set()
    referenciados = set()
    pts_descartados = 0
    rels_editadas = 0
    rels_dropadas = 0

    with open(saida, "w", encoding="utf-8", newline="") as fout:
        for linha, tipo in iter_mantidas(caminho, remover):
            if tipo is None:
                fout.write(linha)  # cabecalho STEP, DATA, ENDSEC etc.
                continue

            if tipo in REL_EDIT:
                nova, removidos = filtra_lista(linha, remover)
                if nova is None:
                    rels_dropadas += 1
                    continue
                if removidos:
                    rels_editadas += 1
                linha = nova

            eq = linha.find("=")
            eid = int(linha[1:eq].strip())
            if eid in remap:
                pts_descartados += 1  # definicao de ponto duplicado -> descartar
                continue

            rhs = REF.sub(sub_ref, linha[eq + 1:])
            fout.write(linha[:eq + 1] + rhs)
            definidos.add(eid)
            referenciados.update(int(r) for r in REF.findall(rhs))

    return definidos, referenciados, pts_descartados, rels_editadas, rels_dropadas


def main():
    args = sys.argv[1:]
    preview = "--preview" in args
    args = [a for a in args if a != "--preview"]
    guids_file = None
    if "--guids" in args:
        i = args.index("--guids")
        guids_file = args[i + 1]
        del args[i:i + 2]

    if not args:
        sys.exit('Uso: python limpar_ifc.py "caminho\\ARQUIVO.ifc"')
    original = os.path.abspath(args[0])
    if not os.path.isfile(original):
        sys.exit("[ERRO] Arquivo nao encontrado: %s" % original)
    if not original.lower().endswith(".ifc"):
        sys.exit("[ERRO] O arquivo nao e .ifc: %s" % original)

    base, ext = os.path.splitext(original)
    if base.lower().endswith("_limpo"):
        print("[AVISO] '%s' ja parece ser um arquivo limpo; a saida virara '%s_limpo%s'."
              % (os.path.basename(original), os.path.basename(base), ext))
    saida = base + "_limpo" + ext
    tam_in = mb(original)

    print("=" * 94)
    print("LIMPADOR IFC")
    print("Arquivo: %s (%.1f MB)" % (original, tam_in))
    print("=" * 94)

    # ---- passada 1: analisar e decidir o que remover
    print("\n[1/3] Analisando objetos...")
    proxy_guid, faces, dims, copias = analisar(original)
    total_faces = sum(faces.values()) or 1
    print("      proxies: %d | faces totais: %d" % (len(faces), total_faces))

    if guids_file:
        with open(guids_file, "r", encoding="utf-8") as gf:
            alvo_guids = set(l.strip() for l in gf if l.strip())
        remover = set(pid for pid, g in proxy_guid.items() if g in alvo_guids)
        detalhe = sorted(
            [(pid, proxy_guid[pid], faces[pid], copias[pid], dims[pid], "manual")
             for pid in remover], key=lambda r: r[2], reverse=True)
    else:
        remover, detalhe = escolher_auto(proxy_guid, faces, dims, copias)

    faces_rem = sum(faces[pid] for pid in remover)
    print("")
    print("=" * 94)
    print("SERAO REMOVIDOS: %d objetos | %d faces (%.1f%% da malha)" % (
        len(remover), faces_rem, 100.0 * faces_rem / total_faces))
    print("=" * 94)
    print("%-22s %10s %6s  %-20s  %s" % ("GUID", "FACES", "copias", "TAM (m)", "motivo"))
    print("-" * 94)
    for pid, g, f, n, (dx, dy, dz), motivo in detalhe[:40]:
        print("%-22s %10d %6d  %-20s  %s" % (g, f, n, "%.1fx%.1fx%.1f" % (dx, dy, dz), motivo))

    mantidos_grandes = sorted(
        [(faces[pid], proxy_guid[pid], dims[pid]) for pid in faces if pid not in remover],
        reverse=True)[:8]
    print("-" * 94)
    print("MAIORES OBJETOS MANTIDOS (confira se algum e entourage que voce queira tirar):")
    for f, g, (dx, dy, dz) in mantidos_grandes:
        print("   %-22s %10d faces  %.1fx%.1fx%.1f m  (%.1f%%)" % (
            g, f, dx, dy, dz, 100.0 * f / total_faces))
    print("-" * 94)

    if preview:
        print("\n[PREVIEW] nenhum arquivo escrito. Rode sem --preview para gerar.")
        return

    # ---- passada 2: mapear vertices duplicados (ja sem o entourage)
    print("\n[2/3] Mapeando vertices duplicados...")
    remap, total_pts, pts_unicos = mapear_pontos(original, remover)
    print("      pontos: %d | unicos: %d | duplicados: %d (%.1f%%)" % (
        total_pts, pts_unicos, len(remap),
        (100.0 * len(remap) / total_pts) if total_pts else 0.0))

    # ---- passada 3: escrever o arquivo final
    print("\n[3/3] Escrevendo %s ..." % os.path.basename(saida))
    definidos, referenciados, pts_desc, rels_ed, rels_dp = escrever(
        original, saida, remover, remap)
    print("      pontos removidos: %d | relacoes editadas: %d | dropadas: %d" % (
        pts_desc, rels_ed, rels_dp))

    # ---- integridade referencial
    orfas = referenciados - definidos
    if orfas:
        print("\n[FALHA] %d referencias orfas! Exemplos: %s" % (
            len(orfas), ", ".join("#%d" % o for o in sorted(orfas)[:10])))
        sys.exit(1)
    print("      integridade OK: %d ids definidos, nenhuma referencia orfa." % len(definidos))

    tam_out = mb(saida)
    print("\n" + "=" * 94)
    print("CONCLUIDO")
    print("Original : %s (%.1f MB)" % (os.path.basename(original), tam_in))
    print("Final    : %s (%.1f MB)" % (os.path.basename(saida), tam_out))
    if tam_in:
        print("Reducao  : %.1f%%" % (100.0 * (tam_in - tam_out) / tam_in))
    print("=" * 94)
    print("Importe no Revit: %s" % saida)


if __name__ == "__main__":
    main()
