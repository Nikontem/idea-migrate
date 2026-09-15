# Configuration reference and testing

This page holds material that used to live in the README: the optional TOML
configuration file and how to run the test suite. It also documents how to
drive the tool from Python instead of from a terminal. See the top-level
[README](../README.md) for everyday usage.

## Configuration

An optional TOML configuration file lets you override the backup location, point the tool at a non-standard IDE settings directory, and exclude specific products from repairs. If no file is specified, all defaults are used.

Pass the configuration file with `--config`:

```bash
idea-migrate --config ~/.idea-migrate.toml --source ~/X --dest ~/Y
```

The configuration file supports these keys:

```toml
# Optional: where to store backups. Defaults to ~/Idea-Migration-Backups.
backup_root = "~/.local/share/idea-backups"

# Optional: where the IDEs keep their settings. Defaults to
# ~/Library/Application Support/JetBrains. Change this only if your settings
# live somewhere else; the location is recorded in each backup's manifest, so
# an undo restores to the directory the backup was actually taken from.
jetbrains_root = "~/Library/Application Support/JetBrains"

# Optional: where Claude Code keeps its data. Defaults to ~/.claude. Recorded
# in each backup's manifest, so an undo restores to the directory the backup
# was actually taken from.
claude_root = "~/.claude"

# Optional: Claude Code's per-project registry file. Defaults to ~/.claude.json.
# Its entries are keyed by absolute project path, so a move renames them.
claude_registry = "~/.claude.json"

# Optional: where the IDEs keep their per-project caches. Defaults to
# ~/Library/Caches/JetBrains. Almost everything in there rebuilds itself and is
# left alone; the exception is the module definitions of a project that stores
# them outside its own .idea directory, which sit in a directory named after a
# hash of the project's path. Those are renamed so the move does not leave the
# project with no modules. Recorded in each backup's manifest, so an undo
# renames them back inside the tree the run actually touched.
caches_root = "~/Library/Caches/JetBrains"

# Optional: which products to skip. Useful if you have a product you never open
# or one that stores problematic references you do not want to touch.
#
# Each entry must be the FULL directory name as it appears under the JetBrains
# settings directory, which includes the version. "GoLand" on its own matches
# nothing, because the directory is called "GoLand2026.2".
exclude_products = ["GoLand2026.2", "RustRover2026.1"]
```

To see the exact names to use, list the settings directory:

```bash
ls ~/Library/"Application Support"/JetBrains
```

All paths support `~` expansion. Keys not present in the file use their defaults.

## Use as a library

Everything the command-line tool does is available as ordinary Python
functions in the `idea_migrate.batch` module. The command-line layer in
`idea_migrate/cli.py` is a thin caller of it: reading arguments, asking the
confirmation question, printing the report and turning an error into an exit
code. Every decision about what a move implies, and every step that changes
something, lives in `batch`.

Nothing in `batch` prints. It returns values and raises exceptions, and
progress is reported only if you ask for it, through a callback of your own.
That is what makes it usable from a program that has its own idea of what
output should look like, or from no terminal at all.

### Planning several moves at once

`plan_moves` decides what a batch of moves implies without touching anything.
It takes a list of `(source, destination)` pairs, the configuration, and the
home directory to interpret paths against:

```python
from pathlib import Path

from idea_migrate.batch import plan_moves
from idea_migrate.config import default_config

home = Path.home()
plan = plan_moves(
    [
        ("~/WebstormProjects", "~/Projects/WebstormProjects"),
        ("~/GolandProjects", "~/Projects/GolandProjects"),
    ],
    default_config(home),
    home,
)
```

Sources and destinations may be strings or `Path` objects, and `~` is
expanded for you. If a destination sits inside folders that do not exist yet,
pass `create_parents=True` and the run will create them — outermost first, and
only the ones that were genuinely missing, so that an undo can remove exactly
those again and only while they are still empty. Without that flag a missing
parent folder is a reason to refuse the move.

The plan that comes back describes the whole batch: `plan.moves` holds one
entry per move, in the order you gave them, each carrying how many stored path
references point at that source, which configuration files would change, which
Claude Code entries would be renamed and which of its files rewritten.
`plan.total_references` is the sum across every move.

### Every problem at once, before anything is touched

Planning applies the same guards the single-move command applies, and applies
them to the batch as a whole. The check for a running JetBrains IDE is made
once, because it is a fact about the machine rather than about a move. Every
pair of moves is checked against every other for overlap — two sources where
one contains the other, two destinations that collide, a destination inside
another move's source, and so on — because moves that individually look fine
can still be impossible together. Each move then gets its own trial run of
every rewrite it would perform, which is what proves that the IDE settings
files, the Claude Code transcripts and the Claude Code registry would all still
be valid afterwards.

