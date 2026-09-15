# idea-migrate

Moves a JetBrains project directory to a new location and repairs the stored path references in every installed IDE (IntelliJ IDEA, PyCharm, WebStorm, GoLand, DataGrip) so Recent Projects keeps working. It also relocates Claude Code's per-project data — session history and memory — so they follow the project to its new location. It backs up the IDE configuration before making any changes, refuses to run while any IDE is open, and ships an undo command for every run.

## Why not just move the folder in Finder

Each IDE records the absolute path of every project it has opened, and it records it *outside* the project, in its own settings directory under `~/Library/Application Support/JetBrains`. Recent Projects, window layout, and run configurations are all keyed by that stored path. Moving the folder in Finder changes where the project is, but nothing tells the IDEs — they keep pointing at a directory that no longer exists, so the project vanishes from Recent Projects or opens onto an empty window. Nothing in the project folder can fix this, because the stale path isn't in the project folder. That's what this tool repairs.

## Usage

No install is required. From the repository root:

```bash
cd ~/Projects/idea-migrate
PYTHONPATH=src python3 -m idea_migrate --dry-run --source ~/WebstormProjects --dest ~/Projects/WebstormProjects
```

`--dry-run` prints the plan — every configuration file that would change, and how many references are in each — and writes nothing. Drop it to do the real move:

```bash
PYTHONPATH=src python3 -m idea_migrate --source ~/WebstormProjects --dest ~/Projects/WebstormProjects
```

`--source` and `--dest` are just directories, so moving a single project works the same way as moving a whole IDE root:

```bash
PYTHONPATH=src python3 -m idea_migrate --source ~/WebstormProjects/my-project --dest ~/Projects/my-project
```

List previous migrations and their undo commands:

```bash
PYTHONPATH=src python3 -m idea_migrate backups
```

Roll back a previous migration:

```bash
PYTHONPATH=src python3 -m idea_migrate undo ~/Idea-Migration-Backups/2026-08-29_143005
```

Run with no flags at all and the tool prompts for both paths, with tab completion. Unless you pass `--yes`, it always prints the plan and waits for you to confirm before writing anything.

If you'd rather not type `PYTHONPATH=src python3 -m idea_migrate` every time and don't want to install the package, a shell function does the job — the tool has no third-party dependencies:

```zsh
idea-migrate() { PYTHONPATH=~/Projects/idea-migrate/src python3 -m idea_migrate "$@"; }
```

## Try one project first

Because path matching is prefix-based and anchored, migrating a single project as a trial rewrites only that project's references and leaves the rest of the root untouched. Migrate one project into a throwaway destination, open the IDE and confirm it resolves from Recent Projects, then undo the run from its backup — that exercises the whole loop on real data and leaves a clean starting point.

One trap: if you trial a project directly into the eventual destination (say, `~/Projects/WebstormProjects/`), that directory then exists, and the later whole-root migration is refused with "destination already exists." Use a throwaway destination and undo afterwards instead.

## Quit your IDEs first

A running JetBrains IDE holds its settings in memory and writes them out when it exits. If an IDE is open while the tool runs, quitting it afterward overwrites the repairs the tool just made and leaves your project references broken again. The tool refuses to run while any IDE is open.

## Backups and undo

Every migration creates a timestamped backup directory under `~/Idea-Migration-Backups`, holding copies of the IDE configuration files from before the migration plus a standalone `undo.sh`. The tool never deletes backups automatically — there's no retention policy or cleanup flag. `undo.sh` doesn't import this package and needs only bash and python3, so it still works even if the tool itself is what broke; it also takes an optional path argument, so it keeps working after the backup folder is moved or renamed. If you forget where your backups are, `idea-migrate backups` lists all of them along with the undo command for each.

## Claude Code project data

Claude Code keeps per-project data — session transcripts and a memory folder — under `~/.claude/projects/<encoded path>/`, where the encoded name is the project's absolute path with every character that isn't a letter or digit replaced by `-`. That encoding is derived from the path, so moving the project without moving this folder orphans it: Claude Code would start a fresh, empty entry for the new location and leave the old history stranded under the old name.

A migration renames the matching entry — and the entry for every directory below the source that has one — to the encoded name of its new location, and repairs the absolute path inside the session transcripts and `~/.claude/history.jsonl`. Both the renames and the rewrites are listed in the dry-run plan, backed up before they happen, and reversed by undo along with everything else.

