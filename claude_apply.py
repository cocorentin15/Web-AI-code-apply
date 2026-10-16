#!/usr/bin/env python3
"""
apply_claude.py

Applique automatiquement une réponse de Claude qui suit l'un des deux modes
du prompt "RÈGLES DE MODIFICATION".

MODE SCRIPT (fichier complet) :

    === FILE: chemin/vers/fichier ===
    contenu complet...
    === END FILE ===

MODE PETITE IA (patch compact) :

    FILE: chemin/vers/fichier
    FIND
    [bloc exact]
    REPLACE
    [nouveau bloc]

  ou, pour plusieurs modifications dans un même fichier :

    FILE: chemin/vers/fichier
    PATCH 1
    FIND
    ...
    REPLACE
    ...
    PATCH 2
    FIND
    ...
    REPLACE
    ...

Les deux modes peuvent être mélangés dans un même texte.

Règles d'application des patches
--------------------------------
- Le bloc FIND doit exister EXACTEMENT UNE FOIS dans le fichier.
  Si l'exact échoue, on retente en ignorant les espaces en fin de ligne.
  0 occurrence ou plusieurs occurrences => erreur, rien n'est écrit
  pour ce fichier (tout ou rien par fichier).
- Les patches d'un fichier sont appliqués dans l'ordre, en mémoire ;
  le fichier n'est écrit que si TOUS réussissent.
- REPLACE vide = suppression du bloc (la ligne est supprimée aussi).
- Les fins de ligne CRLF d'un fichier existant sont préservées.
- Les chemins doivent rester dans le dossier racine (--root, par défaut
  le dossier courant) : pas de chemin absolu ni de "..".

Tolérances
----------
- "=== FILE path ===" sans deux-points, casse libre, 2 "=" ou plus.
- "=== END FILE ===" optionnel : le bloc s'arrête au prochain en-tête
  ou à la fin du texte.
- Fences markdown (``` ou ~~~, avec ou sans langage / espace) autour du
  texte entier, autour d'un fichier ou d'un bloc FIND / REPLACE : retirées.
- Les mots-clés du mode patch (FILE:, FIND, REPLACE, PATCH n) doivent être
  en MAJUSCULES et en début de ligne, pour ne pas être confondus avec du
  contenu.

Usage
-----
    python apply_claude.py reponse.txt
    python apply_claude.py reponse.txt --dry-run     # n'écrit rien
    python apply_claude.py reponse.txt --root mon_projet
    cat reponse.txt | python apply_claude.py -       # lit stdin

Code de sortie : 0 si tout est appliqué, 1 s'il y a eu au moins une erreur.
Dépendances : uniquement la bibliothèque standard Python.
"""

import argparse
import re
import sys
from pathlib import Path


# --- Expressions régulières -------------------------------------------------

# === FILE: chemin ===   (mode script)
START_PATTERN = re.compile(
    r"^\s*={2,}\s*FILE\s*:?\s*(?P<path>.+?)\s*={2,}\s*$", re.IGNORECASE
)
# === END FILE ===
END_PATTERN = re.compile(r"^\s*={2,}\s*END\s+FILE\s*={2,}\s*$", re.IGNORECASE)

# FILE: chemin   (mode patch) - tolère **FILE: x** ou `FILE: x`
PATCH_FILE_PATTERN = re.compile(r"^\s*[*`]*FILE\s*:\s*(?P<path>.+?)[*`\s]*$")
FIND_PATTERN = re.compile(r"^FIND\s*:?\s*$")
REPLACE_PATTERN = re.compile(r"^REPLACE\s*:?\s*$")
PATCHNUM_PATTERN = re.compile(r"^PATCH\s+\d+\s*:?\s*$")

# Fences markdown
FENCE_OPEN = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})\s*[\w+#.\-]*\s*$")
FENCE_CLOSE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})\s*$")


def _clean_path(raw: str) -> str:
    return raw.strip().strip("`*\"' ")


# --- Nettoyage des blocs ----------------------------------------------------

def _count_fences(lines):
    return sum(1 for ln in lines if FENCE_OPEN.match(ln))


