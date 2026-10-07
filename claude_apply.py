#!/usr/bin/env python3
"""
apply_claude.py

But du script
-------------
Tu enregistres dans un fichier un
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
    python apply_claude.py chemin/fichier.txt    # lit un fichier texte

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



# --- Nettoyage et extraction des blocs --------------------------------------

FENCE_OPEN = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})\s*[\w+#.\-]*\s*$")
FENCE_CLOSE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})\s*$")


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
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    if len(lines) >= 2:
        open_m = FENCE_OPEN.match(lines[0])
        close_m = FENCE_CLOSE.match(lines[-1])
        if open_m and close_m and open_m.group("fence") == close_m.group("fence"):
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
        print("no argument")

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