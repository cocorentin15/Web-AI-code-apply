#!/usr/bin/env python3
"""
claude_apply.py

Automatically applies a Claude answer that follows one of the two modes of the
output-rules prompt (see PROMPT_CLAUDE.md): complete files (SCRIPT MODE),
FIND / REPLACE patches (SMALL-AI MODE) and deletions.

In the examples below, the leading "|" is only for readability and to leave no
header line in this file (the parser recognizes a header even when indented,
including inside a complete file). It is NOT part of the syntax.

SCRIPT MODE (complete file):

    | === FILE: path/to/file ===
    | complete content...
    | === END FILE ===

SMALL-AI MODE (compact patch):

    | FILE: path/to/file
    | FIND
    | [exact block]
    | REPLACE
    | [new block]

  or, for several changes in the same file:

    | FILE: path/to/file
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

DELETE (files only):

    | === DELETE FILE: path/to/file ===

  or, tolerated form, a single line, in UPPERCASE, at the start of the line
  (recognized only outside a complete-file block):

    | DELETE FILE: path/to/file

The three forms can be mixed in one text. Operations run in the order of the
text: deleting then recreating the same path in the same answer works.

Application rules
-----------------
- Patch: the FIND block must exist EXACTLY ONCE in the file.
  If the exact match fails, we retry ignoring trailing whitespace.
  0 occurrences or several occurrences => error, nothing is written
  for that file (all or nothing per file).
- Patches for a file are applied in order, in memory;
  the file is written only if ALL of them succeed.
- Empty REPLACE = deletion of the block (the line is removed too).
- CRLF line endings of an existing file are preserved.
- Delete: existing file only. A directory is refused and parent
  directories are never deleted. Missing file = error.
- Paths must stay inside the root folder (--root, by default
  the current directory): no absolute path and no "..".

Tolerances
----------
- Banners: no colon, any case, 2 "=" or more, indentation.
- The end-of-file line is optional: the block stops at the next
  header or at the end of the text (so any text after it is absorbed).
- Markdown fences (three ` or three ~, with or without language / space)
  around the whole text, a file or a FIND / REPLACE block: removed.
- The patch-mode keywords (FILE:, FIND, REPLACE, PATCH n) must be
  in UPPERCASE and at the start of the line, so they are not mistaken for
  content.

Known limitation: no escaping. A line that looks like a header is
treated as one even in the middle of a file's content.

Usage
-----
    python claude_apply.py response.txt
    python claude_apply.py response.txt --dry-run     # writes nothing
    python claude_apply.py response.txt --root my_project
    cat response.txt | python claude_apply.py -       # reads stdin
    python claude_apply.py --copy-prompt             # copies PROMPT_CLAUDE.md

Exit code: 0 if everything is applied, 1 if there was at least one error.
Dependencies: Python standard library only.
"""

import argparse
import platform
import re
import subprocess
import sys
from pathlib import Path


# --- Regular expressions ----------------------------------------------------
# Headers tolerate indentation (\s* at the start): an answer copied
# from a chat often arrives indented. Consequence: a header line is
# recognized even indented, including inside a complete file.

# Complete-file banner (script mode): ":" optional, any case,
# 2 or more "=" signs on each side.
START_PATTERN = re.compile(
    r"^\s*={2,}\s*FILE\s*:?\s*(?P<path>.+?)\s*={2,}\s*$", re.IGNORECASE
)
# End of complete file.
END_PATTERN = re.compile(r"^\s*={2,}\s*END\s+FILE\s*={2,}\s*$", re.IGNORECASE)

# Delete: banner (same tolerances as START_PATTERN) or tolerated
# form "DELETE FILE: path" (UPPERCASE, strict start of line). The tolerated
# form is not recognized inside a complete file, where it might be
# content (see _delete_target).
DELETE_BANNER_PATTERN = re.compile(
    r"^\s*={2,}\s*DELETE\s+FILE\s*:?\s*(?P<path>.+?)\s*={2,}\s*$", re.IGNORECASE
)
DELETE_PLAIN_PATTERN = re.compile(r"^DELETE FILE\s*:\s*(?P<path>.+?)[*`\s]*$")

