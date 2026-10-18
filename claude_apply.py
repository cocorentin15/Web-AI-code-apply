#!/usr/bin/env python3
"""
claude_apply.py

Applique automatiquement une réponse de Claude qui suit l'un des deux modes
du prompt de règles de sortie (voir PROMPT_CLAUDE.md) : fichiers complets
(MODE SCRIPT), patches FIND / REPLACE (MODE PETITE IA) et suppressions.

Dans les exemples ci-dessous, le "|" en début de ligne sert uniquement à la
lisibilité et à ne laisser dans ce fichier aucune ligne d'en-tête (le parseur
reconnaît un en-tête même indenté, y compris dans un fichier complet). Il ne
fait PAS partie de la syntaxe.

MODE SCRIPT (fichier complet) :

    | === FILE: chemin/vers/fichier ===
    | contenu complet...
    | === END FILE ===

MODE PETITE IA (patch compact) :

    | FILE: chemin/vers/fichier
    | FIND
    | [bloc exact]
    | REPLACE
    | [nouveau bloc]

  ou, pour plusieurs modifications dans un même fichier :

    | FILE: chemin/vers/fichier
    | PATCH 1
    | FIND
    | ...
    | REPLACE
    | ...
    | PATCH 2
    | FIND
    | ...
    | REPLACE
    | ...

SUPPRESSION (fichiers uniquement) :

    | === DELETE FILE: chemin/vers/fichier ===

  ou, forme tolérée, une ligne seule, en MAJUSCULES, en début de ligne
  (reconnue uniquement hors d'un bloc de fichier complet) :

    | DELETE FILE: chemin/vers/fichier

Les trois formes peuvent être mélangées dans un même texte. Les opérations
s'exécutent dans l'ordre du texte : supprimer puis recréer le même chemin
dans la même réponse fonctionne.

Règles d'application
--------------------
- Patch : le bloc FIND doit exister EXACTEMENT UNE FOIS dans le fichier.
  Si l'exact échoue, on retente en ignorant les espaces en fin de ligne.
  0 occurrence ou plusieurs occurrences => erreur, rien n'est écrit
  pour ce fichier (tout ou rien par fichier).
- Les patches d'un fichier sont appliqués dans l'ordre, en mémoire ;
  le fichier n'est écrit que si TOUS réussissent.
- REPLACE vide = suppression du bloc (la ligne est supprimée aussi).
- Les fins de ligne CRLF d'un fichier existant sont préservées.
- Suppression : fichier existant uniquement. Un dossier est refusé et les
  dossiers parents ne sont jamais supprimés. Fichier inexistant = erreur.
- Les chemins doivent rester dans le dossier racine (--root, par défaut
  le dossier courant) : pas de chemin absolu ni de "..".

Tolérances
----------
- Bannières : sans deux-points, casse libre, 2 "=" ou plus, indentation.
- La ligne de fin de fichier est optionnelle : le bloc s'arrête au prochain
  en-tête ou à la fin du texte (donc tout texte qui suit est absorbé).
- Fences markdown (trois ` ou trois ~, avec ou sans langage / espace)
  autour du texte entier, d'un fichier ou d'un bloc FIND / REPLACE : retirées.
- Les mots-clés du mode patch (FILE:, FIND, REPLACE, PATCH n) doivent être
  en MAJUSCULES et en début de ligne, pour ne pas être confondus avec du
  contenu.

Limite connue : pas d'échappement. Une ligne qui ressemble à un en-tête est
traitée comme tel même au milieu du contenu d'un fichier.

Usage
-----
    python claude_apply.py reponse.txt
    python claude_apply.py reponse.txt --dry-run     # n'écrit rien
    python claude_apply.py reponse.txt --root mon_projet
    cat reponse.txt | python claude_apply.py -       # lit stdin
    python claude_apply.py --copy-prompt             # copie PROMPT_CLAUDE.md

Code de sortie : 0 si tout est appliqué, 1 s'il y a eu au moins une erreur.
Dépendances : uniquement la bibliothèque standard Python.
"""

import argparse
import platform
import re
import subprocess
import sys
from pathlib import Path


