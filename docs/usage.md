# Configuration reference and testing

This page holds material that used to live in the README: the optional TOML
configuration file and how to run the test suite. See the top-level
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
changed, alongside the copied IDE configuration files and `undo.sh`.