# Patch mode: "FILE: path" - tolerates **FILE: x** or `FILE: x`
PATCH_FILE_PATTERN = re.compile(r"^\s*[*`]*FILE\s*:\s*(?P<path>.+?)[*`\s]*$")
FIND_PATTERN = re.compile(r"^FIND\s*:?\s*$")
REPLACE_PATTERN = re.compile(r"^REPLACE\s*:?\s*$")
PATCHNUM_PATTERN = re.compile(r"^PATCH\s+\d+\s*:?\s*$")

# Markdown fences
FENCE_OPEN = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})\s*[\w+#.\-]*\s*$")
FENCE_CLOSE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})\s*$")


def _clean_path(raw: str) -> str:
    return raw.strip().strip("`*\"' ")


def _delete_target(line: str, allow_plain: bool):
    """
    Target path if `line` is a deletion, otherwise None (empty path possible
    if the header is degenerate: the caller reports it as an error).
    `allow_plain` allows the form "DELETE FILE: path": False inside a
    complete file, where this line may legitimately be part of the content.
    """
    m = DELETE_BANNER_PATTERN.match(line)
    if not m and allow_plain:
        m = DELETE_PLAIN_PATTERN.match(line)
    if not m:
        return None
    # strip("="): a banner without a path would otherwise give the "path" "=".
    return _clean_path(m.group("path")).strip("= ")


# --- Block cleanup ----------------------------------------------------------

def _count_fences(lines):
    return sum(1 for ln in lines if FENCE_OPEN.match(ln))


