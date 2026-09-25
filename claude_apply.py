#!/usr/bin/env python3
"""
claude_apply.py

Lit le presse-papiers, détecte des blocs au format :

=== FILE: chemin/vers/fichier ===
contenu
=== END FILE ===

et écrit/écrase chaque fichier correspondant, en créant les dossiers
parents si nécessaire. Fonctionne pour n'importe quel projet (Expo,
React Native, Java, etc.) : le script ne connaît que le format de
bloc, rien du contenu.

Usage :
    python claude_apply.py

N'utilise que la bibliothèque standard. La lecture du presse-papiers
se fait via un outil du système (pbpaste / clip / xclip / xsel) pour
éviter toute dépendance externe ; si aucun de ces outils n'est
disponible et que le module optionnel "pyperclip" est installé, il
est utilisé en repli.
"""

import re
import sys
import subprocess
from pathlib import Path

# Regex qui capture le chemin et le contenu de chaque bloc.
# - re.DOTALL : le contenu peut être multi-lignes.
# - non-greedy (.*?) : s'arrête au premier "=== END FILE ===" rencontré.
BLOCK_PATTERN = re.compile(
    r"=== FILE:\s*(.+?)\s*===\r?\n(.*?)\r?\n=== END FILE ===",
    re.DOTALL,
)


def read_clipboard() -> str:
    """Récupère le texte du presse-papiers, sans dépendance obligatoire."""
    commands = [
        ["pbpaste"],                                   # macOS
        ["powershell.exe", "-command", "Get-Clipboard"],  # Windows
        ["xclip", "-selection", "clipboard", "-o"],    # Linux (xclip)
        ["xsel", "--clipboard", "--output"],           # Linux (xsel)
    ]
    for cmd in commands:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=True
            )
            return result.stdout
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue

    # Repli optionnel si aucun outil système n'a fonctionné.
    try:
        import pyperclip  # type: ignore
        return pyperclip.paste()
    except ImportError:
        pass

    print(
        "Erreur : impossible de lire le presse-papiers "
        "(aucun outil système trouvé, pyperclip non installé).",
        file=sys.stderr,
    )
    sys.exit(1)


def extract_blocks(text: str):
    """Retourne la liste des blocs (chemin, contenu) trouvés dans le texte."""
    return BLOCK_PATTERN.findall(text)


def write_file(relative_path: str, content: str) -> Path:
    """Écrit le contenu dans le fichier, en créant les dossiers parents."""
    path = Path(relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Ajoute un retour à la ligne final si absent, par propreté.
    if not content.endswith("\n"):
        content += "\n"
    path.write_text(content, encoding="utf-8")
    return path


def main():
    clipboard_text = read_clipboard()

    if not clipboard_text.strip():
        print("Erreur : le presse-papiers est vide.", file=sys.stderr)
        sys.exit(1)

    blocks = extract_blocks(clipboard_text)

    if not blocks:
        print(
            "Aucun bloc '=== FILE: ... === / === END FILE ===' détecté "
            "dans le presse-papiers.",
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