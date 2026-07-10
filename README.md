# SketchUp IFC Optimizer

Shrink and clean **IFC files exported from SketchUp** before importing them into
**Revit** (or any BIM tool). One drag-and-drop, one output file.

Real-world case: an underground parking level went from **1,156 MB → 192 MB**
(**83% smaller**) with the architecture, the structure and the referential
integrity of the file fully intact.

*Leia em [português](README.pt-BR.md).*

---

## Why SketchUp IFC exports are so big

A SketchUp export (faceted BREP, IFC2X3) has two problems:

1. **Entourage** — furniture, cars, vegetation and decoration are exported as
   hyper-detailed `IfcBuildingElementProxy` objects. They are useless in a Revit
   base model and they hold most of the mesh.
2. **Duplicated vertices** — every vertex of every face becomes a brand-new
   `IfcCartesianPoint`, with zero reuse. Pure bloat.

This tool attacks both, in a single pass.

---

## Usage

**Drag your `.ifc` file onto `processar_ifc.bat`** (Windows).

A single new file appears next to the original:

```
YOUR_FILE.ifc         (original, untouched)
YOUR_FILE_limpo.ifc   <-- import this one
```

> Requirement: **Python 3** on your PATH (`python --version` must work).
> No third-party packages — standard library only.

From a terminal (any OS):

```bash
python limpar_ifc.py "/path/to/YOUR_FILE.ifc"
```

> Note: the console output and the code comments are in **Brazilian Portuguese**,
> the author's language. The tool itself is language-agnostic.

---

## How it works

`limpar_ifc.py` reads the file in **three streaming passes** and writes it **once**:

| # | Pass | What it does |
|---|------|--------------|
| 1 | Analyze | Measures face count, bounding box and number of copies of every proxy; decides what counts as entourage. Read-only. |
| 2 | Map | Finds identical `IfcCartesianPoint`s (rounded to 6 decimals) that can be merged — **ignoring points that belong to entourage**, since those are about to disappear. |
| 3 | Write | Writes the cleaned file: entourage proxies and their geometry gone, relationships repaired (`IfcRelContainedInSpatialStructure` / `IfcRelDefinesByProperties`), vertices merged. Finally it checks that no `#id` reference was left dangling — and aborts with an error if one is. |

Vertex deduplication is **lossless**: same mesh, same face and object count.

### Why entourage before dedup?

In the **original** file each object occupies a contiguous, self-contained block of
IDs, so an entire object can be deleted safely. After deduplication points become
**shared** between objects, and deleting one object would tear geometry out of
another. Hence the fixed order: **entourage → dedup**. Both happen during the same
write, so the intermediate file never hits the disk.

### What is preserved

`IfcBeam`, `IfcRailing`, `IfcDoor`, building storeys, property sets, and the large
architectural proxies (ramps, walls, terrain).

### Entourage detection rule (deliberately conservative)

An object is removed when it is:

- **scattered garbage** — largest dimension ≥ `MAXDIM_LIXO` m. This is "exploded"
  SketchUp geometry: loose faces and vertices spread over hundreds of metres or
  kilometres. In Revit it shows up as a cloud of specks that swallows the scene.
  No real building element comes close to that size, so it is always safe to drop; or
- **repeated and small** — belongs to a family with ≥ `MIN_COPIAS` copies and its
  largest dimension is ≤ `MAXDIM_REPETIDO` m (fixture, car, plant, furniture); or
- **small and very dense** — ≥ `MIN_FACES_DENSO` faces and ≤ `MAXDIM_DENSO` m
  (hyper-detailed decoration); or
- **small** — largest dimension ≤ `MAXDIM_MANTER` m (loose furniture/objects).

The thresholds live at the top of `limpar_ifc.py` and are meant to be tuned:

```python
MIN_COPIAS      = 3     # repeated family with at least N copies
MAXDIM_REPETIDO = 4.5   # m  (small/medium repeated objects = entourage)
MIN_FACES_DENSO = 30000
MAXDIM_DENSO    = 2.5   # m  (small + very detailed = decoration)
MAXDIM_MANTER   = 3.0   # m  (proxies smaller than this are removed)
MAXDIM_LIXO     = 150.0 # m  (absurd bbox = scattered garbage, always removed)
TOL_DECIMAIS    = 6     # decimal places used to merge vertices (mm)
```

---

## Options

### Preview without writing

Prints the report (what would be removed and why, plus the biggest objects that
would survive) and **writes nothing**:

```bash
python limpar_ifc.py "YOUR_FILE.ifc" --preview
```

Use it whenever you suspect the automatic rule is about to delete something you
want to keep.

### Manual GUID list

To remove exactly the objects you picked instead of using the automatic rule, put
one GUID per line in a text file:

```bash
python limpar_ifc.py "YOUR_FILE.ifc" --guids guids.txt
```

(GUIDs are printed in both report tables — run with `--preview` first to collect them.)

---

## Files

| File | Role |
|------|------|
| `processar_ifc.bat` | Windows drag-and-drop entry point. |
| `limpar_ifc.py` | Everything: entourage removal + dedup + integrity check. |

**CPython 3, standard library only, fully streaming** — handles files well over 1 GB.

---

## Caveats

- Tuned for **SketchUp's IFC2X3 faceted-BREP export**, which writes one entity per
  line and keeps each object in a contiguous ID block. Exports from other tools may
  not satisfy those assumptions.
- The original file is never modified, but **always keep a backup** and open the
  result before trusting it. The integrity check proves no reference is dangling —
  it cannot prove the removal rule matched your intent. That is what `--preview` is for.

## License

[MIT](LICENSE) © Leonardo Marinho Côrtes
