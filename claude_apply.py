#!/usr/bin/env python3
"""
apply_claude.py

But du script
-------------
Tu copies dans ton presse-papiers (ou tu enregistres dans un fichier) un
texte qui contient un ou plusieurs blocs du genre :

    === FILE: chemin/vers/fichier ===
    contenu du fichier...
    === END FILE ===

Le script détecte ces blocs et écrit chaque contenu dans le fichier
correspondant, en créant les dossiers parents si besoin.

Il fonctionne pour n'importe quel projet (Expo, React Native, Java...).
Le script ne sait RIEN du contenu : il ne connaît que le format de bloc.

Tolérances ajoutées (pour résister aux variations de sortie de Claude) :
- "=== FILE path ===" (sans deux-points) est accepté.
- La casse est ignorée ("File", "file", "END FILE", ...).
- Le nombre de "=" est libre (2 ou plus).
- "=== END FILE ===" est optionnel : le bloc se termine au prochain
  "=== FILE ... ===" ou à la fin du texte.
- Si le contenu est enveloppé dans une fence markdown (``` ou ~~~),
  avec ou sans langage, avec ou sans espace après les backticks
  (ex : "``` tsx"), la fence est retirée automatiquement.

Usage
-----
    python apply_claude.py                       # lit le presse-papiers
    python apply_claude.py chemin/fichier.txt    # lit un fichier texte

Le mode "fichier" est le plus fiable, surtout sous Ubuntu/WSL/SSH où
l'accès au presse-papiers système n'est pas toujours possible.

Dépendances : uniquement la bibliothèque standard Python.
"""

# --- Imports ----------------------------------------------------------------
# platform  : pour savoir sur quel OS on tourne (Windows, macOS, Linux).
# re        : pour les expressions régulières (détection des blocs).
# sys       : pour lire argv, écrire sur stderr, quitter avec un code.
# subprocess: pour lancer les utilitaires externes (pbpaste, xclip, xsel).
# Path      : pour manipuler les chemins proprement (création de dossiers,
#             lecture/écriture de fichiers, indépendant de l'OS).
import platform
import re
import sys
import subprocess
from pathlib import Path


# --- Expressions régulières de détection des marqueurs ----------------------
#
# On utilise re.MULTILINE nulle part ici : on applique les regex ligne par
# ligne plus bas, ce qui évite les pièges de fin de ligne.
#
# START_PATTERN : repère une ligne du type "=== FILE: chemin ===".
#   - ^\s*           : début de ligne, espaces/tabs optionnels.
#   - ={2,}          : au moins 2 signes "=" (tolère "==", "====", ...).
#   - \s*FILE\s*     : le mot FILE, avec espaces autour. IGNORECASE plus bas
#                      rend "file", "File", "FILE" équivalents.
#   - :?             : deux-points OPTIONNEL (Claude les oublie parfois).
#   - (?P<path>.+?)  : capture non-greedy du chemin (groupe nommé "path").
#   - \s*={2,}\s*$   : on referme sur au moins 2 "=" en fin de ligne.
START_PATTERN = re.compile(
    r"^\s*={2,}\s*FILE\s*:?\s*(?P<path>.+?)\s*={2,}\s*$",
    re.IGNORECASE,
)

# END_PATTERN : repère "=== END FILE ===" de la même façon tolérante.
# On n'utilise pas de capture ici, on veut juste savoir "cette ligne
# termine le bloc courant".
END_PATTERN = re.compile(
    r"^\s*={2,}\s*END\s+FILE\s*={2,}\s*$",
    re.IGNORECASE,
)

# FENCE_OPEN : détecte une ligne d'ouverture de bloc de code markdown.
#   - (`{3,}|~{3,})  : au moins 3 backticks OU au moins 3 tildes.
#   - \s*            : espaces optionnels AVANT le langage.
#                      C'est CE \s* qui corrige ton bug "``` tsx".
#   - [\w+#.\-]*     : le langage : lettres/chiffres/underscore, plus
#                      "+", "#", ".", "-" pour c++, c#, .ts, ts-node, etc.
#                      Le "*" rend le langage optionnel ("```" tout seul).
#   - \s*$           : espaces/tabs en fin de ligne tolérés.
FENCE_OPEN = re.compile(r"^\s*(`{3,}|~{3,})\s*[\w+#.\-]*\s*$")

