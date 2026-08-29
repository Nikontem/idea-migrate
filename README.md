# idea-migrate

Moves a JetBrains project directory to a new location and repairs the stored path references in every installed IDE (IntelliJ IDEA, PyCharm, WebStorm, GoLand, DataGrip) so Recent Projects keeps working. It backs up the IDE configuration before making any changes, refuses to run while any IDE is open, and ships an undo command for every run.

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

## What it does not touch

Projects' own `.idea` directories are untouched — they use the `$PROJECT_DIR$` placeholder for paths, which already makes them portable. IDE caches, by contrast, are keyed by the absolute project path, so expect each IDE to do one slow re-index of the moved project on first launch; this is expected and self-resolving.

## Requirements

macOS, Python 3.11 or newer, no third-party dependencies.

For the optional TOML configuration file and how to run the tests, see [docs/usage.md](docs/usage.md).
