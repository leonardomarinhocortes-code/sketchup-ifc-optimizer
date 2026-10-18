# SketchUp IFC Optimizer (Limpador IFC)

Reduz e limpa arquivos **IFC exportados do SketchUp** antes de importar no **Revit**.
Um arrastar-e-soltar, um arquivo de saída.

Caso real: um subsolo de garagem saiu de **1.156 MB → 192 MB** (**83% menor**),
mantendo intactas a arquitetura, a estrutura e a integridade referencial do arquivo.

*Read this in [English](README.en.md).*

---

## Por que o IFC do SketchUp fica gigante

Export do SketchUp (faceted BREP, IFC2X3) tem dois problemas:

1. **Entourage** — móveis, carros, vegetação e decoração saem como
   `IfcBuildingElementProxy` hiper-detalhados. Não servem numa base de Revit e
   concentram a maior parte das faces.
2. **Vértices duplicados** — cada vértice de cada face vira um `IfcCartesianPoint`
   novo, sem nenhum reaproveitamento. Infla o arquivo sem nenhum ganho.

Este script ataca os dois, numa passada só.

---

## Uso (arrastar e soltar)

**Arraste o seu arquivo `.ifc` sobre `processar_ifc.bat`.**

Ao lado do original aparece **um único arquivo novo**:

```
SEU_ARQUIVO.ifc         (original, intacto)
SEU_ARQUIVO_limpo.ifc   <-- ESTE vai para o Revit
```

No fim aparece um resumo `MB original → MB final (% de redução)`.

> Requisito: **Python 3** instalado e no PATH (`python --version` deve funcionar).
> Nenhuma dependência externa — só biblioteca padrão.

Pela linha de comando dá no mesmo:

```bat
processar_ifc.bat "C:\caminho\SEU_ARQUIVO.ifc"
python limpar_ifc.py "C:\caminho\SEU_ARQUIVO.ifc"
```

---

## O que ele faz (e por quê)

`limpar_ifc.py` lê o arquivo em **três passadas de streaming** e escreve **uma vez**:

| # | Passada | O que faz |
|---|---------|-----------|
| 1 | Analisar | Mede faces, bounding box e nº de cópias de cada proxy; decide o que é entourage. Só leitura. |
| 2 | Mapear | Encontra os `IfcCartesianPoint` idênticos (arredondados a 6 casas) que podem ser fundidos — **ignorando os pontos do entourage**, que vão sumir. |
| 3 | Escrever | Grava o `_limpo.ifc`: sem os proxies de entourage nem a geometria deles, com as relações corrigidas (`IfcRelContainedInSpatialStructure` / `IfcRelDefinesByProperties`) e com os vértices fundidos. Ao final, confere que nenhuma referência `#id` ficou órfã — se ficar, aborta com erro. |

O dedup é **lossless**: mesma malha, mesmo nº de faces e objetos.

### Por que entourage ANTES do dedup?

No arquivo **original**, cada objeto ocupa um bloco de IDs contíguos e
autossuficientes — dá para apagar um objeto inteiro com segurança. Depois do dedup
os pontos passam a ser **compartilhados** entre objetos, então apagar um objeto
poderia derrubar geometria de outro. Por isso a ordem é fixa: **entourage → dedup**.
As duas etapas acontecem na mesma escrita, então o intermediário nunca vai pro disco.

### O que é preservado

`IfcBeam` (vigas), `IfcRailing` (guarda-corpos), `IfcDoor` (portas), os pavimentos,
as propriedades e os proxies arquitetônicos grandes (rampa, paredes, terreno).

### Regra automática de entourage (conservadora)

Um objeto é removido se for:

- **lixo disperso** — maior dimensão ≥ `MAXDIM_LIXO` m. É geometria "explodida" do
  SketchUp (faces/vértices soltos espalhados por centenas de metros ou quilômetros).
  Aparece no Revit como uma "chuva"/nuvem de pontos que engole a cena e some com o
  prédio real. Nenhum elemento de um prédio chega perto disso, então é seguro; ou
