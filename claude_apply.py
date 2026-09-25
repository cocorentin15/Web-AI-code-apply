#!/usr/bin/env python3
"""
apply_claude.py

Lit le presse-papiers, détecte des blocs au format :

=== FILE: chemin/vers/fichier ===
contenu
=== END FILE ===

et écrit/écrase chaque fichier correspondant, en créant les dossiers
parents si nécessaire. Fonctionne pour n'importe quel projet (Expo,
React Native, Java, etc.) : le script ne connaît que le format de
bloc, rien du contenu.

Usage :
    python apply_claude.py                # lit le presse-papiers
    python apply_claude.py chemin/fichier.txt   # lit un fichier texte

N'utilise que la bibliothèque standard.

Si un argument est fourni, il est lu comme un fichier texte (UTF-8) :
c'est le mode le plus fiable, indépendant de l'OS et des outils
disponibles (pratique sous Ubuntu/WSL/SSH où le presse-papiers
système n'est pas toujours accessible).

Sans argument, le script lit le presse-papiers, spécifiquement par
plateforme :
- Windows : appel direct de l'API Windows via ctypes (pas de sous-
  processus, donc aucun souci d'encodage de console).
- macOS   : commande "pbpaste", décodée explicitement en UTF-8.
- Linux   : "xclip" ou "xsel" (à installer : "sudo apt install
  xclip"), décodée explicitement en UTF-8. Si aucun des deux n'est
  disponible (ou pas de serveur X), utilisez plutôt le mode fichier.
Si aucune de ces méthodes ne fonctionne et que le module optionnel
"pyperclip" est installé, il est utilisé en repli.
"""

import platform
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


def read_clipboard_windows():
    """
    Lit le presse-papiers Windows directement via l'API Win32 (ctypes),
    sans passer par un sous-processus. Évite tout problème d'encodage
    de console : le presse-papiers texte Windows est déjà en UTF-16,
    on le récupère tel quel.
    Retourne None si le presse-papiers ne contient pas de texte.
    """
    import ctypes

    CF_UNICODETEXT = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    if not user32.OpenClipboard(0):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            data = ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
        return data
    finally:
        user32.CloseClipboard()


def read_clipboard_command(cmd):
    """Exécute une commande externe et décode sa sortie en UTF-8."""
    result = subprocess.run(cmd, capture_output=True, check=True)
    return result.stdout.decode("utf-8", errors="replace")


def read_clipboard() -> str:
    """Récupère le texte du presse-papiers selon la plateforme courante."""
    system = platform.system()

    if system == "Windows":
        try:
            text = read_clipboard_windows()
            if text is not None:
                return text
        except OSError:
            pass
    elif system == "Darwin":
        try:
            return read_clipboard_command(["pbpaste"])
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass
    else:
        for cmd in (
            ["xclip", "-selection", "clipboard", "-o"],
            ["xsel", "--clipboard", "--output"],
        ):
            try:
                return read_clipboard_command(cmd)
            except (FileNotFoundError, subprocess.CalledProcessError):
                continue

    # Repli optionnel si la méthode native n'a pas fonctionné.
    try:
        import pyperclip  # type: ignore
        return pyperclip.paste()
    except ImportError:
        pass

    print(
        "Erreur : impossible de lire le presse-papiers "
        "(aucune méthode système n'a fonctionné, pyperclip non installé).",
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