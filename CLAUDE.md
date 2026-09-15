# idea-migrate

Move a JetBrains project directory to a new location and repair every stored
reference to its old path, so nothing that pointed at the project breaks.

## The problem this solves

A JetBrains IDE records the absolute path of every project it has opened
*outside* the project, in its own settings directory under
`~/Library/Application Support/JetBrains`. Claude Code does the same, in
`~/.claude/projects/<encoded-path>/` and in `~/.claude.json`. Move the folder in
Finder and none of them are told: the project disappears from Recent Projects,
and the session history and per-project tool permissions are orphaned under a
name derived from a path that no longer exists. Nothing inside the project
folder can fix this, because the stale path is not in the project folder.

The goal is **reversibility**. Every run backs up what it is about to change,
writes a manifest of what it did, and ships an `undo.sh` that works even if this
tool is what broke. Assume any change you make will be judged on whether the
undo path still restores the machine byte-for-byte; `tests/test_roundtrip.py`
hashes every file before and after to prove it.

## Architecture

The command-line layer is thin; the decisions live in `batch`.

**`batch.py`** is the API and the centre of the design. `plan_moves(pairs,
config, home)` decides what a set of moves implies and touches nothing, running
every guard for every move and raising `PlanError` carrying *every* problem
found rather than the first. `BatchRun(plan, now=...).execute()` then carries it
out and raises `RunFailed` — whose `.state` says how far it got — if a move
fails part-way. Nothing in `batch` prints; progress goes to an optional
callback. Preserve both properties: plan-before-touch, and print-free.

**`cli.py`** reads arguments, asks the confirmation question, prints the report,
turns an error into an exit code. New behaviour belongs in `batch`, not here.

Everything else is a single-purpose module, each with a module docstring
explaining the reasoning behind its rules. Read that docstring before changing
the module — it is the design record.

| Module | Job |
| --- | --- |
| `paths.py` | Validate a source/destination pair; normalise lexically, compare with `os.path.samefile`. |
| `mover.py` | The move itself: atomic rename within a filesystem, verified copy across. |
| `rewrite.py` | Rewrite path references inside JetBrains XML. The highest-risk code here. |
| `claude_projects.py` | Rename Claude Code's per-project directories and rewrite its JSONL transcripts. |
| `claude_registry.py` | Rename the project keys in `~/.claude.json`. |
| `ide_caches.py` | Rename the IDE's external module storage, which lives under `~/Library/Caches` and is keyed by a hash of the project path. |
| `backup.py` | Copy `options/` and `workspace/` before the run; generate the standalone `undo.sh`. |
| `manifest.py` | The record of what a run did. Single source of truth for undo and for listing. |
| `undo.py` | Roll back one run. Never deletes the backup. |
| `products.py` | Find installed IDE config directories structurally (a dir containing `options/`). |
| `processes.py` | Detect running IDEs. Toolbox is excluded on purpose. |
| `config.py` | Optional TOML config; every setting has a working default. |
| `listing.py`, `report.py`, `completion.py`, `errors.py` | Backups listing, output formatting, readline path completion, exception hierarchy. |

## Invariants to preserve

These are the rules the safety of the tool rests on. Each is explained in full
in its module's docstring.

- **Boundary anchoring.** A path prefix matches only when the next character
  ends the path component. Space and `>` are legal in macOS directory names and
  are therefore *not* boundaries. Widening this set once caused `~/Projects` to
  corrupt unrelated references; keep the boundary set explicit.
- **Case-insensitive matching, case-preserving output.** macOS is
  case-insensitive, so the same directory can be recorded under two spellings
  and both must move. Replace only the matched prefix and keep every byte after
  it exactly as found.
- **One pass.** All prefix variants go into a single alternation, so no byte can
  be rewritten twice by a later variant matching an earlier one's output.
- **Verify then write.** JetBrains XML and Claude transcripts are edited as raw
  text and re-parsed to confirm validity before the write lands. `~/.claude.json`
  is the one exception: it is edited on the parsed object, because a key rename
  cannot be done safely by text substitution. Its formatting is measured from
  the file and reproduced.
- **Prefix matching over-reaches.** `/x/foobar` starts with `/x/foo`. A match
  requires equality or a genuine separator, in every module that matches paths.
- **The cache hash is looked up, never trusted.** A project's stored module
  definitions live in a directory named `<name>.<hash>`, where the hash is
  Java's `String.hashCode()` of the absolute path in hex. Computing it for the
  source is the same lookup the IDE does, but the derivation is undocumented and
  32 bits wide, so a candidate is only renamed once its own `cache-state.xml` is
  shown to name the source path. No match means a warning, never a guess.

- **Errors derive from `MigrateError`.** The CLI catches that one class and
  prints a single line. A traceback means a bug, not a user mistake.

## Working in this repo

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t . -v   # tests
PYTHONPATH=src python3 -m idea_migrate --dry-run --source ~/A --dest ~/B
```

- **Standard library only.** Python 3.11+, macOS, zero third-party
  dependencies — including in the tests, which use `unittest`. Reach for
  `tempfile`, `unittest.mock`, and synthetic home directories built in the test
  itself, the way the existing tests do.
- **Test alongside the module** (`tests/test_<module>.py`); cross-cutting
  guarantees about migrate-then-undo go in `tests/test_roundtrip.py`.
- **Comments and docstrings carry the reasoning, not the mechanism.** The house
  style states *why* a rule exists and what breaks without it. Match that
  density when adding code.
- Prefer frozen dataclasses for the values passed between modules, and
  `from __future__ import annotations` at the top of each module.

Everyday usage lives in [README.md](README.md); the config reference, the Python
API in detail, and test instructions live in [docs/usage.md](docs/usage.md).
The design record for the original build is under `docs/superpowers/`.