- **repetido e pequeno** — faz parte de família com ≥ `MIN_COPIAS` cópias e maior
  dimensão ≤ `MAXDIM_REPETIDO` m (fixture/carro/planta/móvel); ou
- **pequeno e muito denso** — ≥ `MIN_FACES_DENSO` faces e ≤ `MAXDIM_DENSO` m
  (decoração/móvel hiper-detalhado); ou
- **pequeno** — maior dimensão ≤ `MAXDIM_MANTER` m (móvel/objeto avulso).

Esses parâmetros ficam no topo de `limpar_ifc.py` e podem ser ajustados:

```python
MIN_COPIAS      = 3     # família repetida com pelo menos N cópias
MAXDIM_REPETIDO = 4.5   # m  (repetidos pequenos/médios = entourage)
MIN_FACES_DENSO = 30000
MAXDIM_DENSO    = 2.5   # m  (objeto pequeno e muito detalhado = decoração)
MAXDIM_MANTER   = 3.0   # m  (proxies menores que isto são removidos)
MAXDIM_LIXO     = 150.0 # m  (bbox absurdo = geometria dispersa/lixo, removido sempre)
TOL_DECIMAIS    = 6     # casas decimais para fundir vértices (mm)
```

---

## Casos especiais

### Previsualizar sem escrever

Mostra o relatório (o que seria removido, e os maiores objetos que ficariam) e
**não gera arquivo nenhum**:

```bat
python limpar_ifc.py "SEU_ARQUIVO.ifc" --preview
```

Use quando desconfiar que o automático vai apagar algo que você quer manter.

### Lista manual de GUIDs

Para remover exatamente os objetos que você escolheu, em vez da regra automática,
crie um `guids.txt` com um GUID por linha e use:

```bat
python limpar_ifc.py "SEU_ARQUIVO.ifc" --guids guids.txt
```

(Os GUIDs aparecem nas duas tabelas do relatório — rode com `--preview` primeiro
para pegá-los.)

---

## Arquivos desta pasta

| Arquivo | Papel |
|---------|-------|
| `processar_ifc.bat` | Entrada arrastar-e-soltar (chama o `.py`). |
| `limpar_ifc.py` | Tudo: entourage + dedup + verificação de integridade. |

**CPython 3, só biblioteca padrão, em streaming** — aguenta arquivos de mais de 1 GB.

---

## IFC exportado do Revit: `simplificar_revit_ifc.py`

O `limpar_ifc.py` não serve para IFC do **Revit** (outra estrutura de arquivo). Quando
o peso vem de famílias de biblioteca super-detalhadas — típico das portas/janelas
**venezianas da Blocks**, em que cada palheta de 50 × 2 × 0,3 cm vem com ~3.400
triângulos — use:

```bash
python simplificar_revit_ifc.py "C:\caminho\ARQUIVO.ifc"
```

Gera `ARQUIVO_leve.ifc`. Ele acha as peças soltas que são **placas finas retangulares
e pesadas** (palhetas, ripas, lâminas: ≤ 2 cm de espessura, ≥ 500 triângulos, forma e
contorno de caixa) e troca cada uma pela sua caixa orientada exata (12 triângulos).
Nada mais muda: elementos, GUIDs, Psets, materiais, estilos, aberturas e pavimentos
ficam idênticos, e o script confere integridade e GUIDs no final.

Caso real: 314 MB → 48 MB; faces somando todas as instâncias (o que o Revit monta)
5,55 mi → 326 mil (−94%), sem diferença visível. Requer `ifcopenshell`, `numpy`, `scipy`.
`--preview` mostra o relatório sem gravar.

---

## Avisos

- Feito para o **export IFC2X3 faceted-BREP do SketchUp**, que grava uma entidade por
  linha e mantém cada objeto num bloco contíguo de IDs. Exports de outras ferramentas
  podem não satisfazer essas premissas.
- O original nunca é modificado, mas **sempre mantenha backup** e abra o resultado
  antes de confiar nele. A verificação de integridade prova que nenhuma referência
  ficou órfã — ela não prova que a regra de remoção acertou a sua intenção. É para
  isso que serve o `--preview`.

## Licença

[MIT](LICENSE) © Leonardo Marinho Côrtes