# --- Expressions régulières -------------------------------------------------
# Les en-têtes tolèrent l'indentation (\s* en début) : une réponse copiée
# depuis un chat arrive souvent décalée. Conséquence : une ligne d'en-tête est
# reconnue même indentée, y compris à l'intérieur d'un fichier complet.

# Bannière de fichier complet (mode script) : ":" optionnel, casse libre,
# 2 signes "=" ou plus de chaque côté.
START_PATTERN = re.compile(
    r"^\s*={2,}\s*FILE\s*:?\s*(?P<path>.+?)\s*={2,}\s*$", re.IGNORECASE
)
# Fin de fichier complet.
END_PATTERN = re.compile(r"^\s*={2,}\s*END\s+FILE\s*={2,}\s*$", re.IGNORECASE)

# Suppression : bannière (mêmes tolérances que START_PATTERN) ou forme
# tolérée "DELETE FILE: chemin" (MAJUSCULES, début de ligne strict). La forme
# tolérée n'est pas reconnue dans un fichier complet, où elle pourrait être du
# contenu (voir _delete_target).
DELETE_BANNER_PATTERN = re.compile(
    r"^\s*={2,}\s*DELETE\s+FILE\s*:?\s*(?P<path>.+?)\s*={2,}\s*$", re.IGNORECASE
)
DELETE_PLAIN_PATTERN = re.compile(r"^DELETE FILE\s*:\s*(?P<path>.+?)[*`\s]*$")

# Mode patch : "FILE: chemin" - tolère **FILE: x** ou `FILE: x`
PATCH_FILE_PATTERN = re.compile(r"^\s*[*`]*FILE\s*:\s*(?P<path>.+?)[*`\s]*$")
FIND_PATTERN = re.compile(r"^FIND\s*:?\s*$")
REPLACE_PATTERN = re.compile(r"^REPLACE\s*:?\s*$")
PATCHNUM_PATTERN = re.compile(r"^PATCH\s+\d+\s*:?\s*$")

# Fences markdown
FENCE_OPEN = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})\s*[\w+#.\-]*\s*$")
FENCE_CLOSE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})\s*$")


def _clean_path(raw: str) -> str:
    return raw.strip().strip("`*\"' ")


def _delete_target(line: str, allow_plain: bool):
    """
    Chemin visé si `line` est une suppression, sinon None (chemin vide possible
    si l'en-tête est dégénéré : l'appelant le signale en erreur).
    `allow_plain` autorise la forme "DELETE FILE: chemin" : False dans un
    fichier complet, où cette ligne peut légitimement faire partie du contenu.
    """
    m = DELETE_BANNER_PATTERN.match(line)
    if not m and allow_plain:
        m = DELETE_PLAIN_PATTERN.match(line)
    if not m:
        return None
    # strip("=") : une bannière sans chemin donne sinon le "chemin" "=".
    return _clean_path(m.group("path")).strip("= ")


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
    return bool(
        START_PATTERN.match(line)
        or PATCH_FILE_PATTERN.match(line)
        or DELETE_BANNER_PATTERN.match(line)
    )


# --- Parsing ----------------------------------------------------------------