# FENCE_CLOSE : une ligne composée uniquement de ``` ou ~~~ (avec espaces
# éventuels autour). Sert à vérifier qu'on peut refermer proprement.
FENCE_CLOSE = re.compile(r"^\s*(`{3,}|~{3,})\s*$")


# --- Lecture du presse-papiers ----------------------------------------------

def read_clipboard_windows():
    """
    Lecture du presse-papiers sous Windows via l'API Win32 en ctypes.

    Pourquoi ne pas lancer "powershell Get-Clipboard" ?
    Parce qu'un sous-processus passe par la console, et l'encodage de la
    console Windows est une plaie (CP850, CP1252, UTF-8...). En appelant
    directement l'API, on récupère la chaîne UTF-16 telle quelle.

    Retourne None si le presse-papiers ne contient pas de texte.
    """
    # Import local : inutile de charger ctypes sur Linux/macOS.
    import ctypes

    # CF_UNICODETEXT = 13 : identifiant Win32 du format "texte Unicode".
    CF_UNICODETEXT = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # OpenClipboard(0) : 0 = fenêtre propriétaire (aucune dans notre cas).
    # Retourne 0 en cas d'échec (presse-papiers verrouillé par un autre
    # process, par exemple).
    if not user32.OpenClipboard(0):
        return None
    try:
        # GetClipboardData renvoie un HANDLE (entier opaque), pas la chaîne.
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None

        # GlobalLock : on "épingle" la mémoire du presse-papiers pour
        # obtenir un pointeur utilisable.
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            # wstring_at lit une chaîne UTF-16 jusqu'au NUL final.
            data = ctypes.wstring_at(pointer)
        finally:
            # Toujours déverrouiller, même en cas d'exception.
            kernel32.GlobalUnlock(handle)
        return data
    finally:
        # Toujours refermer le presse-papiers, sinon il reste bloqué pour
        # les autres applications.
        user32.CloseClipboard()


def read_clipboard_command(cmd):
    """
    Lance une commande externe qui écrit le presse-papiers sur stdout
    (pbpaste, xclip, xsel) et décode sa sortie en UTF-8.

    errors="replace" : si un octet est invalide, on le remplace par un
    caractère de substitution plutôt que de planter.
    """
    # check=True : lève CalledProcessError si la commande échoue
    # (utile pour basculer vers la méthode suivante).
    result = subprocess.run(cmd, capture_output=True, check=True)
    return result.stdout.decode("utf-8", errors="replace")


def read_clipboard() -> str:
    """
    Récupère le texte du presse-papiers selon la plateforme courante,
    avec plusieurs replis en cascade.
    """
    system = platform.system()

    if system == "Windows":
        try:
            text = read_clipboard_windows()
            # Attention : une chaîne vide est valide (presse-papiers vide).
            # On teste donc "is not None", pas la véracité.
            if text is not None:
                return text
        except OSError:
            # Si l'API Win32 échoue, on tombera dans le repli pyperclip.
            pass

    elif system == "Darwin":
        try:
            return read_clipboard_command(["pbpaste"])
        except (FileNotFoundError, subprocess.CalledProcessError):
            # pbpaste absent (improbable sur macOS) ou en échec.
            pass

    else:
        # Linux et autres Unix : on tente xclip puis xsel.
        # Les deux sont des utilitaires X11 ; à installer via
        # "sudo apt install xclip" (ou xsel).
        for cmd in (
            ["xclip", "-selection", "clipboard", "-o"],
            ["xsel", "--clipboard", "--output"],
        ):
            try:
                return read_clipboard_command(cmd)
            except (FileNotFoundError, subprocess.CalledProcessError):
                # FileNotFoundError : utilitaire non installé.
                # CalledProcessError : échec (pas de serveur X, etc.).
                continue

    # Repli optionnel : pyperclip, s'il est installé.
    # C'est une bibliothèque tierce, donc pas de dépendance dure.
    try:
        import pyperclip  # type: ignore
        return pyperclip.paste()
    except ImportError:
        pass

    # Si rien n'a marché, message clair + code de sortie non nul.
    print(
        "Erreur : impossible de lire le presse-papiers "
        "(aucune méthode système n'a fonctionné, pyperclip non installé).",
        file=sys.stderr,
    )
    sys.exit(1)


