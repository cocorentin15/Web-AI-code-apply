# claude_apply

A single-file Python script that automatically applies an AI assistant's answer to your project: complete files, small find-and-replace patches, and file deletions.

## What it does and why

When you ask an AI to modify a codebase, you usually end up copy-pasting code by hand, which is slow and error-prone. `claude_apply.py` removes that step. You ask the AI to format its answer in a strict, simple syntax, save the answer to a text file, and the script applies every change for you.

It supports three kinds of operations, which can be mixed in one answer and are executed in the order they appear in the text:

- **Full file**: create a new file or replace an existing one (script mode).
- **Patch**: replace a unique block of text inside an existing file (small-AI mode). It is cheaper in tokens than rewriting a whole file.
- **Delete**: remove a file.

## Installation

There is nothing to install. The script uses only the Python standard library and needs Python 3 (3.8 or newer is fine). Copy `claude_apply.py` into your project, or anywhere on your machine, and run it.

## Usage

```bash
python claude_apply.py response.txt
python claude_apply.py response.txt --dry-run
python claude_apply.py response.txt --root my_project
cat response.txt | python claude_apply.py -
```

| Argument | Meaning |
|---|---|
| `source` | Text file containing the AI's answer. Use `-` (or nothing) to read from stdin. |
| `--root DIR` | Project root. All paths are relative to it and cannot escape it. Default: current directory. |
| `--dry-run` | Simulate everything. Nothing is written or deleted, and the summary shows what would happen. |

The script prints a summary (created, replaced, patched, deleted, and a list of errors). The exit code is 0 if everything was applied and 1 if there was at least one error.

## Response formats

The format is line-based. Each marker below must be alone on its own line.

### Full file

A full-file block is made of these lines, in this order:

1. the header line `=== FILE: path/to/file ===`
2. the complete content of the file
3. the end line `=== END FILE ===`

If the file already exists it is replaced, otherwise it is created (parent folders included).

### Single patch

A patch block is made of these lines, in this order:

1. the path line `FILE: path/to/file`
2. the line `FIND`, followed by the exact lines to locate
3. the line `REPLACE`, followed by the new lines

For instance, to rename a variable in `src/app.js`, the answer contains the path line `FILE: src/app.js`, a `FIND` line followed by `let count = 0;`, then a `REPLACE` line followed by `let total = 0;`.

An empty REPLACE removes the matched block, including its line.

### Multiple patches in one file

Use one path line, then repeat this group for each patch: a line `PATCH 1` (then `PATCH 2`, and so on), the `FIND` line and its block, the `REPLACE` line and its block. A new `FIND` directly after a REPLACE block also starts a new patch.

### Deleting a file

- Main syntax: a line `=== DELETE FILE: path/to/file ===`. It is recognized everywhere, including inside a full-file block, where it ends the block like a new header would.
- Tolerated form: a line `DELETE FILE: path/to/file`, in capitals, at the very start of the line. It is recognized only outside a full-file block, otherwise it could be mistaken for file content.

Deleting then recreating the same path in the same answer works. A recreated file is treated as a new file (LF line endings).

## Application rules

- **Unique FIND**: the FIND block must appear exactly once in the file. If the exact text is not found, the script retries ignoring trailing spaces at the end of lines. Zero or several matches is an error.
- **All or nothing per file**: patches for a file are applied in memory, in order, and the file is written only if all of them succeed. One failing file does not stop the others.
- **CRLF preserved**: an existing file that uses Windows line endings keeps them after a patch or a replacement.
- **Paths confined to the root**: absolute paths and `..` escapes are refused.
- **Delete**: only existing files. Directories are refused, missing files are an error, and parent folders are never removed.
- **Order matters**: a patch on a file deleted earlier in the same answer fails cleanly, in a real run and in `--dry-run`.
- **Dry run**: deletions are simulated in an in-memory cache, so later operations see the simulated state.

## Tolerances

The parser forgives common formatting accidents:

- Header lines may omit the colon, use any letter case, use two or more equal signs, and be indented.
- The end line of a full file is optional: the block then stops at the next header or at the end of the text (and swallows any text that follows, see limitations).
- Markdown code fences around the whole text, around a file, or around a FIND or REPLACE block are removed.
- Patch keywords (`FILE:`, `FIND`, `REPLACE`, `PATCH n`) must be in capitals at the start of a line, so that ordinary content is not mistaken for them. A path line may be wrapped in bold or backticks.
- Prose outside any block is ignored.

## Known limitations

- **No escaping**: any line that looks like a header is treated as one, even in the middle of a file's content. Inside a full-file block this applies to the full-file header, the end line and the delete banner. Inside FIND and REPLACE blocks it also applies to the patch path line and to the plain delete line. Never write such a marker alone on its own line in a file delivered this way. That is why this README and `PROMPT_CLAUDE.md` describe the markers only inline, between backticks, in a sentence or a list item. A file that must contain such a line has to be reworded or written by hand.
- **Trailing text is absorbed**: a full file without an end line, or the last REPLACE block, swallows any text that follows it. Allow nothing after the last block.
- **Blank lines at the edges** of file contents, FIND blocks and REPLACE blocks are trimmed, and a full file always ends with exactly one newline.
- **Wrapping fences are removed**: a file whose first and last lines are both code fences loses them.
- **UTF-8 text only**: binary files are not supported.
- **Not atomic across files**: the all-or-nothing rule is per file. Other operations still run when one fails.
- **No undo**: use version control and review the result with a diff.
- The plain `DELETE FILE:` line is also recognized in prose and in patch blocks, as long as it starts the line.

## Recommended workflow

1. Pack your repository with Repomix and copy it to the clipboard: `npx repomix --copy`.
2. Paste it in the chat, with the contents of `PROMPT_CLAUDE.md` and your request.
3. Copy the AI's complete answer into a text file, for example `response.txt`.
4. Run `python claude_apply.py response.txt --dry-run` and check the summary and the errors.
5. Run it again without `--dry-run`, then review the changes with `git diff`.

## The prompt

`PROMPT_CLAUDE.md` contains the output rules to paste into your prompts so that the AI produces answers that are fully compatible with the script.