Claude Code should not be running in the project being moved; the tool does not check for this. Other files under `~/.claude` that merely mention the path in their text — permission rules in `settings.json`, saved plans, memory notes — are left alone.

The migration also rewrites Claude Code's per-project registry file, `~/.claude.json`. This file holds one entry per project, keyed by that project's absolute path, and each entry carries per-project settings such as which tools are allowed and which MCP servers are configured. The tool renames the entry for the moved project — and the entry for every directory beneath it that has one — to its new absolute path, so those settings stay attached to the project instead of being orphaned under the old path. Registry keys that merely share a prefix with the source path, without actually being it or a directory beneath it, are left untouched. Like the rest of Claude Code's data, the registry file is backed up before the run and restored byte-for-byte by undo.

## What it does not touch

Projects' own `.idea` directories are untouched — they use the `$PROJECT_DIR$` placeholder for paths, which already makes them portable. IDE caches, by contrast, are keyed by the absolute project path, so expect each IDE to do one slow re-index of the moved project on first launch; this is expected and self-resolving.

One cache is not self-resolving, and it is moved for you. A build-system project usually keeps its module definitions *outside* its `.idea` directory — `modules.xml`, the `.iml` files and the resolved Maven or Gradle project tree all live under `~/Library/Caches/JetBrains/<Product><version>/projects/`, in a directory named after a hash of the project's absolute path. Move the project and the IDE looks under a different hash, finds nothing, and opens the project with no modules at all: the Maven tool window comes up completely empty, with no Lifecycle and no Plugins. The tool renames that directory to match the new path and repairs the one path recorded inside it. Renaming is reversible on its own, so undo puts it back without needing a copy in the backup.

If a project was moved once already without being reopened in between, its stored modules are keyed to a path two generations old and nothing can be matched. The report says so, and the fix is one action in the IDE: right-click `pom.xml` (or `build.gradle`) and add it as a project.

Claude Code entries whose encoded name merely resembles the source — for example a sibling directory `foo-bar` next to `foo` — are also left alone, and listed separately in the plan.

## Use as a library

The command-line tool is a thin wrapper. All the real work — working out what a move implies, refusing the ones that cannot safely be made, taking the backup, moving the directory and repairing every reference to it — lives in the `idea_migrate.batch` module, and you can call it directly from Python. Nothing in that module prints anything: it returns values and raises exceptions, so it is usable from a script, a test, or a program with its own idea of what output should look like.

Working with it is two steps. `plan_moves` takes a list of `(source, destination)` pairs and answers what those moves would imply, without touching anything on disk. It runs every check first — the paths, whether an IDE is running, whether two of the moves collide, and trial rewrites that prove the IDE settings, the Claude Code transcripts and the Claude Code registry would all survive being changed — and if anything is wrong it raises a `PlanError` listing every problem it found rather than only the first. Then `BatchRun(...).execute()` carries the plan out and tells you what actually happened.

Several moves planned together are made as one run, which means one backup directory, one record of what changed, and one `undo.sh` covering the lot. Undoing that run reverses every move in it, in the reverse of the order they were made.

```python
from datetime import datetime
from pathlib import Path

from idea_migrate.batch import BatchRun, plan_moves
from idea_migrate.config import default_config
from idea_migrate.errors import PlanError, RunFailed

home = Path.home()
config = default_config(home)

try:
    plan = plan_moves(
        [
            ("~/WebstormProjects", "~/Projects/WebstormProjects"),
            ("~/GolandProjects", "~/Projects/GolandProjects"),
        ],
        config,
        home,
    )
except PlanError as exc:
    for problem in exc.problems:
        print(problem)
else:
    try:
        result = BatchRun(plan, now=datetime.now()).execute()
    except RunFailed as exc:
        print(f"stopped after {len(exc.state.completed)} moves: {exc}")
    else:
        print(f"backup saved to {result.backup_dir}")
```

There is more detail — the progress callback, creating destination folders that do not exist yet, and undoing a run from Python — in [docs/usage.md](docs/usage.md).

## Requirements

macOS, Python 3.11 or newer, no third-party dependencies.

For the optional TOML configuration file and how to run the tests, see [docs/usage.md](docs/usage.md).