def parse(text: str):
    """
    Retourne (ops, errors).
    ops, dans l'ordre du texte :
      ("full", chemin, contenu)
      ("patch", chemin, [(find, repl), ...])
      ("delete", chemin, None)

    Parseur à états, une seule passe ligne par ligne :
      idle        hors de tout bloc : la prose est ignorée
      full        contenu d'un fichier complet : seuls un en-tête de fichier,
                  la fin de fichier ou une bannière de suppression comptent
                  (FIND, REPLACE, etc. y sont du contenu)
      after_file  après l'en-tête de patch, en attente du premier FIND
      find        lecture du bloc FIND
      replace     lecture du bloc REPLACE
    Hors de "full", tous les en-têtes sont reconnus à tout moment et
    terminent le bloc en cours (finish).

    Piège : sans fin explicite, le texte qui suit un bloc complet ou un
    REPLACE (prose comprise) est absorbé dans son contenu. D'où la règle
    "rien après le dernier bloc" du prompt.
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
        """Clôt le bloc en cours (émet son op ou une erreur) et revient à idle."""
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
        # Une fence sans langage juste avant un en-tête est l'ouverture
        # (parasite) du bloc suivant, pas la fermeture du bloc en cours.
        if state != "idle" and FENCE_CLOSE.match(line) and _is_header(next_nonblank(i)):
            if FENCE_CLOSE.match(prev_nonblank(i)) or _count_fences(buf) % 2 == 0:
                continue

        # Fichier complet : seuls fin, nouvel en-tête et bannière de
        # suppression comptent (la forme DELETE nue serait du contenu).
        if state == "full":
            m = START_PATTERN.match(line)
            target = _delete_target(line, allow_plain=False)
            if END_PATTERN.match(line):
                finish()
            elif m:
                finish()
                state, path, buf = "full", _clean_path(m.group("path")), []
            elif target is not None:
                finish()
                ops.append(("delete", target, None))
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
        target = _delete_target(line, allow_plain=True)
        if target is not None:
            finish()
            ops.append(("delete", target, None))
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


# --- Système de fichiers (avec cache mémoire) -------------------------------

_DELETED = object()  # marqueur de suppression dans Workspace.cache


class Workspace:
    """
    Accès disque + cache mémoire, indexé par chemin résolu.

    Le cache sert à deux choses :
    - les opérations successives sur un même fichier voient le résultat des
      précédentes (patches enchaînés, suppression puis recréation, ...) ;
    - en --dry-run il est la seule "écriture" : rien ne touche le disque, mais
      exists() et read() reflètent l'état simulé.
    Valeur en cache : texte du fichier (fins de ligne normalisées en LF) ou
    _DELETED. `crlf` retient les fichiers lus en CRLF pour les réécrire ainsi.
    """

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

    def was_deleted(self, p: Path) -> bool:
        return self.cache.get(p) is _DELETED

    def exists(self, p: Path) -> bool:
        if p in self.cache:
            return self.cache[p] is not _DELETED
        return p.is_file()

    def read(self, p: Path) -> str:
        if p in self.cache:
            if self.cache[p] is _DELETED:
                raise FileNotFoundError(f"fichier supprimé : {p}")
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

    def delete(self, p: Path):
        """Supprime un fichier (jamais un dossier, jamais les dossiers parents)."""
        if p not in self.cache and p.is_dir():
            raise ValueError("c'est un dossier (la suppression ne concerne que les fichiers)")
        if not self.exists(p):
            raise ValueError("fichier introuvable (rien à supprimer)")
        if not self.dry_run:
            p.unlink()
        self.cache[p] = _DELETED
        # Un fichier recréé ensuite est un fichier neuf : fins de ligne LF.
        self.crlf.pop(p, None)


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

    # Repli sans espaces de fin de ligne : un bloc recopié depuis un chat ou
    # un éditeur perd (ou gagne) souvent ces espaces invisibles. On n'accepte
    # le résultat que s'il reste unique.
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


# --- Presse-papiers ---------------------------------------------------------

PROMPT_FILE = "PROMPT_CLAUDE.md"


def _copy_windows(text: str) -> bool:
    """
    Écrit dans le presse-papiers Windows via l'API Win32 en ctypes.

    Pourquoi ne pas lancer "clip" ? Parce qu'un sous-processus passe par la
    console, dont l'encodage est une plaie (CP850, CP1252, UTF-8...). En
    appelant directement SetClipboardData, on dépose la chaîne UTF-16 telle
    quelle, sans dépendre de la page de codes de la console.
    """
    import ctypes
    from ctypes import wintypes

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # Sur Windows 64 bits, un HANDLE/pointeur ne tient pas dans un int : sans
    # restype explicite, ctypes tronque la valeur et le memmove écrit n'importe
    # où. On déclare donc les signatures précises utilisées.
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE

    # Chaîne UTF-16 terminée par un NUL (le format attendu par le presse-papiers).
    data = (text + "\0").encode("utf-16-le")

    if not user32.OpenClipboard(0):
        return False
    try:
        if not user32.EmptyClipboard():
            return False
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        p = kernel32.GlobalLock(h)
        if not p:
            kernel32.GlobalFree(h)
            return False
        try:
            ctypes.memmove(p, data, len(data))
        finally:
            kernel32.GlobalUnlock(h)
        # Après SetClipboardData, la mémoire appartient au presse-papiers :
        # il ne faut PAS la libérer nous-mêmes.
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
            return False
        return True
    finally:
        user32.CloseClipboard()


def _copy_command(cmd, text: str):
    """Lance un utilitaire externe (pbcopy, xclip, xsel, wl-copy) qui lit stdin."""
    subprocess.run(cmd, input=text.encode("utf-8"), check=True)


def write_clipboard(text: str) -> bool:
    """
    Écrit `text` dans le presse-papiers selon la plateforme, avec plusieurs
    replis en cascade. Retourne True si une méthode a réussi.
    """
    system = platform.system()

    if system == "Windows":
        try:
            if _copy_windows(text):
                return True
        except OSError:
            # Échec de l'API Win32 : on tentera pyperclip en repli.
            pass
    elif system == "Darwin":
        try:
            _copy_command(["pbcopy"], text)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass
    else:
        # Linux et autres Unix : Wayland d'abord, puis X11 (xclip, xsel).
        for cmd in (
            ["wl-copy"],
            ["xclip", "-selection", "clipboard", "-i"],
            ["xsel", "--clipboard", "--input"],
        ):
            try:
                _copy_command(cmd, text)
                return True
            except (FileNotFoundError, subprocess.CalledProcessError):
                continue

    # Repli optionnel : pyperclip, s'il est installé (bibliothèque tierce,
    # donc pas de dépendance dure).
    try:
        import pyperclip  # type: ignore

        pyperclip.copy(text)
        return True
    except Exception:
        return False


def copy_prompt() -> int:
    """Copie PROMPT_CLAUDE.md (à côté du script) dans le presse-papiers."""
    prompt_path = Path(__file__).resolve().parent / PROMPT_FILE
    try:
        text = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Erreur : impossible de lire '{prompt_path}' : {exc}", file=sys.stderr)
        return 1

    if write_clipboard(text):
        print(f"Prompt copié dans le presse-papiers : {prompt_path}")
        return 0

    print(
        "Erreur : impossible d'écrire dans le presse-papiers "
        "(aucune méthode système n'a fonctionné, pyperclip non installé).",
        file=sys.stderr,
    )
    return 1


# --- Programme principal ----------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Applique les fichiers complets, patches FIND / REPLACE et suppressions d'une réponse de Claude."
    )
    ap.add_argument("source", nargs="?", default="-", help="fichier texte à lire ('-' ou absent = stdin)")
    ap.add_argument("--root", default=".", help="dossier racine du projet (défaut : dossier courant)")
    ap.add_argument("--dry-run", action="store_true", help="simule sans rien écrire ni supprimer")
    ap.add_argument(
        "--copy-prompt",
        action="store_true",
        help="copie le contenu de PROMPT_CLAUDE.md dans le presse-papiers puis quitte",
    )
    args = ap.parse_args()

    if args.copy_prompt:
        sys.exit(copy_prompt())

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
        print("Aucun bloc détecté (fichier complet, patch FIND / REPLACE ou suppression).", file=sys.stderr)
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
                recreated = ws.was_deleted(p)
                existed = ws.exists(p)
                if existed and p not in ws.cache:
                    ws.read(p)  # pour retenir le style de fin de ligne
                content = payload if payload.endswith("\n") else payload + "\n"
                ws.write(p, content)
                label = "remplacé" if existed else ("recréé" if recreated else "créé")
                done.append(f"{label} : {rel}")

            elif kind == "delete":
                ws.delete(p)
                done.append(f"supprimé : {rel}")

            else:  # patch
                if not ws.exists(p):
                    if ws.was_deleted(p):
                        raise ValueError("fichier supprimé plus tôt dans cette réponse (un patch exige un fichier existant)")
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

    print("[DRY-RUN] aucun fichier écrit ni supprimé.\n" if args.dry_run else "", end="")
    print("Résultat :")
    for d in done:
        print(f"  - {d}")
    if errors:
        print("\nErreurs :")
        for e in errors:
            print(f"  - {e}")
    print(f"\nTotal : {len(done)} opération(s) appliquée(s), {len(errors)} erreur(s).")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