If anything is wrong, `plan_moves` raises `PlanError`, and `PlanError.problems`
is the list of every problem found rather than just the first:

```python
from idea_migrate.errors import PlanError

try:
    plan = plan_moves(pairs, config, home)
except PlanError as exc:
    for problem in exc.problems:
        print(problem)
```

Someone fixing five destinations learns about all five now instead of
rediscovering the next one on each attempt. The exception's own message is
those problems joined by newlines, so a batch of one still reads as the single
line that one problem would have produced on its own.

### Executing a plan

`BatchRun` carries a plan out. Constructing it touches nothing; `execute()`
does the work, once — a second call is not supported, because the plan it holds
describes the machine as it was before the first one.

```python
from datetime import datetime

from idea_migrate.batch import BatchRun

run = BatchRun(plan, now=datetime.now(), progress=print)
result = run.execute()

print(result.backup_dir)      # where everything was saved beforehand
for move in result.moves:     # one entry per move, in the order they were made
    print(move.plan.source, "->", move.plan.dest, move.rewritten_files)
```

The `now` argument is the timestamp the backup directory is named after and
recorded with, and it is required rather than read from the clock so that a
caller can control it. The `progress` argument is optional: give it any
function taking one string and it will be called at the start of each phase
that takes a while — backing up, moving a directory, repairing references,
relocating Claude Code data, scanning for hardcoded paths. Leave it out and the
run says nothing at all.

If a move fails part-way through, `execute()` raises `RunFailed`. Its message
is the underlying failure's own message, `RunFailed.cause` is that underlying
exception, and `RunFailed.state` says how far the run got:

```python
from idea_migrate.errors import RunFailed

try:
    result = run.execute()
except RunFailed as exc:
    state = exc.state
    print(state.backup_dir)      # the backup, or None if it was never taken
    print(len(state.completed))  # moves that finished
    print(state.failed)          # the move that was in progress, or None
    print(state.pending)         # moves that were never started
    if state.anything_moved:
        print("a directory is no longer where it started")
```

`state.anything_moved` is worked out from the filesystem rather than from the
kind of error, because an interruption such as Ctrl-C loses a directory just as
surely as a failed rewrite does. Interruptions and unexpected bugs are not
wrapped in `RunFailed` — they propagate unchanged — but the state is filled in
before they do, so `run.state` is still worth reading in that case too.

### One backup, one undo, however many moves

A run takes a single backup, whether it makes one move or six. That backup
directory holds one `manifest.json` recording every move in the run, the copied
IDE configuration files, copies of the Claude Code files that were rewritten,
and one standalone `undo.sh`. The manifest is updated as each move completes,
so a run that stopped half way through still leaves an accurate record.

Undoing reverses every move in the run, in the reverse of the order they were
made, which is what makes a batch safe to unwind: a move made possible by an
earlier one is undone before that earlier one is. The `undo.sh` in the backup
and the `idea-migrate undo` command both do this, and so does `undo_run` from
Python:

```python
from datetime import datetime
from pathlib import Path

from idea_migrate.batch import undo_run

result = undo_run(Path("~/Idea-Migration-Backups/2026-08-29_143005").expanduser(),
                  config, datetime.now())
for line in result.actions:
    print(line)
```

`result.actions` is the list of lines describing what was restored — the same
lines the `idea-migrate undo` command prints, returned rather than printed.
`undo_run` works out for itself which IDE settings directory to restore into,
by reading the location recorded in the backup's manifest, so a migration run
with a non-standard `jetbrains_root` does not have to be undone with the same
`--config` flag.

These names are imported from `idea_migrate.batch` rather than from
`idea_migrate` itself. The package's top level deliberately re-exports nothing,
so that there is one place to look for the batch API and one obvious import
line for it, and so that importing the package stays cheap for anything that
only wants the version number.

## Running the tests

From the repository root:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t . -v
```

## Other details

Installing with `pip install -e .` (from the repository root) puts an
`idea-migrate` command on your `PATH`, so the examples in the README that
call `idea-migrate` directly also work as-is once installed.

Checking the version:

```bash
idea-migrate --version
```

Cancelling: pressing Ctrl-C at the confirmation prompt exits with status 130
and changes nothing. At the prompt itself, only `y` or `yes` continues —
anything else, including pressing Return or reaching end of input in a
script, cancels with nothing changed.

Every backup also includes a `manifest.json` file that tracks what was
changed — including the Claude Code project entries that were renamed and
the files that were rewritten — alongside the copied IDE configuration
files, copies of the rewritten Claude Code files under `claude/`, and
`undo.sh`. When a run also rewrote the Claude Code per-project registry
file, the backup additionally holds a copy of it, named `claude-registry.json`.