def clean_block(lines):
    """
    Retire les lignes vides aux extrémités puis une éventuelle fence :
    - fence ouvrante + fermante qui englobent tout le bloc : retirées ;
    - fence fermante orpheline en fin (nombre de fences impair, pas
      d'ouverture au début) : retirée.
    Les fences au milieu du contenu ne sont jamais touchées.
    """
    lines = list(lines)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    if len(lines) >= 2:
        o = FENCE_OPEN.match(lines[0])
        c = FENCE_CLOSE.match(lines[-1])
        if o and c and o.group("fence")[0] == c.group("fence")[0] \
                and len(c.group("fence")) >= len(o.group("fence")):
            lines = lines[1:-1]
        elif c and not o and _count_fences(lines) % 2 == 1:
            lines = lines[:-1]
            while lines and not lines[-1].strip():
                lines.pop()
    elif len(lines) == 1 and FENCE_CLOSE.match(lines[0]):
        lines = []
    return lines


def _strip_global_fence(text: str) -> str:
    """Retire une fence qui enveloppe TOUT le texte (exactement 2 fences)."""
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) >= 2 and _count_fences(lines) == 2:
        if FENCE_OPEN.match(lines[0]) and FENCE_CLOSE.match(lines[-1]):
            return "\n".join(lines[1:-1])
    return "\n".join(lines)


def _is_header(line: str) -> bool:
    return bool(START_PATTERN.match(line) or PATCH_FILE_PATTERN.match(line))


# --- Parsing ----------------------------------------------------------------

def parse(text: str):
    """
    Retourne (ops, errors).
    ops : liste de ("full", chemin, contenu) ou ("patch", chemin, [(find, repl), ...]).
    """
    ops, errors = [], []
    lines = _strip_global_fence(text).splitlines()

    state = "idle"          # idle | full | after_file | find | replace
    path = None
    buf = []
    find_buf = None
    patches = []

    def joined(block):
        return "\n".join(clean_block(block))

    def finish():
        nonlocal state, path, buf, find_buf, patches
        if state == "full":
            ops.append(("full", path, joined(buf)))
        elif state == "replace":
            patches.append((joined(find_buf), joined(buf)))
        elif state == "find":
            errors.append(f"{path} : bloc FIND sans REPLACE (ignoré).")
        if state in ("after_file", "find", "replace"):
            if patches:
                ops.append(("patch", path, patches))
            else:
                errors.append(f"{path} : aucun patch exploitable.")
        state, path, buf, find_buf, patches = "idle", None, [], None, []

    def prev_nonblank(i):
        j = i - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        return lines[j] if j >= 0 else ""

    def next_nonblank(i):
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        return lines[j] if j < len(lines) else ""

    for i, line in enumerate(lines):
        # Fence "d'ouverture" collée devant un en-tête suivant : on l'ignore.
        if state != "idle" and FENCE_CLOSE.match(line) and _is_header(next_nonblank(i)):
            if FENCE_CLOSE.match(prev_nonblank(i)) or _count_fences(buf) % 2 == 0:
                continue

        # Mode script : à l'intérieur d'un bloc, seuls START / END comptent.
        if state == "full":
            if END_PATTERN.match(line):
                finish()
            elif START_PATTERN.match(line):
                finish()
                m = START_PATTERN.match(line)
                state, path, buf = "full", _clean_path(m.group("path")), []
            else:
                buf.append(line)
            continue

        # Autres états : en-têtes reconnus partout.
        m = START_PATTERN.match(line)
        if m:
            finish()
            state, path, buf = "full", _clean_path(m.group("path")), []
            continue
        m = PATCH_FILE_PATTERN.match(line)
        if m:
            finish()
            state, path, buf, patches = "after_file", _clean_path(m.group("path")), [], []
            continue
        if END_PATTERN.match(line):
            finish()
            continue

        if state == "after_file":
            if FIND_PATTERN.match(line):
                state, buf = "find", []
            # PATCH n, fences, lignes vides, prose : ignorés
        elif state == "find":
            if REPLACE_PATTERN.match(line):
                find_buf, buf = buf, []
                state = "replace"
            else:
                buf.append(line)
        elif state == "replace":
            if PATCHNUM_PATTERN.match(line):
                patches.append((joined(find_buf), joined(buf)))
                find_buf, buf = None, []
                state = "after_file"
            elif FIND_PATTERN.match(line):
                patches.append((joined(find_buf), joined(buf)))
                find_buf, buf = None, []
                state = "find"
            else:
                buf.append(line)
        # idle : ignoré

    finish()
    return ops, errors


# --- Système de fichiers (avec cache mémoire pour --dry-run) ----------------