# --- Nettoyage et extraction des blocs --------------------------------------

def _strip_outer_fence(content: str) -> str:
    """
    Si TOUT le contenu du fichier est enveloppé dans une unique fence
    markdown, la retire. Sinon, renvoie le contenu tel quel.

    Objectif : ne pas casser les fichiers qui contiennent légitimement
    des blocs ``` au milieu (README, doc, tutoriel...). On ne retire la
    fence que si elle englobe vraiment tout.

    Exemple traité :
        ``` tsx            <- ligne 1 (fence ouvrante)
        import React ...
        export default ...
        ```                <- dernière ligne (fence fermante)

    Exemple NON traité (fence au milieu, on ne touche à rien) :
        Voici un exemple :
        ```ts
        console.log("hi")


        #!/usr/bin/env python3
"""

    lines = content.splitlines()

    # Retire les lignes vides en début et fin.
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    if (
        len(lines) >= 2
        and FENCE_OPEN.match(lines[0])
        and FENCE_CLOSE.match(lines[-1])
    ):
        lines = lines[1:-1]

    return "\n".join(lines)


def extract_blocks(text: str):
    """
    Parse le texte et retourne une liste de tuples (chemin, contenu).
    Gère l'absence de "=== END FILE ===" : le bloc se termine alors au
    prochain "=== FILE ... ===" ou à la fin du texte.
    """
    blocks = []
    current_path = None
    current_lines: list[str] = []

    def flush():
        if current_path is None:
            return
        raw = "\n".join(current_lines)
        content = _strip_outer_fence(raw)
        blocks.append((current_path, content))

    for line in text.splitlines():
        start_match = START_PATTERN.match(line)
        end_match = END_PATTERN.match(line)

        if start_match:
            flush()
            current_path = start_match.group("path").strip()
            current_lines = []
            continue

        if end_match:
            flush()
            current_path = None
            current_lines = []
            continue

        if current_path is not None:
            current_lines.append(line)

    flush()
    return blocks


# --- Écriture ---------------------------------------------------------------

def write_file(relative_path: str, content: str) -> Path:
    path = Path(relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not content.endswith("\n"):
        content += "\n"
    path.write_text(content, encoding="utf-8")
    return path


def main():
    if len(sys.argv) > 1:
        source_path = Path(sys.argv[1])
        try:
            source_text = source_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Erreur : impossible de lire '{source_path}' : {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        source_text = read_clipboard()

    if not source_text.strip():
        print("Erreur : la source (presse-papiers ou fichier) est vide.", file=sys.stderr)
        sys.exit(1)

    blocks = extract_blocks(source_text)

    if not blocks:
        print(
            "Aucun bloc '=== FILE: ... === / === END FILE ===' détecté "
            "dans la source fournie.",
            file=sys.stderr,
        )
        sys.exit(1)

    processed = []
    errors = []

    for raw_path, content in blocks:
        raw_path = raw_path.strip()
        if not raw_path:
            errors.append("Bloc ignoré : chemin de fichier vide.")
            continue
        try:
            written_path = write_file(raw_path, content)
            processed.append(str(written_path))
        except OSError as exc:
            errors.append(f"Échec d'écriture pour '{raw_path}' : {exc}")

    print("Fichiers créés ou modifiés :")
    for p in processed:
        print(f"  - {p}")

    if errors:
        print("\nErreurs rencontrées :")
        for e in errors:
            print(f"  - {e}")

    print(f"\nTotal traité : {len(processed)} fichier(s) sur {len(blocks)} bloc(s).")


if __name__ == "__main__":
    main()