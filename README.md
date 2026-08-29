# idea-migrate

Moves a JetBrains project directory to a new location and repairs the stored path references in every installed IDE so Recent Projects keeps working. The tool does this safely by backing up the IDE configuration before making any changes, refusing to run while any IDE is open, and providing an undo command for any run.

## Requirements

macOS. Python 3.11 or newer. No third-party dependencies.

## Install

From the repository root:

```bash
pip install -e .
```

To run without installing, use:

```bash
PYTHONPATH=src python3 -m idea_migrate
```

All examples below assume the tool is installed; adjust the command to `PYTHONPATH=src python3 -m idea_migrate` if running without installation.

## Usage

The tool supports four invocation shapes, each explained below.

Migrate a directory with explicit paths:

```bash
idea-migrate --source ~/WebstormProjects --dest ~/Projects/WebstormProjects
```

Migrate with prompts for both paths, which offer tab completion:

```bash
idea-migrate
```

Preview the changes without writing anything:

```bash
idea-migrate --dry-run --source ~/X --dest ~/Y
```

List all previous migrations and their undo commands:

```bash
idea-migrate backups
```

Roll back a previous migration:

```bash
idea-migrate undo ~/Idea-Migration-Backups/2026-08-29_143005
```

## Try one project first

The tool does not assume the source is a whole IDE root. `--source` and `--dest` are just directories, so you can migrate a single project as a trial:

```bash
idea-migrate --source ~/WebstormProjects/my-project --dest ~/Projects/_trial/my-project
```

Because path matching is prefix-based and anchored, this rewrites only the references beginning with that project's path and leaves every other project in that root untouched.

The recommended trial workflow is to migrate one project into a throwaway destination, open the IDE and confirm the project resolves from Recent Projects, then undo the run from its backup. That exercises the whole loop—move, repair, and restore—on real data, and leaves a clean starting point.

Avoid this trap: if you trial a project directly into `~/Projects/WebstormProjects/`, that directory then exists, and the later whole-root migration is refused with "destination already exists"—leaving you to migrate the remaining projects one at a time. Using a throwaway destination and undoing afterwards avoids this.

## Quit your IDEs first

A running JetBrains IDE holds its settings in memory and writes them out when it exits. If any IDE is running when the tool runs, it will quit and overwrite the repairs the tool just made, leaving your project references broken. The tool refuses to run while any IDE is open.

## Backups

Every migration creates a timestamped backup directory under `~/Idea-Migration-Backups`. Each backup contains:

- Copies of the IDE configuration files before the migration (`options/` and `workspace/` subdirectories from each product).
- A standalone `undo.sh` script that reverses the migration.
- A `manifest.json` file that tracks what was changed.

The tool never deletes backups. There is no retention policy and no cleanup flag; all backups persist until you manually remove them.

The `undo.sh` script takes an optional path argument, so it continues to work after the backup folder is moved or renamed:

```bash
~/Idea-Migration-Backups/2026-08-29_143005/undo.sh
# or, if the backup was moved:
~/Idea-Migration-Backups/2026-08-29_143005/undo.sh /new/location/backup
```

If you forget where your backups are, run:

```bash
idea-migrate backups
```

This lists every backup and the undo command to run for each.

## What it does not touch

Projects' own `.idea` directories are not modified. These directories use the `$PROJECT_DIR$` placeholder for paths, which makes them portable—they do not need to be updated when the project moves. You can safely ignore any `.idea` directories in your migrated projects.

IDE caches are keyed by the absolute path to each project. After a migration, each IDE rebuilds its cache for the moved project on first launch. This is expected behavior and appears as a slow re-index of the project files. The cache rebuilds itself automatically; no action is needed.

## Configuration

An optional TOML configuration file lets you override the backup location and exclude specific products from repairs. If no file is specified, all defaults are used.

Pass the configuration file with `--config`:

```bash
idea-migrate --config ~/.idea-migrate.toml --source ~/X --dest ~/Y
```

The configuration file supports these keys:

```toml
# Optional: where to store backups. Defaults to ~/Idea-Migration-Backups.
backup_root = "~/.local/share/idea-backups"

# Optional: which products to skip. Useful if you have a product you never open
# or one that stores problematic references you do not want to touch.
exclude_products = ["GoLand", "RustRover"]
```

All paths support `~` expansion. Keys not present in the file use their defaults.

## Running the tests

From the repository root:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t . -v
```