def clean_block(lines):
    """
    Removes blank lines at both ends then any fence:
    - opening + closing fence that enclose the whole block: removed;
    - orphan closing fence at the end (odd number of fences, no
      opening at the start): removed.
    Fences in the middle of the content are never touched.
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
    """Removes a fence that wraps the WHOLE text (exactly 2 fences)."""
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
    Returns (ops, errors).
    ops, in the order of the text:
      ("full", path, content)
      ("patch", path, [(find, repl), ...])
      ("delete", path, None)

    State parser, a single pass line by line:
      idle        outside any block: prose is ignored
      full        content of a complete file: only a file header,
                  the end of file or a delete banner count
                  (FIND, REPLACE, etc. are content there)
      after_file  after the patch header, waiting for the first FIND
      find        reading the FIND block
      replace     reading the REPLACE block
    Outside "full", all headers are recognized at any time and
    end the current block (finish).

    Trap: without an explicit end, the text following a complete block or a
    REPLACE (including prose) is absorbed into its content. Hence the rule
    "nothing after the last block" in the prompt.
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
        """Closes the current block (emits its op or an error) and returns to idle."""
        nonlocal state, path, buf, find_buf, patches
        if state == "full":
            ops.append(("full", path, joined(buf)))
        elif state == "replace":
            patches.append((joined(find_buf), joined(buf)))
        elif state == "find":
            errors.append(f"{path} : FIND block without REPLACE (ignored).")
        if state in ("after_file", "find", "replace"):
            if patches:
                ops.append(("patch", path, patches))
            else:
                errors.append(f"{path} : no usable patch.")
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
        # A fence without a language just before a header is the opening
        # (spurious) fence of the next block, not the closing of the current
        # block.
        if state != "idle" and FENCE_CLOSE.match(line) and _is_header(next_nonblank(i)):
            if FENCE_CLOSE.match(prev_nonblank(i)) or _count_fences(buf) % 2 == 0:
                continue

        # Complete file: only the end, a new header and the delete
        # banner count (the bare DELETE form would be content).
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

        # Other states: headers recognized everywhere.
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
            # PATCH n, fences, blank lines, prose: ignored
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
        # idle: ignored

    finish()
    return ops, errors


# --- File system (with in-memory cache) -------------------------------------

_DELETED = object()  # deletion marker in Workspace.cache


class Workspace:
    """
    Disk access + in-memory cache, indexed by resolved path.

    The cache serves two purposes:
    - successive operations on the same file see the result of the
      previous ones (chained patches, delete then recreate, ...);
    - in --dry-run it is the only "write": nothing touches the disk, but
      exists() and read() reflect the simulated state.
    Cache value: file text (line endings normalized to LF) or
    _DELETED. `crlf` remembers files read as CRLF so they are rewritten that
    way.
    """

    def __init__(self, root: Path, dry_run: bool):
        self.root = root.resolve()
        self.dry_run = dry_run
        self.cache = {}
        self.crlf = {}

    def resolve(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(f"path outside the root folder: {rel}")
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
                raise FileNotFoundError(f"file deleted: {p}")
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
        """Deletes a file (never a directory, never parent directories)."""
        if p not in self.cache and p.is_dir():
            raise ValueError("this is a directory (deletion only applies to files)")
        if not self.exists(p):
            raise ValueError("file not found (nothing to delete)")
        if not self.dry_run:
            p.unlink()
        self.cache[p] = _DELETED
        # A file recreated afterwards is a new file: LF line endings.
        self.crlf.pop(p, None)


# --- Applying a patch -------------------------------------------------------

def apply_one(text: str, find: str, repl: str) -> str:
    """Replaces `find` (unique) with `repl`. Raises ValueError otherwise."""
    if not find.strip():
        raise ValueError("empty FIND block")

    n = text.count(find)
    if n == 1:
        if repl == "" and text.count(find + "\n") == 1:
            return text.replace(find + "\n", "", 1)
        return text.replace(find, repl, 1)
    if n > 1:
        raise ValueError(f"ambiguous FIND block ({n} occurrences)")

    # Fallback without trailing whitespace: a block copied from a chat or
    # an editor often loses (or gains) these invisible spaces. The
    # result is accepted only if it remains unique.
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
        raise ValueError(f"ambiguous FIND block ({len(hits)} occurrences, whitespace ignored)")
    raise ValueError("FIND block not found")


# --- Clipboard --------------------------------------------------------------

PROMPT_FILE = "PROMPT_CLAUDE.md"


def _copy_windows(text: str) -> bool:
    """
    Writes to the Windows clipboard via the Win32 API in ctypes.

    Why not launch "clip"? Because a subprocess goes through the
    console, whose encoding is a pain (CP850, CP1252, UTF-8...). By
    calling SetClipboardData directly, we place the UTF-16 string as-is,
    without depending on the console code page.
    """
    import ctypes
    from ctypes import wintypes

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # On 64-bit Windows, a HANDLE/pointer does not fit in an int: without an
    # explicit restype, ctypes truncates the value and memmove writes
    # anywhere. So we declare the precise signatures used.
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE

    # UTF-16 string terminated by a NUL (the format expected by the clipboard).
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
        # After SetClipboardData, the memory belongs to the clipboard:
        # we must NOT free it ourselves.
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
            return False
        return True
    finally:
        user32.CloseClipboard()


def _copy_command(cmd, text: str):
    """Runs an external utility (pbcopy, xclip, xsel, wl-copy) that reads stdin."""
    subprocess.run(cmd, input=text.encode("utf-8"), check=True)


def write_clipboard(text: str) -> bool:
    """
    Writes `text` to the clipboard depending on the platform, with several
    cascading fallbacks. Returns True if one method succeeded.
    """
    system = platform.system()

    if system == "Windows":
        try:
            if _copy_windows(text):
                return True
        except OSError:
            # Failure of the Win32 API: we will try pyperclip as a fallback.
            pass
    elif system == "Darwin":
        try:
            _copy_command(["pbcopy"], text)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass
    else:
        # Linux and other Unix: Wayland first, then X11 (xclip, xsel).
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

    # Optional fallback: pyperclip, if installed (third-party library,
    # so not a hard dependency).
    try:
        import pyperclip  # type: ignore

        pyperclip.copy(text)
        return True
    except Exception:
        return False


def copy_prompt() -> int:
    """Copies PROMPT_CLAUDE.md (next to the script) to the clipboard."""
    prompt_path = Path(__file__).resolve().parent / PROMPT_FILE
    try:
        text = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Error: cannot read '{prompt_path}': {exc}", file=sys.stderr)
        return 1

    if write_clipboard(text):
        print(f"Prompt copied to the clipboard: {prompt_path}")
        return 0

    print(
        "Error: cannot write to the clipboard "
        "(no system method worked, pyperclip not installed).",
        file=sys.stderr,
    )
    return 1


# --- Main program -----------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Applies the complete files, FIND / REPLACE patches and deletions of a Claude answer."
    )
    ap.add_argument("source", nargs="?", default="-", help="text file to read ('-' or absent = stdin)")
    ap.add_argument("--root", default=".", help="root folder of the project (default: current directory)")
    ap.add_argument("--dry-run", action="store_true", help="simulates without writing or deleting anything")
    ap.add_argument(
        "--copy-prompt",
        action="store_true",
        help="copies the contents of PROMPT_CLAUDE.md to the clipboard then quits",
    )
    args = ap.parse_args()

    if args.copy_prompt:
        sys.exit(copy_prompt())

    if args.source == "-":
        if sys.stdin.isatty():
            ap.error("no source: give a file or send the text on stdin")
        source_text = sys.stdin.read()
    else:
        try:
            source_text = Path(args.source).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Error: cannot read '{args.source}': {exc}", file=sys.stderr)
            sys.exit(1)

    if not source_text.strip():
        print("Error: the source is empty.", file=sys.stderr)
        sys.exit(1)

    ops, errors = parse(source_text)
    if not ops and not errors:
        print("No block detected (complete file, FIND / REPLACE patch or deletion).", file=sys.stderr)
        sys.exit(1)

    ws = Workspace(Path(args.root), args.dry_run)
    done = []

    for kind, rel, payload in ops:
        if not rel:
            errors.append("Block ignored: empty file path.")
            continue
        try:
            p = ws.resolve(rel)

            if kind == "full":
                recreated = ws.was_deleted(p)
                existed = ws.exists(p)
                if existed and p not in ws.cache:
                    ws.read(p)  # to remember the line-ending style
                content = payload if payload.endswith("\n") else payload + "\n"
                ws.write(p, content)
                label = "replaced" if existed else ("recreated" if recreated else "created")
                done.append(f"{label}: {rel}")

            elif kind == "delete":
                ws.delete(p)
                done.append(f"deleted: {rel}")

            else:  # patch
                if not ws.exists(p):
                    if ws.was_deleted(p):
                        raise ValueError("file deleted earlier in this answer (a patch requires an existing file)")
                    raise ValueError("file not found (a patch requires an existing file)")
                text = ws.read(p)
                failed = False
                for k, (find, repl) in enumerate(payload, 1):
                    try:
                        text = apply_one(text, find, repl)
                    except ValueError as exc:
                        errors.append(f"{rel} (patch {k}/{len(payload)}): {exc}.")
                        failed = True
                if failed:
                    errors.append(f"{rel}: this block of patches was NOT applied (all or nothing).")
                else:
                    ws.write(p, text)
                    done.append(f"patched ({len(payload)}): {rel}")

        except (OSError, ValueError, UnicodeDecodeError) as exc:
            errors.append(f"{rel}: {exc}")

    print("[DRY-RUN] no file written or deleted.\n" if args.dry_run else "", end="")
    print("Result:")
    for d in done:
        print(f"  - {d}")
    if errors:
        print("\nErrors:")
        for e in errors:
            print(f"  - {e}")
    print(f"\nTotal: {len(done)} operation(s) applied, {len(errors)} error(s).")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
