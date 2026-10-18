# OUTPUT RULES (answer applied automatically by script)

1. Structure: a short analysis BEFORE the changes (files touched, behavior, risks; 3 to 6 lines), then ONLY blocks. No text between the blocks or after the last one: the script would absorb it into the content of the last file or the last REPLACE.
2. Mode: choose the cheapest in tokens; on a tie, SCRIPT MODE (more robust). New or heavily modified file = SCRIPT MODE. Patch = existing file only.
3. Exact formats (each marker alone on its line, at the start of the line, without a markdown fence; operations run in order):
- SCRIPT MODE: `=== FILE: path ===`, then the COMPLETE file, then `=== END FILE ===` (mandatory after each file).
- SMALL-AI MODE, one patch: `FILE: path`, `FIND`, exact block, `REPLACE`, new block (empty REPLACE = block deleted).
- Several patches: `FILE: path`, then for each patch `PATCH n`, `FIND`, exact block, `REPLACE`, new block.
- Deleting a file: `=== DELETE FILE: path ===`, nothing else.
4. FIND: copied verbatim from the provided context (exact indentation) and unique in the file; add neighboring lines if needed.
5. Forbidden: pseudo-code, "...", "left unchanged", line numbers, markdown fences around blocks, abridged or partial complete file.
6. A file whose content would itself contain a header line of the format (start of line: `=== FILE`, `=== END FILE`, `=== DELETE FILE`, `FILE:`) should be avoided or reworded.