class Workspace:
    def __init__(self, root: Path, dry_run: bool):
        self.root = root.resolve()
        self.dry_run = dry_run
        self.cache = {}
        self.crlf = {}

    def resolve(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(f"chemin hors du dossier racine : {rel}")
        return p

    def exists(self, p: Path) -> bool:
        return p in self.cache or p.is_file()

    def read(self, p: Path) -> str:
        if p in self.cache:
            return self.cache[p]
        raw = p.read_bytes().decode("utf-8")
        self.crlf[p] = "\r\n" in raw
        text = raw.replace("\r\n", "\n")
        self.cache[p] = text
        return text

    def write(self, p: Path, text: str):
        self.cache[p] = text
        if self.dry_run:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        out = text.replace("\n", "\r\n") if self.crlf.get(p) else text
        p.write_bytes(out.encode("utf-8"))


# --- Application d'un patch -------------------------------------------------

def apply_one(text: str, find: str, repl: str) -> str:
    """Remplace `find` (unique) par `repl`. Lève ValueError sinon."""
    if not find.strip():
        raise ValueError("bloc FIND vide")

    n = text.count(find)
    if n == 1:
        if repl == "" and text.count(find + "\n") == 1:
            return text.replace(find + "\n", "", 1)
        return text.replace(find, repl, 1)
    if n > 1:
        raise ValueError(f"bloc FIND ambigu ({n} occurrences)")

    # Repli : comparaison ligne à ligne en ignorant les espaces de fin de ligne.
    tl = text.split("\n")
    fl = find.split("\n")
    hits = [
        i for i in range(len(tl) - len(fl) + 1)
        if all(tl[i + j].rstrip() == fl[j].rstrip() for j in range(len(fl)))
    ]
    if len(hits) == 1:
        i = hits[0]
        new = repl.split("\n") if repl != "" else []
        return "\n".join(tl[:i] + new + tl[i + len(fl):])
    if len(hits) > 1:
        raise ValueError(f"bloc FIND ambigu ({len(hits)} occurrences, espaces ignorés)")
    raise ValueError("bloc FIND introuvable")


# --- Programme principal ----------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Applique les blocs FILE / FIND-REPLACE d'une réponse de Claude.")
    ap.add_argument("source", nargs="?", default="-", help="fichier texte à lire ('-' ou absent = stdin)")
    ap.add_argument("--root", default=".", help="dossier racine du projet (défaut : dossier courant)")
    ap.add_argument("--dry-run", action="store_true", help="simule sans rien écrire")
    args = ap.parse_args()

    if args.source == "-":
        if sys.stdin.isatty():
            ap.error("aucune source : donne un fichier ou envoie le texte sur stdin")
        source_text = sys.stdin.read()
    else:
        try:
            source_text = Path(args.source).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Erreur : impossible de lire '{args.source}' : {exc}", file=sys.stderr)
            sys.exit(1)

    if not source_text.strip():
        print("Erreur : la source est vide.", file=sys.stderr)
        sys.exit(1)

    ops, errors = parse(source_text)
    if not ops and not errors:
        print("Aucun bloc '=== FILE: ... ===' ni 'FILE: ... / FIND / REPLACE' détecté.", file=sys.stderr)
        sys.exit(1)

    ws = Workspace(Path(args.root), args.dry_run)
    done = []

    for kind, rel, payload in ops:
        if not rel:
            errors.append("Bloc ignoré : chemin de fichier vide.")
            continue
        try:
            p = ws.resolve(rel)

            if kind == "full":
                existed = ws.exists(p)
                if existed and p not in ws.cache:
                    ws.read(p)  # pour retenir le style de fin de ligne
                content = payload if payload.endswith("\n") else payload + "\n"
                ws.write(p, content)
                done.append(f"{'remplacé' if existed else 'créé'} : {rel}")

            else:  # patch
                if not ws.exists(p):
                    raise ValueError("fichier introuvable (un patch exige un fichier existant)")
                text = ws.read(p)
                failed = False
                for k, (find, repl) in enumerate(payload, 1):
                    try:
                        text = apply_one(text, find, repl)
                    except ValueError as exc:
                        errors.append(f"{rel} (patch {k}/{len(payload)}) : {exc}.")
                        failed = True
                if failed:
                    errors.append(f"{rel} : ce bloc de patches n'a PAS été appliqué (tout ou rien).")
                else:
                    ws.write(p, text)
                    done.append(f"patché ({len(payload)}) : {rel}")

        except (OSError, ValueError, UnicodeDecodeError) as exc:
            errors.append(f"{rel} : {exc}")

    print("[DRY-RUN] aucun fichier écrit.\n" if args.dry_run else "", end="")
    print("Résultat :")
    for d in done:
        print(f"  - {d}")
    if errors:
        print("\nErreurs :")
        for e in errors:
            print(f"  - {e}")
    print(f"\nTotal : {len(done)} fichier(s) traité(s), {len(errors)} erreur(s).")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()