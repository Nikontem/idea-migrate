# idea-migrate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a command-line tool that moves a JetBrains project directory to a new location and repairs every stored path reference in the installed IDEs, with a non-deleting backup and a working undo.

**Architecture:** A Python package with one module per responsibility, wired together by a thin command-line layer. The user gives a source and a destination; the tool backs up all IDE configuration, moves the directory with an atomic rename, then does boundary-anchored case-insensitive text replacement across the IDEs' XML configuration files, verifying each result parses as XML before writing it.

**Tech Stack:** Python 3.11+ (3.14.3 installed), standard library only. Tests use `unittest` from the standard library.

**Spec:** `docs/superpowers/specs/2026-08-29-jetbrains-project-relocation-design.md`

## Global Constraints

- Python 3.11 or newer. The package imports **only** from the standard library — no third-party dependencies at runtime or in tests.
- Tests use `unittest`. Run the whole suite with `python3 -m unittest discover -s tests -t . -v`. Do **not** use pytest: it is installed at `/opt/homebrew/bin/pytest` but is not importable from the active `python3` (a pyenv shim at `~/.pyenv/shims/python3`), so `python3 -m pytest` fails.
- No test may read or write the real home directory, the real `~/Library/Application Support/JetBrains/`, or the real `~/Idea-Migration-Backups`. Every test builds a synthetic tree under `tempfile.TemporaryDirectory()`. Any function that touches those locations takes the root as a parameter so tests can redirect it.
- The tool **never deletes a backup**. No retention policy, no pruning, no cleanup flag.
- Nothing writes without explicit user confirmation, and `--dry-run` never writes.
- Default backup root is `~/Idea-Migration-Backups`. Default JetBrains root is `~/Library/Application Support/JetBrains`.
- Package lives at `src/idea_migrate/`. Tests live at `tests/`.
- Every task ends with a commit. Commit messages use the `feat:` / `test:` / `docs:` prefix shown in the task.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/idea_migrate/errors.py` | Exception hierarchy |
| `src/idea_migrate/config.py` | `Config` dataclass and defaults |
| `src/idea_migrate/products.py` | Locate installed JetBrains product configuration directories |
| `src/idea_migrate/processes.py` | Detect running JetBrains IDEs |
| `src/idea_migrate/paths.py` | Validate a source/destination pair into a `MoveSpec` |
| `src/idea_migrate/completion.py` | Readline path completion and interactive prompting |
| `src/idea_migrate/rewrite.py` | Path prefix variants, anchored replacement, XML verification |
| `src/idea_migrate/manifest.py` | Manifest dataclass, read and write |
| `src/idea_migrate/backup.py` | Copy configuration, generate `undo.sh` |
| `src/idea_migrate/mover.py` | Preflight and the move itself |
| `src/idea_migrate/undo.py` | The `undo` command |
| `src/idea_migrate/listing.py` | The `backups` command |
| `src/idea_migrate/report.py` | Final output formatting |
| `src/idea_migrate/cli.py` | Argument parsing and orchestration |
| `src/idea_migrate/__main__.py` | Entry point |

---

### Task 1: Project scaffolding, errors, and config

**Model:** Sonnet 5

**Files:**
- Create: `pyproject.toml`
- Create: `src/idea_migrate/__init__.py`
- Create: `src/idea_migrate/errors.py`
- Create: `src/idea_migrate/config.py`
- Create: `tests/__init__.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `idea_migrate.errors.MigrateError` (base), `PathValidationError`, `IdeRunningError`, `XmlIntegrityError`, `BackupError`, `UndoError` — all subclasses of `MigrateError`
  - `idea_migrate.config.Config` — frozen dataclass with fields `backup_root: Path`, `jetbrains_root: Path`, `exclude_products: tuple[str, ...]`
  - `idea_migrate.config.default_config(home: Path) -> Config`
  - `idea_migrate.config.load_config(path: Path | None, home: Path) -> Config`
  - `idea_migrate.__version__: str`

- [ ] **Step 1: Write the failing test**

Create `tests/__init__.py` as an empty file, then `tests/test_config.py`:

```python
import tempfile
import unittest
from pathlib import Path

from idea_migrate.config import Config, default_config, load_config


class TestDefaultConfig(unittest.TestCase):
    def test_defaults_derive_from_home(self):
        home = Path("/fake/home")
        cfg = default_config(home)
        self.assertEqual(cfg.backup_root, home / "Idea-Migration-Backups")
        self.assertEqual(
            cfg.jetbrains_root,
            home / "Library" / "Application Support" / "JetBrains",
        )
        self.assertEqual(cfg.exclude_products, ())

    def test_config_is_frozen(self):
        cfg = default_config(Path("/fake/home"))
        with self.assertRaises(Exception):
            cfg.backup_root = Path("/elsewhere")


class TestLoadConfig(unittest.TestCase):
    def test_none_path_returns_defaults(self):
        home = Path("/fake/home")
        self.assertEqual(load_config(None, home), default_config(home))

    def test_missing_file_returns_defaults(self):
        home = Path("/fake/home")
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.toml"
            self.assertEqual(load_config(missing, home), default_config(home))

    def test_file_overrides_defaults(self):
        home = Path("/fake/home")
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "config.toml"
            cfg_file.write_text(
                'backup_root = "/custom/backups"\n'
                'exclude_products = ["Toolbox", "Air"]\n',
                encoding="utf-8",
            )
            cfg = load_config(cfg_file, home)
            self.assertEqual(cfg.backup_root, Path("/custom/backups"))
            self.assertEqual(cfg.exclude_products, ("Toolbox", "Air"))
            # Unset keys keep their defaults.
            self.assertEqual(
                cfg.jetbrains_root,
                home / "Library" / "Application Support" / "JetBrains",
            )

    def test_tilde_in_config_is_expanded(self):
        home = Path("/fake/home")
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "config.toml"
            cfg_file.write_text('backup_root = "~/my-backups"\n', encoding="utf-8")
            cfg = load_config(cfg_file, home)
            self.assertEqual(cfg.backup_root, Path.home() / "my-backups")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_config -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'idea_migrate'`

- [ ] **Step 3: Write the implementation**

`pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "idea-migrate"
version = "0.1.0"
description = "Move JetBrains project directories and repair the IDEs' stored path references."
requires-python = ">=3.11"
dependencies = []

[project.scripts]
idea-migrate = "idea_migrate.cli:main"

[tool.setuptools.packages.find]
where = ["src"]
```

`src/idea_migrate/__init__.py`:

```python
"""Move JetBrains project directories and repair the IDEs' stored path references."""

__version__ = "0.1.0"
```

`src/idea_migrate/errors.py`:

```python
"""Exception hierarchy for idea-migrate.

Every error the tool raises deliberately derives from MigrateError so the
command-line layer can catch one class and print a clean message instead of a
traceback.
"""


class MigrateError(Exception):
    """Base class for every error this tool raises deliberately."""


class PathValidationError(MigrateError):
    """The source or destination path is unusable."""


class IdeRunningError(MigrateError):
    """A JetBrains IDE is running, so its configuration must not be touched."""


class XmlIntegrityError(MigrateError):
    """Rewriting a file would have produced invalid XML."""


class BackupError(MigrateError):
    """The backup could not be created."""


class UndoError(MigrateError):
    """A backup could not be rolled back."""
```

`src/idea_migrate/config.py`:

```python
"""Configuration: where backups go and where the IDEs keep their settings.

Every setting has a working default, so the TOML file is optional.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

BACKUP_DIR_NAME = "Idea-Migration-Backups"


@dataclass(frozen=True)
class Config:
    backup_root: Path
    jetbrains_root: Path
    exclude_products: tuple[str, ...]


def default_config(home: Path) -> Config:
    """Return the configuration used when no file overrides anything."""
    return Config(
        backup_root=home / BACKUP_DIR_NAME,
        jetbrains_root=home / "Library" / "Application Support" / "JetBrains",
        exclude_products=(),
    )


def load_config(path: Path | None, home: Path) -> Config:
    """Load configuration from a TOML file, falling back to defaults.

    A missing file is not an error - the file is optional. Keys absent from the
    file keep their default values.
    """
    config = default_config(home)
    if path is None or not path.is_file():
        return config

    with path.open("rb") as handle:
        data = tomllib.load(handle)

    changes: dict[str, object] = {}
    if "backup_root" in data:
        changes["backup_root"] = Path(data["backup_root"]).expanduser()
    if "jetbrains_root" in data:
        changes["jetbrains_root"] = Path(data["jetbrains_root"]).expanduser()
    if "exclude_products" in data:
        changes["exclude_products"] = tuple(data["exclude_products"])

    return replace(config, **changes)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_config -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/idea-migrate
git add pyproject.toml src/idea_migrate/__init__.py src/idea_migrate/errors.py src/idea_migrate/config.py tests/__init__.py tests/test_config.py
git commit -m "feat: add project scaffolding, error hierarchy, and config loading"
```

---

### Task 2: Locate installed JetBrains product directories

**Model:** Sonnet 5

**Files:**
- Create: `src/idea_migrate/products.py`
- Test: `tests/test_products.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `idea_migrate.products.find_product_dirs(jetbrains_root: Path, exclude: Sequence[str] = ()) -> list[Path]` — returns directories sorted by name, each containing an `options` subdirectory

**Context:** A directory under the JetBrains root is a product configuration directory if and only if it contains an `options` subdirectory. This structural test is deliberate: it picks up newly installed products and versions automatically, and it excludes support directories like `Toolbox`, `consentOptions`, and `PrivacyPolicy` without needing to name them.

- [ ] **Step 1: Write the failing test**

`tests/test_products.py`:

```python
import tempfile
import unittest
from pathlib import Path

from idea_migrate.products import find_product_dirs


def build_jetbrains_root(base: Path) -> Path:
    """Build a synthetic JetBrains configuration root."""
    root = base / "JetBrains"
    for name in ("IntelliJIdea2026.2", "PyCharm2026.2", "WebStorm2025.3"):
        (root / name / "options").mkdir(parents=True)
        (root / name / "workspace").mkdir(parents=True)
    # Support directories with no options/ subdirectory - must be ignored.
    (root / "Toolbox").mkdir(parents=True)
    (root / "consentOptions").mkdir(parents=True)
    (root / "PrivacyPolicy" / "nested").mkdir(parents=True)
    # A stray file at the top level - must not crash the scan.
    (root / "stray.txt").write_text("ignore me", encoding="utf-8")
    return root


class TestFindProductDirs(unittest.TestCase):
    def test_finds_only_dirs_containing_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_jetbrains_root(Path(tmp))
            found = [p.name for p in find_product_dirs(root)]
            self.assertEqual(
                found, ["IntelliJIdea2026.2", "PyCharm2026.2", "WebStorm2025.3"]
            )

    def test_results_are_sorted_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_jetbrains_root(Path(tmp))
            found = [p.name for p in find_product_dirs(root)]
            self.assertEqual(found, sorted(found))

    def test_exclude_removes_named_products(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_jetbrains_root(Path(tmp))
            found = [p.name for p in find_product_dirs(root, exclude=["PyCharm2026.2"])]
            self.assertEqual(found, ["IntelliJIdea2026.2", "WebStorm2025.3"])

    def test_missing_root_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(find_product_dirs(Path(tmp) / "absent"), [])

    def test_returns_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_jetbrains_root(Path(tmp))
            for product in find_product_dirs(root):
                self.assertTrue(product.is_absolute())
                self.assertTrue((product / "options").is_dir())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_products -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'idea_migrate.products'`

- [ ] **Step 3: Write the implementation**

`src/idea_migrate/products.py`:

```python
"""Find the configuration directories of installed JetBrains products.

A directory counts as a product configuration directory when it contains an
``options`` subdirectory. That structural test is deliberate: it picks up newly
installed products and versions without a hardcoded name list, and it skips
support directories such as Toolbox, consentOptions, and PrivacyPolicy.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


def find_product_dirs(
    jetbrains_root: Path, exclude: Sequence[str] = ()
) -> list[Path]:
    """Return every product configuration directory under ``jetbrains_root``.

    Results are absolute and sorted by directory name. A missing root is not an
    error - it simply means no JetBrains products are installed.
    """
    if not jetbrains_root.is_dir():
        return []

    excluded = set(exclude)
    products = [
        entry
        for entry in jetbrains_root.iterdir()
        if entry.is_dir()
        and entry.name not in excluded
        and (entry / "options").is_dir()
    ]
    return sorted((p.resolve() for p in products), key=lambda p: p.name)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_products -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/idea-migrate
git add src/idea_migrate/products.py tests/test_products.py
git commit -m "feat: locate installed JetBrains product config directories"
```

---

### Task 3: Detect running JetBrains IDEs

**Model:** Sonnet 5

**Files:**
- Create: `src/idea_migrate/processes.py`
- Test: `tests/test_processes.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `idea_migrate.processes.running_ides(ps_output: str | None = None) -> list[str]` — returns sorted unique IDE display names currently running

**Context:** A running IDE holds its configuration in memory and writes it out on exit, which would silently overwrite our repairs. `ps -Ao comm=` prints one executable path per line, for example `/Applications/IntelliJ IDEA.app/Contents/MacOS/idea`. The `ps_output` parameter exists so tests can supply fixture text instead of inspecting the real process table.

JetBrains Toolbox must **not** count as a running IDE — it is an installer and updater that many people leave running permanently, and blocking on it would make the tool unusable.

- [ ] **Step 1: Write the failing test**

`tests/test_processes.py`:

```python
import unittest

from idea_migrate.processes import running_ides

SAMPLE_PS = """\
/System/Library/CoreServices/loginwindow.app/Contents/MacOS/loginwindow
/Applications/IntelliJ IDEA.app/Contents/MacOS/idea
/Applications/Firefox.app/Contents/MacOS/firefox
/Users/someone/Applications/PyCharm.app/Contents/MacOS/pycharm
/usr/sbin/cfprefsd
"""

TOOLBOX_ONLY = """\
/Applications/JetBrains Toolbox.app/Contents/MacOS/jetbrains-toolbox
/usr/sbin/cfprefsd
"""

NO_IDES = """\
/System/Library/CoreServices/loginwindow.app/Contents/MacOS/loginwindow
/usr/sbin/cfprefsd
"""


class TestRunningIdes(unittest.TestCase):
    def test_detects_running_ides(self):
        self.assertEqual(running_ides(SAMPLE_PS), ["IntelliJ IDEA", "PyCharm"])

    def test_ignores_unrelated_processes(self):
        self.assertEqual(running_ides(NO_IDES), [])

    def test_toolbox_is_not_an_ide(self):
        self.assertEqual(running_ides(TOOLBOX_ONLY), [])

    def test_deduplicates_repeated_processes(self):
        doubled = SAMPLE_PS + "/Applications/IntelliJ IDEA.app/Contents/MacOS/idea\n"
        self.assertEqual(running_ides(doubled), ["IntelliJ IDEA", "PyCharm"])

    def test_detects_toolbox_installed_ide_paths(self):
        toolbox_path = (
            "/Users/someone/Library/Application Support/JetBrains/Toolbox/apps/"
            "goland/GoLand.app/Contents/MacOS/goland\n"
        )
        self.assertEqual(running_ides(toolbox_path), ["GoLand"])

    def test_empty_output_is_safe(self):
        self.assertEqual(running_ides(""), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_processes -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'idea_migrate.processes'`

- [ ] **Step 3: Write the implementation**

`src/idea_migrate/processes.py`:

```python
"""Detect running JetBrains IDEs.

A running IDE holds its configuration in memory and flushes it on exit, which
would silently undo our repairs. So the tool refuses to run while one is alive.

JetBrains Toolbox is deliberately excluded: it is an installer and updater that
many people leave running permanently, and treating it as an IDE would block the
tool for no reason.
"""

from __future__ import annotations

import re
import subprocess

# Maps the executable basename inside the app bundle to the product's display
# name. Matching the basename rather than the bundle name keeps this working for
# IDEs installed through Toolbox, whose bundle paths are deeply nested.
IDE_EXECUTABLES = {
    "idea": "IntelliJ IDEA",
    "pycharm": "PyCharm",
    "webstorm": "WebStorm",
    "goland": "GoLand",
    "datagrip": "DataGrip",
    "clion": "CLion",
    "phpstorm": "PhpStorm",
    "rubymine": "RubyMine",
    "rider": "Rider",
    "rustrover": "RustRover",
}

_EXECUTABLE_PATTERN = re.compile(
    r"/(?P<name>" + "|".join(IDE_EXECUTABLES) + r")$", re.IGNORECASE
)


def _read_process_table() -> str:
    """Return one executable path per line for every running process."""
    result = subprocess.run(
        ["ps", "-Ao", "comm="],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def running_ides(ps_output: str | None = None) -> list[str]:
    """Return the display names of JetBrains IDEs that are currently running.

    ``ps_output`` accepts pre-captured ``ps -Ao comm=`` text so tests can run
    against fixtures instead of the real process table.
    """
    if ps_output is None:
        ps_output = _read_process_table()

    found: set[str] = set()
    for line in ps_output.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _EXECUTABLE_PATTERN.search(line)
        if match:
            found.add(IDE_EXECUTABLES[match.group("name").lower()])
    return sorted(found)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_processes -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/idea-migrate
git add src/idea_migrate/processes.py tests/test_processes.py
git commit -m "feat: detect running JetBrains IDEs from the process table"
```

---

### Task 4: Validate the source and destination pair

**Model:** Opus 5

**Files:**
- Create: `src/idea_migrate/paths.py`
- Test: `tests/test_paths.py`

**Interfaces:**
- Consumes: `idea_migrate.errors.PathValidationError`
- Produces:
  - `idea_migrate.paths.MoveSpec` — frozen dataclass with fields `source: Path`, `dest: Path`, `home: Path`, `same_device: bool`
  - `idea_migrate.paths.validate_move(source: str | Path, dest: str | Path, home: Path) -> MoveSpec`

**Context — read carefully, this task carries the dangerous checks:**

The destination-inside-source check is the one that prevents catastrophe: `--source ~/Projects --dest ~/Projects/sub` would otherwise try to move a directory into itself.

The filesystem is case-insensitive, so string comparison is not a reliable way to ask "are these the same directory". Where both paths exist, compare with `os.path.samefile`, which asks the filesystem. Where the destination does not exist yet (which is always), walk its ancestors and compare each *existing* ancestor with `samefile`.

Checks run in this order, each raising `PathValidationError` with a message naming the offending path:

1. Source exists and is a directory.
2. Source is not the home directory itself.
3. Source and destination are not the same directory. This is checked by comparing the two path strings case-insensitively, and it must come **before** the existence check below. Otherwise `--source ~/PycharmProjects --dest ~/PyCharmProjects` — two spellings of one real directory — fails with the confusing message "destination already exists" instead of saying they are the same directory.
4. Destination does not already exist.
5. Destination's parent exists, is a directory, and is writable.
6. Destination is not inside the source.
7. Record whether source and destination are on the same filesystem device.

The design spec also listed "source is not inside the destination" as a separate rule. It is not implementable as a distinct check: the destination never exists at validation time, so the only concrete cases it could catch are already covered by rules 3, 4, and 6. It is deliberately omitted rather than implemented as something that can never fire.

- [ ] **Step 1: Write the failing test**

`tests/test_paths.py`:

```python
import os
import tempfile
import unittest
from pathlib import Path

from idea_migrate.errors import PathValidationError
from idea_migrate.paths import MoveSpec, validate_move


class PathTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        self.source = self.home / "WebstormProjects"
        (self.source / "a-project").mkdir(parents=True)
        self.dest_parent = self.home / "Projects"
        self.dest_parent.mkdir()
        self.dest = self.dest_parent / "WebstormProjects"

    def tearDown(self):
        self._tmp.cleanup()


class TestValidMove(PathTestCase):
    def test_returns_a_move_spec(self):
        spec = validate_move(self.source, self.dest, self.home)
        self.assertIsInstance(spec, MoveSpec)
        self.assertTrue(os.path.samefile(spec.source, self.source))
        self.assertEqual(spec.dest, self.dest)
        self.assertTrue(spec.same_device)

    def test_expands_tilde_and_relative_input(self):
        spec = validate_move(str(self.source), str(self.dest), self.home)
        self.assertTrue(spec.source.is_absolute())
        self.assertTrue(spec.dest.is_absolute())


class TestRejections(PathTestCase):
    def test_missing_source(self):
        with self.assertRaises(PathValidationError) as ctx:
            validate_move(self.home / "absent", self.dest, self.home)
        self.assertIn("does not exist", str(ctx.exception))

    def test_source_is_a_file(self):
        a_file = self.home / "a-file.txt"
        a_file.write_text("x", encoding="utf-8")
        with self.assertRaises(PathValidationError) as ctx:
            validate_move(a_file, self.dest, self.home)
        self.assertIn("not a directory", str(ctx.exception))

    def test_source_is_home(self):
        with self.assertRaises(PathValidationError) as ctx:
            validate_move(self.home, self.dest, self.home)
        self.assertIn("home directory", str(ctx.exception))

    def test_destination_already_exists(self):
        self.dest.mkdir()
        with self.assertRaises(PathValidationError) as ctx:
            validate_move(self.source, self.dest, self.home)
        self.assertIn("already exists", str(ctx.exception))

    def test_destination_parent_missing(self):
        with self.assertRaises(PathValidationError) as ctx:
            validate_move(self.source, self.home / "nope" / "here", self.home)
        self.assertIn("does not exist", str(ctx.exception))

    def test_destination_inside_source_is_rejected(self):
        inside = self.source / "nested" / "target"
        (self.source / "nested").mkdir()
        with self.assertRaises(PathValidationError) as ctx:
            validate_move(self.source, inside, self.home)
        self.assertIn("inside", str(ctx.exception))

    def test_same_directory_is_rejected(self):
        with self.assertRaises(PathValidationError) as ctx:
            validate_move(self.source, self.source, self.home)
        self.assertIn("same directory", str(ctx.exception))

    def test_same_directory_under_a_different_capitalization_is_rejected(self):
        # The filesystem is case-insensitive, so these name one real directory.
        # This must report "same directory", not "already exists".
        other_case = self.home / "webstormprojects"
        with self.assertRaises(PathValidationError) as ctx:
            validate_move(self.source, other_case, self.home)
        self.assertIn("same directory", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_paths -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'idea_migrate.paths'`

- [ ] **Step 3: Write the implementation**

`src/idea_migrate/paths.py`:

```python
"""Validate a source and destination pair before anything is moved.

The filesystem on macOS is case-insensitive, so comparing path strings is not a
reliable way to ask whether two paths name the same directory. Wherever both
paths exist, this module asks the filesystem instead, via os.path.samefile.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import PathValidationError


@dataclass(frozen=True)
class MoveSpec:
    source: Path
    dest: Path
    home: Path
    same_device: bool


def _absolute(value: str | Path) -> Path:
    """Expand ``~`` and make the path absolute without requiring it to exist."""
    return Path(os.path.expanduser(str(value))).absolute()


def _same_dir(left: Path, right: Path) -> bool:
    """True when both paths exist and name the same directory."""
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _is_inside(inner: Path, outer: Path) -> bool:
    """True when ``inner`` is at or below ``outer``.

    ``inner`` need not exist. Each of its existing ancestors is compared against
    ``outer`` with samefile, so a case-insensitive filesystem cannot hide a
    match behind a different spelling.
    """
    candidate = inner
    while True:
        if candidate.exists() and _same_dir(candidate, outer):
            return True
        parent = candidate.parent
        if parent == candidate:
            return False
        candidate = parent


def validate_move(source: str | Path, dest: str | Path, home: Path) -> MoveSpec:
    """Check a move is safe and return the validated specification.

    Raises PathValidationError, naming the offending path, on the first problem.
    """
    src = _absolute(source)
    dst = _absolute(dest)
    home = _absolute(home)

    if not src.exists():
        raise PathValidationError(f"Source does not exist: {src}")
    if not src.is_dir():
        raise PathValidationError(f"Source is not a directory: {src}")
    if _same_dir(src, home):
        raise PathValidationError(
            f"Refusing to move the home directory itself: {src}"
        )
    # Checked before the existence test below: on a case-insensitive filesystem
    # two spellings of one directory would otherwise be reported as
    # "destination already exists", which tells the user nothing useful.
    if str(src).lower() == str(dst).lower():
        raise PathValidationError(
            f"Source and destination are the same directory: {src}"
        )
    if dst.exists():
        raise PathValidationError(f"Destination already exists: {dst}")
    if not dst.parent.is_dir():
        raise PathValidationError(
            f"Destination's parent directory does not exist: {dst.parent}"
        )
    if not os.access(dst.parent, os.W_OK):
        raise PathValidationError(
            f"Destination's parent directory is not writable: {dst.parent}"
        )
    if _is_inside(dst, src):
        raise PathValidationError(
            f"Destination {dst} is inside the source {src}; "
            "a directory cannot be moved into itself."
        )

    same_device = os.stat(src).st_dev == os.stat(dst.parent).st_dev
    return MoveSpec(source=src, dest=dst, home=home, same_device=same_device)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_paths -v`
Expected: PASS, 10 tests

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/idea-migrate
git add src/idea_migrate/paths.py tests/test_paths.py
git commit -m "feat: validate source and destination pair before moving"
```

---

### Task 5: Readline path completion

**Model:** Opus 5

**Files:**
- Create: `src/idea_migrate/completion.py`
- Test: `tests/test_completion.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces:
  - `idea_migrate.completion.path_candidates(text: str, only_dirs: bool = True) -> list[str]`
  - `idea_migrate.completion.PathCompleter` with method `complete(text: str, state: int) -> str | None`
  - `idea_migrate.completion.enable_path_completion() -> bool` — returns False when readline is unavailable
  - `idea_migrate.completion.prompt_for_path(prompt: str) -> Path`

**Context — three details that make or break this:**

**Delimiters.** By default readline treats `/` and `~` as word delimiters, so the completer would receive only the fragment after the last slash and completions would corrupt the typed path. Call `readline.set_completer_delims(" \t\n")` so the whole path arrives as one token.

**Two readline implementations, two bindings.** GNU readline binds Tab with `readline.parse_and_bind("tab: complete")`. The libedit build shipped with some macOS Pythons needs `readline.parse_and_bind("bind ^I rl_complete")` and silently does nothing with the GNU form. Detect by checking whether `"libedit"` appears in `readline.__doc__`. The target machine has GNU readline; the tool must not break on libedit.

**Preserve the typed prefix.** If the user typed `~/Webst`, the completion must come back as `~/WebstormProjects/`, not `/Users/someone/WebstormProjects/`. Expand `~` only to search the filesystem, then re-attach the directory portion exactly as the user typed it.

Completions get a trailing `/` so the user can keep typing deeper without retyping the separator. Matching is case-insensitive, matching the filesystem's own behaviour.

- [ ] **Step 1: Write the failing test**

`tests/test_completion.py`:

```python
import os
import tempfile
import unittest
from pathlib import Path

from idea_migrate.completion import PathCompleter, path_candidates


class CompletionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        (self.base / "WebstormProjects").mkdir()
        (self.base / "WebstormProjectsArchive").mkdir()
        (self.base / "IdeaProjects").mkdir()
        (self.base / "notes.txt").write_text("x", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()


class TestPathCandidates(CompletionTestCase):
    def test_completes_a_partial_final_component(self):
        found = path_candidates(f"{self.base}/Webst")
        self.assertEqual(
            sorted(found),
            [
                f"{self.base}/WebstormProjects/",
                f"{self.base}/WebstormProjectsArchive/",
            ],
        )

    def test_completions_end_with_a_separator(self):
        for candidate in path_candidates(f"{self.base}/Idea"):
            self.assertTrue(candidate.endswith("/"))

    def test_files_are_excluded_when_only_dirs(self):
        found = path_candidates(f"{self.base}/not", only_dirs=True)
        self.assertEqual(found, [])

    def test_files_are_included_when_not_only_dirs(self):
        found = path_candidates(f"{self.base}/not", only_dirs=False)
        self.assertEqual(found, [f"{self.base}/notes.txt"])

    def test_matching_is_case_insensitive(self):
        found = path_candidates(f"{self.base}/webst")
        self.assertEqual(len(found), 2)

    def test_empty_component_lists_directory_contents(self):
        found = path_candidates(f"{self.base}/")
        self.assertEqual(len(found), 3)

    def test_tilde_prefix_is_preserved_in_output(self):
        home = Path.home()
        found = path_candidates("~/")
        self.assertTrue(all(c.startswith("~/") for c in found))
        # And they correspond to real entries in the home directory.
        names = {c[len("~/"):].rstrip("/") for c in found}
        actual = {p.name for p in home.iterdir() if p.is_dir()}
        self.assertTrue(names.issubset(actual))

    def test_nonexistent_directory_returns_empty(self):
        self.assertEqual(path_candidates(f"{self.base}/absent/xyz"), [])


class TestPathCompleter(CompletionTestCase):
    def test_state_iterates_then_returns_none(self):
        completer = PathCompleter()
        first = completer.complete(f"{self.base}/Webst", 0)
        second = completer.complete(f"{self.base}/Webst", 1)
        third = completer.complete(f"{self.base}/Webst", 2)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(third)
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_completion -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'idea_migrate.completion'`

- [ ] **Step 3: Write the implementation**

`src/idea_migrate/completion.py`:

```python
"""Terminal-style tab completion for filesystem paths.

Three details matter here, and each one silently breaks completion if wrong:

* readline treats "/" and "~" as word delimiters by default, so the completer
  would only ever see the fragment after the last slash. The delimiter set is
  narrowed to whitespace so the whole path arrives as one token.
* GNU readline and the libedit build that ships with some macOS Pythons need
  different key bindings for Tab, and each ignores the other's form.
* Completions re-attach the directory prefix exactly as the user typed it, so a
  path beginning with "~" stays that way instead of expanding on screen.
"""

from __future__ import annotations

import os
from pathlib import Path

try:  # pragma: no cover - exercised by whichever branch the platform provides
    import readline
except ImportError:  # pragma: no cover
    readline = None  # type: ignore[assignment]


def path_candidates(text: str, only_dirs: bool = True) -> list[str]:
    """Return the completions for a partially typed path.

    The returned strings keep whatever directory prefix the user typed, so "~/"
    stays "~/" rather than expanding to an absolute path on screen. Directories
    come back with a trailing separator so typing can continue straight into the
    next component.
    """
    typed_dir, partial = os.path.split(text)
    search_dir = Path(os.path.expanduser(typed_dir or "."))

    if not search_dir.is_dir():
        return []

    # Re-attached to every candidate so the display keeps the user's own prefix.
    prefix = typed_dir + "/" if typed_dir and not typed_dir.endswith("/") else typed_dir

    lowered = partial.lower()
    candidates: list[str] = []
    try:
        entries = sorted(search_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return []

    for entry in entries:
        if not entry.name.lower().startswith(lowered):
            continue
        is_dir = entry.is_dir()
        if only_dirs and not is_dir:
            continue
        candidates.append(f"{prefix}{entry.name}{'/' if is_dir else ''}")
    return candidates


class PathCompleter:
    """A readline completer over filesystem paths."""

    def __init__(self, only_dirs: bool = True) -> None:
        self.only_dirs = only_dirs
        self._cache_text: str | None = None
        self._cache: list[str] = []

    def complete(self, text: str, state: int) -> str | None:
        """Return the ``state``-th completion for ``text``, or None when exhausted."""
        if state == 0 or text != self._cache_text:
            self._cache_text = text
            self._cache = path_candidates(text, only_dirs=self.only_dirs)
        if state < len(self._cache):
            return self._cache[state]
        return None


def enable_path_completion(only_dirs: bool = True) -> bool:
    """Install the path completer on readline.

    Returns False when readline is unavailable, so the caller can fall back to a
    plain prompt rather than failing.
    """
    if readline is None:
        return False

    readline.set_completer(PathCompleter(only_dirs=only_dirs).complete)
    # Without this, "/" and "~" are treated as word boundaries and the completer
    # receives only the trailing fragment.
    readline.set_completer_delims(" \t\n")

    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
    return True


def prompt_for_path(prompt: str) -> Path:
    """Ask the user for a path, with tab completion when it is available."""
    enable_path_completion()
    while True:
        raw = input(prompt).strip()
        if raw:
            return Path(os.path.expanduser(raw)).absolute()
        print("Please enter a path.")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_completion -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/idea-migrate
git add src/idea_migrate/completion.py tests/test_completion.py
git commit -m "feat: add readline path completion with libedit fallback"
```

---

### Task 6: Rewrite path references in IDE configuration

**Model:** Opus 5

**Files:**
- Create: `src/idea_migrate/rewrite.py`
- Create: `tests/fixtures/recentProjects.xml`
- Create: `tests/fixtures/malformed.xml`
- Test: `tests/test_rewrite.py`

**Interfaces:**
- Consumes: `idea_migrate.errors.XmlIntegrityError`
- Produces:
  - `idea_migrate.rewrite.prefix_variants(old: Path, new: Path, home: Path) -> list[tuple[str, str]]`
  - `idea_migrate.rewrite.rewrite_text(text: str, variants: Sequence[tuple[str, str]]) -> tuple[str, int]`
  - `idea_migrate.rewrite.rewrite_file(path: Path, variants, dry_run: bool = False) -> int`
  - `idea_migrate.rewrite.config_files(product_dir: Path) -> list[Path]`
  - `idea_migrate.rewrite.rewrite_products(product_dirs, variants, dry_run: bool = False) -> dict[str, int]`
  - `idea_migrate.rewrite.count_references(product_dirs, variants) -> int`

**Context — this task has the highest blast radius in the tool. Three rules:**

**Boundary anchoring.** A prefix only matches when the next character is `/`, a quote, `<`, or end of string. Without this, moving `WebstormProjects` would also corrupt every reference to `WebstormProjectsArchive`.

**Case-insensitive matching, exact-length replacement.** The filesystem is case-insensitive, so different IDEs may have recorded the same directory under different spellings — `PycharmProjects` and `PyCharmProjects` are one real directory on the target machine. A case-sensitive search would silently miss half the references, report success, and leave the user with broken entries and no error. Match case-insensitively; replace only the matched prefix and leave every byte after it untouched.

**Verify, then write.** Do not round-trip through an XML parser to edit — re-serializing reorders attributes and normalizes whitespace across the whole file. Instead replace text, then parse the *result* to confirm it is still well-formed, and only then write. If the *original* file was already malformed, skip it with a warning rather than blaming the rewrite.

Replacement must be literal: use a `lambda` in `re.sub`, never a replacement string, or a `\` or `\g` in a path would be interpreted as a backreference.

Writes are atomic: write a temporary file in the same directory, then `os.replace` it over the original, preserving the original file mode.

- [ ] **Step 1: Write the failing test**

Create `tests/fixtures/recentProjects.xml`:

```xml
<application>
  <component name="RecentProjectsManager">
    <option name="additionalInfo">
      <map>
        <entry key="$USER_HOME$/WebstormProjects/alpha">
          <value>
            <RecentProjectMetaInfo frameTitle="alpha" projectWorkspaceId="AAA">
              <option name="binFolder" value="$APPLICATION_HOME_DIR$/bin" />
            </RecentProjectMetaInfo>
          </value>
        </entry>
        <entry key="$USER_HOME$/WebstormProjectsArchive/beta">
          <value>
            <RecentProjectMetaInfo frameTitle="beta" projectWorkspaceId="BBB" />
          </value>
        </entry>
        <entry key="/Users/tester/webstormprojects/gamma">
          <value>
            <RecentProjectMetaInfo frameTitle="gamma" projectWorkspaceId="CCC" />
          </value>
        </entry>
        <entry key="$USER_HOME$/Downloads/delta">
          <value>
            <RecentProjectMetaInfo frameTitle="delta" projectWorkspaceId="DDD" />
          </value>
        </entry>
      </map>
    </option>
  </component>
</application>
```

Create `tests/fixtures/malformed.xml`:

```xml
<application>
  <component name="Broken">
    <entry key="$USER_HOME$/WebstormProjects/alpha">
  </component>
```

Create `tests/test_rewrite.py`:

```python
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from idea_migrate.errors import XmlIntegrityError
from idea_migrate.rewrite import (
    config_files,
    count_references,
    prefix_variants,
    rewrite_file,
    rewrite_products,
    rewrite_text,
)

FIXTURES = Path(__file__).parent / "fixtures"
HOME = Path("/Users/tester")
OLD = HOME / "WebstormProjects"
NEW = HOME / "Projects" / "WebstormProjects"


class TestPrefixVariants(unittest.TestCase):
    def test_produces_placeholder_and_expanded_forms(self):
        variants = dict(prefix_variants(OLD, NEW, HOME))
        self.assertEqual(
            variants["$USER_HOME$/WebstormProjects"],
            "$USER_HOME$/Projects/WebstormProjects",
        )
        self.assertEqual(
            variants["/Users/tester/WebstormProjects"],
            "/Users/tester/Projects/WebstormProjects",
        )

    def test_produces_file_url_forms(self):
        variants = dict(prefix_variants(OLD, NEW, HOME))
        self.assertEqual(
            variants["file:///Users/tester/WebstormProjects"],
            "file:///Users/tester/Projects/WebstormProjects",
        )
        self.assertEqual(
            variants["file://$USER_HOME$/WebstormProjects"],
            "file://$USER_HOME$/Projects/WebstormProjects",
        )

    def test_path_outside_home_has_no_placeholder_variant(self):
        outside = Path("/opt/work")
        variants = dict(prefix_variants(outside, Path("/opt/moved"), HOME))
        self.assertNotIn("$USER_HOME$/opt/work", variants)
        self.assertIn("/opt/work", variants)


class TestRewriteText(unittest.TestCase):
    def setUp(self):
        self.variants = prefix_variants(OLD, NEW, HOME)

    def test_replaces_placeholder_form(self):
        text = '<entry key="$USER_HOME$/WebstormProjects/alpha">'
        result, count = rewrite_text(text, self.variants)
        self.assertEqual(
            result, '<entry key="$USER_HOME$/Projects/WebstormProjects/alpha">'
        )
        self.assertEqual(count, 1)

    def test_does_not_match_longer_sibling_directory(self):
        text = '<entry key="$USER_HOME$/WebstormProjectsArchive/beta">'
        result, count = rewrite_text(text, self.variants)
        self.assertEqual(result, text)
        self.assertEqual(count, 0)

    def test_matches_a_different_capitalization(self):
        text = '<entry key="/Users/tester/webstormprojects/gamma">'
        result, count = rewrite_text(text, self.variants)
        self.assertEqual(
            result, '<entry key="/Users/tester/Projects/WebstormProjects/gamma">'
        )
        self.assertEqual(count, 1)

    def test_matches_bare_prefix_before_closing_quote(self):
        text = '<option value="$USER_HOME$/WebstormProjects" />'
        result, count = rewrite_text(text, self.variants)
        self.assertEqual(
            result, '<option value="$USER_HOME$/Projects/WebstormProjects" />'
        )
        self.assertEqual(count, 1)

    def test_leaves_unrelated_paths_alone(self):
        text = '<entry key="$USER_HOME$/Downloads/delta">'
        result, count = rewrite_text(text, self.variants)
        self.assertEqual(result, text)
        self.assertEqual(count, 0)

    def test_replacement_is_literal_not_a_backreference(self):
        odd_new = Path("/Users/tester/a\\1b")
        variants = prefix_variants(OLD, odd_new, HOME)
        text = '<entry key="/Users/tester/WebstormProjects/x">'
        result, _ = rewrite_text(text, variants)
        self.assertIn("a\\1b", result)


class TestRewriteFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.variants = prefix_variants(OLD, NEW, HOME)

    def tearDown(self):
        self._tmp.cleanup()

    def test_rewrites_and_stays_valid_xml(self):
        target = self.tmp / "recentProjects.xml"
        shutil.copy(FIXTURES / "recentProjects.xml", target)
        count = rewrite_file(target, self.variants)
        self.assertEqual(count, 2)  # placeholder form + lowercase expanded form
        text = target.read_text(encoding="utf-8")
        self.assertIn("$USER_HOME$/Projects/WebstormProjects/alpha", text)
        self.assertIn("$USER_HOME$/WebstormProjectsArchive/beta", text)
        self.assertIn("$USER_HOME$/Downloads/delta", text)
        ET.fromstring(text)  # raises if malformed

    def test_dry_run_reports_but_does_not_write(self):
        target = self.tmp / "recentProjects.xml"
        shutil.copy(FIXTURES / "recentProjects.xml", target)
        before = target.read_bytes()
        count = rewrite_file(target, self.variants, dry_run=True)
        self.assertEqual(count, 2)
        self.assertEqual(target.read_bytes(), before)

    def test_already_malformed_file_is_skipped(self):
        target = self.tmp / "malformed.xml"
        shutil.copy(FIXTURES / "malformed.xml", target)
        before = target.read_bytes()
        count = rewrite_file(target, self.variants)
        self.assertEqual(count, 0)
        self.assertEqual(target.read_bytes(), before)

    def test_rewrite_that_would_break_xml_raises(self):
        target = self.tmp / "recentProjects.xml"
        shutil.copy(FIXTURES / "recentProjects.xml", target)
        before = target.read_bytes()
        breaking = [("$USER_HOME$/WebstormProjects", 'x"><broken')]
        with self.assertRaises(XmlIntegrityError):
            rewrite_file(target, breaking)
        self.assertEqual(target.read_bytes(), before)

    def test_file_with_no_matches_is_untouched(self):
        target = self.tmp / "other.xml"
        target.write_text('<a key="$USER_HOME$/Downloads/x" />', encoding="utf-8")
        before = target.stat().st_mtime_ns
        self.assertEqual(rewrite_file(target, self.variants), 0)
        self.assertEqual(target.stat().st_mtime_ns, before)


class TestProductScan(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.product = Path(self._tmp.name) / "IntelliJIdea2026.2"
        (self.product / "options").mkdir(parents=True)
        (self.product / "workspace").mkdir(parents=True)
        shutil.copy(
            FIXTURES / "recentProjects.xml",
            self.product / "options" / "recentProjects.xml",
        )
        (self.product / "workspace" / "AAA.xml").write_text(
            '<project><path value="$USER_HOME$/WebstormProjects/alpha" /></project>',
            encoding="utf-8",
        )
        (self.product / "options" / "notes.txt").write_text("x", encoding="utf-8")
        self.variants = prefix_variants(OLD, NEW, HOME)

    def tearDown(self):
        self._tmp.cleanup()

    def test_config_files_finds_only_xml_in_options_and_workspace(self):
        names = sorted(p.name for p in config_files(self.product))
        self.assertEqual(names, ["AAA.xml", "recentProjects.xml"])

    def test_count_references_before_rewriting(self):
        self.assertEqual(count_references([self.product], self.variants), 3)

    def test_rewrite_products_reports_per_file_counts(self):
        result = rewrite_products([self.product], self.variants)
        self.assertEqual(sum(result.values()), 3)
        self.assertEqual(count_references([self.product], self.variants), 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_rewrite -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'idea_migrate.rewrite'`

- [ ] **Step 3: Write the implementation**

`src/idea_migrate/rewrite.py`:

```python
"""Rewrite stored path references inside JetBrains configuration files.

This is the highest-risk part of the tool, so three rules govern it.

Boundary anchoring: a prefix matches only when the next character ends the path
component - a separator, a quote, an angle bracket, or end of string. Without
this, moving "WebstormProjects" would also corrupt "WebstormProjectsArchive".

Case-insensitive matching: the filesystem is case-insensitive, so different IDEs
may have recorded one directory under different spellings. A case-sensitive
search would silently miss half the references and report success. Only the
matched prefix is replaced; every byte after it is preserved.

Verify then write: editing is done on raw text, never by re-serializing the XML
tree, because that would reorder attributes and reflow the entire file. The
result is parsed to confirm it is still well-formed before it is written. A file
that was already malformed before we touched it is skipped, not blamed on us.
"""

from __future__ import annotations

import os
import re
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from pathlib import Path

from .errors import XmlIntegrityError

USER_HOME_MACRO = "$USER_HOME$"

# The prefix must be followed by something that ends the path component.
_BOUNDARY = r"""(?=[/"'<]|$)"""


def _macro_form(path: Path, home: Path) -> str | None:
    """Return the ``$USER_HOME$``-relative form of a path, or None if not under home."""
    try:
        relative = path.relative_to(home)
    except ValueError:
        return None
    return f"{USER_HOME_MACRO}/{relative.as_posix()}"


def prefix_variants(old: Path, new: Path, home: Path) -> list[tuple[str, str]]:
    """Return every (old, new) prefix pair to search for.

    JetBrains writes paths in more than one form: absolute, relative to the
    ``$USER_HOME$`` placeholder, and as ``file://`` URLs of either.
    """
    variants: list[tuple[str, str]] = [(old.as_posix(), new.as_posix())]

    old_macro = _macro_form(old, home)
    new_macro = _macro_form(new, home)
    if old_macro and new_macro:
        variants.append((old_macro, new_macro))

    for old_form, new_form in list(variants):
        variants.append((f"file://{old_form}", f"file://{new_form}"))

    return variants


def rewrite_text(
    text: str, variants: Sequence[tuple[str, str]]
) -> tuple[str, int]:
    """Replace every anchored occurrence of each old prefix. Returns text and count."""
    total = 0
    for old, new in variants:
        pattern = re.compile(re.escape(old) + _BOUNDARY, re.IGNORECASE)
        # A lambda keeps the replacement literal - a backslash or \g in a path
        # would otherwise be read as a backreference.
        text, count = pattern.subn(lambda _match, value=new: value, text)
        total += count
    return text, total


def config_files(product_dir: Path) -> list[Path]:
    """Return the XML files under a product's options/ and workspace/ directories."""
    found: list[Path] = []
    for subdir in ("options", "workspace"):
        directory = product_dir / subdir
        if directory.is_dir():
            found.extend(sorted(directory.glob("*.xml")))
    return found


def rewrite_file(
    path: Path, variants: Sequence[tuple[str, str]], dry_run: bool = False
) -> int:
    """Rewrite one file in place. Returns the number of replacements made.

    Returns 0 without writing when nothing matched or when the file was already
    malformed. Raises XmlIntegrityError if the rewrite would produce invalid XML,
    leaving the original untouched.
    """
    try:
        original = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0

    try:
        ET.fromstring(original)
    except ET.ParseError:
        # Already broken before we arrived; not ours to fix or to blame.
        return 0

    updated, count = rewrite_text(original, variants)
    if count == 0:
        return 0

    try:
        ET.fromstring(updated)
    except ET.ParseError as exc:
        raise XmlIntegrityError(
            f"Rewriting {path} would have produced invalid XML: {exc}"
        ) from exc

    if dry_run:
        return count

    mode = path.stat().st_mode
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    )
    try:
        handle.write(updated)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)
    os.chmod(path, mode)
    return count


def rewrite_products(
    product_dirs: Iterable[Path],
    variants: Sequence[tuple[str, str]],
    dry_run: bool = False,
) -> dict[str, int]:
    """Rewrite every configuration file under each product directory.

    Returns a mapping of file path to replacement count, listing only files that
    actually changed.
    """
    changed: dict[str, int] = {}
    for product_dir in product_dirs:
        for file_path in config_files(product_dir):
            count = rewrite_file(file_path, variants, dry_run=dry_run)
            if count:
                changed[str(file_path)] = count
    return changed


def count_references(
    product_dirs: Iterable[Path], variants: Sequence[tuple[str, str]]
) -> int:
    """Count how many references to the old prefixes still exist."""
    total = 0
    for product_dir in product_dirs:
        for file_path in config_files(product_dir):
            try:
                text = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            _, count = rewrite_text(text, variants)
            total += count
    return total
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_rewrite -v`
Expected: PASS, 18 tests

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/idea-migrate
git add src/idea_migrate/rewrite.py tests/test_rewrite.py tests/fixtures/
git commit -m "feat: anchored case-insensitive path rewriting with XML verification"
```

---

### Task 7: The backup manifest

**Model:** Sonnet 5

**Files:**
- Create: `src/idea_migrate/manifest.py`
- Test: `tests/test_manifest.py`

**Interfaces:**
- Consumes: `idea_migrate.__version__`
- Produces:
  - `idea_migrate.manifest.MANIFEST_NAME = "manifest.json"`
  - `idea_migrate.manifest.Manifest` — dataclass with fields `version: int`, `tool_version: str`, `created_at: str`, `home: str`, `source: str`, `dest: str`, `move_status: str`, `backed_up_products: list[str]`, `rewritten_files: dict[str, int]`, `undone_at: str | None`
  - `idea_migrate.manifest.write_manifest(backup_dir: Path, manifest: Manifest) -> Path`
  - `idea_migrate.manifest.read_manifest(backup_dir: Path) -> Manifest`
  - `idea_migrate.manifest.mark_undone(backup_dir: Path, when: str) -> Manifest`

- [ ] **Step 1: Write the failing test**

`tests/test_manifest.py`:

```python
import json
import tempfile
import unittest
from pathlib import Path

from idea_migrate.manifest import (
    MANIFEST_NAME,
    Manifest,
    mark_undone,
    read_manifest,
    write_manifest,
)


def sample_manifest() -> Manifest:
    return Manifest(
        version=1,
        tool_version="0.1.0",
        created_at="2026-08-29T14:30:05",
        home="/Users/tester",
        source="/Users/tester/WebstormProjects",
        dest="/Users/tester/Projects/WebstormProjects",
        move_status="moved",
        backed_up_products=["IntelliJIdea2026.2", "PyCharm2026.2"],
        rewritten_files={"/x/options/recentProjects.xml": 3},
        undone_at=None,
    )


class TestManifestRoundTrip(unittest.TestCase):
    def test_write_then_read_returns_equal_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            original = sample_manifest()
            write_manifest(backup_dir, original)
            self.assertEqual(read_manifest(backup_dir), original)

    def test_written_file_is_readable_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            write_manifest(backup_dir, sample_manifest())
            data = json.loads((backup_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertEqual(data["source"], "/Users/tester/WebstormProjects")
            self.assertIsNone(data["undone_at"])

    def test_missing_manifest_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                read_manifest(Path(tmp))


class TestMarkUndone(unittest.TestCase):
    def test_sets_undone_timestamp_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            write_manifest(backup_dir, sample_manifest())
            updated = mark_undone(backup_dir, "2026-08-30T09:00:00")
            self.assertEqual(updated.undone_at, "2026-08-30T09:00:00")
            self.assertEqual(read_manifest(backup_dir).undone_at, "2026-08-30T09:00:00")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_manifest -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'idea_migrate.manifest'`

- [ ] **Step 3: Write the implementation**

`src/idea_migrate/manifest.py`:

```python
"""The record of what a run did, stored alongside its backup.

The manifest is the single source of truth for both undo and the backups
listing, so nothing else in the tool has to keep state.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1


@dataclass(frozen=True)
class Manifest:
    version: int
    tool_version: str
    created_at: str
    home: str
    source: str
    dest: str
    move_status: str
    backed_up_products: list[str]
    rewritten_files: dict[str, int]
    undone_at: str | None


def write_manifest(backup_dir: Path, manifest: Manifest) -> Path:
    """Write the manifest into a backup directory and return its path."""
    target = backup_dir / MANIFEST_NAME
    target.write_text(
        json.dumps(asdict(manifest), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def read_manifest(backup_dir: Path) -> Manifest:
    """Read the manifest from a backup directory."""
    target = backup_dir / MANIFEST_NAME
    if not target.is_file():
        raise FileNotFoundError(f"No {MANIFEST_NAME} in {backup_dir}")
    data = json.loads(target.read_text(encoding="utf-8"))
    return Manifest(**data)


def mark_undone(backup_dir: Path, when: str) -> Manifest:
    """Record that this backup has been rolled back, and persist the change."""
    updated = replace(read_manifest(backup_dir), undone_at=when)
    write_manifest(backup_dir, updated)
    return updated
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_manifest -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/idea-migrate
git add src/idea_migrate/manifest.py tests/test_manifest.py
git commit -m "feat: add the backup manifest"
```

---

### Task 8: Create backups and generate undo.sh

**Model:** Sonnet 5

**Files:**
- Create: `src/idea_migrate/backup.py`
- Test: `tests/test_backup.py`

**Interfaces:**
- Consumes: `idea_migrate.errors.BackupError`
- Produces:
  - `idea_migrate.backup.new_backup_dir(backup_root: Path, now: datetime) -> Path`
  - `idea_migrate.backup.back_up_products(product_dirs: Sequence[Path], backup_dir: Path) -> list[str]`
  - `idea_migrate.backup.write_undo_script(backup_dir: Path) -> Path`
  - `idea_migrate.backup.UNDO_SCRIPT_NAME = "undo.sh"`

**Context:** Only `options/` and `workspace/` are copied — those are the only directories the tool modifies, and together they are about 36 MB across all products, versus 23 GB for the full configuration (almost all plugins).

The generated `undo.sh` must not depend on the `idea_migrate` package being installed or working, since it exists precisely for when something has gone wrong. It may rely on `python3` being present for JSON parsing, which is safe — this is a Python tool. It defaults to its own directory and accepts an optional path argument, so it still works after the backup folder is moved.

- [ ] **Step 1: Write the failing test**

`tests/test_backup.py`:

```python
import os
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from idea_migrate.backup import (
    UNDO_SCRIPT_NAME,
    back_up_products,
    new_backup_dir,
    write_undo_script,
)


class BackupTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.jetbrains = self.tmp / "JetBrains"
        self.products = []
        for name in ("IntelliJIdea2026.2", "PyCharm2026.2"):
            product = self.jetbrains / name
            (product / "options").mkdir(parents=True)
            (product / "workspace").mkdir(parents=True)
            (product / "plugins").mkdir(parents=True)
            (product / "options" / "recentProjects.xml").write_text(
                "<application />", encoding="utf-8"
            )
            (product / "workspace" / "AAA.xml").write_text(
                "<project />", encoding="utf-8"
            )
            (product / "plugins" / "huge.jar").write_text("x" * 100, encoding="utf-8")
            self.products.append(product)
        self.backup_root = self.tmp / "Idea-Migration-Backups"

    def tearDown(self):
        self._tmp.cleanup()


class TestNewBackupDir(BackupTestCase):
    def test_creates_a_timestamped_directory(self):
        when = datetime(2026, 8, 29, 14, 30, 5)
        created = new_backup_dir(self.backup_root, when)
        self.assertTrue(created.is_dir())
        self.assertEqual(created.name, "2026-08-29_143005")
        self.assertEqual(created.parent, self.backup_root)

    def test_creates_the_backup_root_if_absent(self):
        self.assertFalse(self.backup_root.exists())
        new_backup_dir(self.backup_root, datetime(2026, 8, 29, 14, 30, 5))
        self.assertTrue(self.backup_root.is_dir())

    def test_second_run_in_the_same_second_gets_a_distinct_directory(self):
        when = datetime(2026, 8, 29, 14, 30, 5)
        first = new_backup_dir(self.backup_root, when)
        second = new_backup_dir(self.backup_root, when)
        self.assertNotEqual(first, second)
        self.assertTrue(second.is_dir())


class TestBackUpProducts(BackupTestCase):
    def test_copies_options_and_workspace_only(self):
        backup_dir = new_backup_dir(self.backup_root, datetime(2026, 8, 29, 14, 30, 5))
        names = back_up_products(self.products, backup_dir)
        self.assertEqual(names, ["IntelliJIdea2026.2", "PyCharm2026.2"])
        config = backup_dir / "config"
        self.assertTrue((config / "IntelliJIdea2026.2" / "options" / "recentProjects.xml").is_file())
        self.assertTrue((config / "IntelliJIdea2026.2" / "workspace" / "AAA.xml").is_file())
        self.assertFalse((config / "IntelliJIdea2026.2" / "plugins").exists())

    def test_file_contents_are_preserved(self):
        backup_dir = new_backup_dir(self.backup_root, datetime(2026, 8, 29, 14, 30, 5))
        back_up_products(self.products, backup_dir)
        copied = (
            backup_dir / "config" / "PyCharm2026.2" / "options" / "recentProjects.xml"
        )
        self.assertEqual(copied.read_text(encoding="utf-8"), "<application />")


class TestUndoScript(BackupTestCase):
    def test_script_is_written_and_executable(self):
        backup_dir = new_backup_dir(self.backup_root, datetime(2026, 8, 29, 14, 30, 5))
        script = write_undo_script(backup_dir)
        self.assertEqual(script.name, UNDO_SCRIPT_NAME)
        self.assertTrue(script.is_file())
        self.assertTrue(os.stat(script).st_mode & stat.S_IXUSR)

    def test_script_passes_a_syntax_check(self):
        backup_dir = new_backup_dir(self.backup_root, datetime(2026, 8, 29, 14, 30, 5))
        script = write_undo_script(backup_dir)
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_script_accepts_a_path_argument(self):
        backup_dir = new_backup_dir(self.backup_root, datetime(2026, 8, 29, 14, 30, 5))
        text = write_undo_script(backup_dir).read_text(encoding="utf-8")
        self.assertIn('${1:-', text)

    def test_script_refuses_without_a_manifest(self):
        backup_dir = new_backup_dir(self.backup_root, datetime(2026, 8, 29, 14, 30, 5))
        script = write_undo_script(backup_dir)
        result = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manifest", (result.stderr + result.stdout).lower())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_backup -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'idea_migrate.backup'`

- [ ] **Step 3: Write the implementation**

`src/idea_migrate/backup.py`:

```python
"""Create the backup that makes a run reversible.

Only options/ and workspace/ are copied, because they are the only directories
the tool modifies. Together they are around 36 MB across every installed
product, against 23 GB for the full configuration - the rest is plugins.

The generated undo.sh deliberately does not import this package. It exists for
the case where something has gone wrong, so its only dependency beyond a shell
is python3, for reading the manifest.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from .errors import BackupError

UNDO_SCRIPT_NAME = "undo.sh"
BACKED_UP_SUBDIRS = ("options", "workspace")

UNDO_SCRIPT = r"""#!/usr/bin/env bash
# Roll back one idea-migrate run.
#
# Usage: ./undo.sh [BACKUP_DIR]
# With no argument it uses the directory this script lives in, so it keeps
# working after the backup folder has been moved or renamed.
set -euo pipefail

BACKUP_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
MANIFEST="$BACKUP_DIR/manifest.json"

if [ ! -f "$MANIFEST" ]; then
  echo "No manifest.json found in $BACKUP_DIR" >&2
  echo "Pass the backup directory explicitly: $0 /path/to/backup" >&2
  exit 1
fi

read_field() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2]) or "")' "$MANIFEST" "$1"
}

SOURCE="$(read_field source)"
DEST="$(read_field dest)"
UNDONE_AT="$(read_field undone_at)"
HOME_DIR="$(read_field home)"
JETBRAINS="$HOME_DIR/Library/Application Support/JetBrains"

if [ -n "$UNDONE_AT" ]; then
  echo "This backup was already rolled back at $UNDONE_AT." >&2
  echo "Refusing to undo it twice." >&2
  exit 1
fi

if pgrep -f 'JetBrains|IntelliJ IDEA\.app|PyCharm\.app|WebStorm\.app|GoLand\.app|DataGrip\.app' >/dev/null 2>&1; then
  echo "A JetBrains application appears to be running." >&2
  echo "Quit it before undoing, or its settings will be overwritten on exit." >&2
  exit 1
fi

echo "Rolling back:"
echo "  move   $DEST -> $SOURCE"
echo "  config restored from $BACKUP_DIR/config"
echo

if [ -d "$DEST" ] && [ ! -e "$SOURCE" ]; then
  mv "$DEST" "$SOURCE"
  echo "Moved $DEST back to $SOURCE"
elif [ -e "$SOURCE" ]; then
  echo "Source $SOURCE already exists; leaving the directory alone."
else
  echo "Destination $DEST not found; leaving the directory alone."
fi

if [ -d "$BACKUP_DIR/config" ]; then
  for product_dir in "$BACKUP_DIR"/config/*/; do
    [ -d "$product_dir" ] || continue
    product="$(basename "$product_dir")"
    for sub in options workspace; do
      if [ -d "$product_dir/$sub" ]; then
        mkdir -p "$JETBRAINS/$product/$sub"
        cp -R "$product_dir/$sub/." "$JETBRAINS/$product/$sub/"
      fi
    done
    echo "Restored settings for $product"
  done
fi

python3 - "$MANIFEST" <<'PY'
import datetime, json, sys
path = sys.argv[1]
with open(path) as handle:
    data = json.load(handle)
data["undone_at"] = datetime.datetime.now().isoformat(timespec="seconds")
with open(path, "w") as handle:
    json.dump(data, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

echo
echo "Undo complete."
"""


def new_backup_dir(backup_root: Path, now: datetime) -> Path:
    """Create and return a fresh timestamped directory under the backup root."""
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d_%H%M%S")
    candidate = backup_root / stamp
    suffix = 1
    while candidate.exists():
        candidate = backup_root / f"{stamp}-{suffix}"
        suffix += 1
    candidate.mkdir()
    return candidate


def back_up_products(
    product_dirs: Sequence[Path], backup_dir: Path
) -> list[str]:
    """Copy each product's options/ and workspace/ into the backup directory.

    Returns the product names backed up, in the order given.
    """
    config_root = backup_dir / "config"
    names: list[str] = []
    try:
        for product in product_dirs:
            for subdir in BACKED_UP_SUBDIRS:
                origin = product / subdir
                if origin.is_dir():
                    shutil.copytree(origin, config_root / product.name / subdir)
            names.append(product.name)
    except OSError as exc:
        raise BackupError(f"Could not create the backup: {exc}") from exc
    return names


def write_undo_script(backup_dir: Path) -> Path:
    """Write the standalone undo script into a backup directory."""
    script = backup_dir / UNDO_SCRIPT_NAME
    script.write_text(UNDO_SCRIPT, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return script
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_backup -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/idea-migrate
git add src/idea_migrate/backup.py tests/test_backup.py
git commit -m "feat: create config backups and generate a standalone undo script"
```

---

### Task 9: Preflight and move

**Model:** Sonnet 5

**Files:**
- Create: `src/idea_migrate/mover.py`
- Test: `tests/test_mover.py`

**Interfaces:**
- Consumes: `idea_migrate.paths.MoveSpec`, `idea_migrate.errors.IdeRunningError`, `idea_migrate.processes.running_ides`
- Produces:
  - `idea_migrate.mover.assert_no_ide_running(ps_output: str | None = None) -> None`
  - `idea_migrate.mover.move_directory(spec: MoveSpec) -> None`

- [ ] **Step 1: Write the failing test**

`tests/test_mover.py`:

```python
import tempfile
import unittest
from pathlib import Path

from idea_migrate.errors import IdeRunningError
from idea_migrate.mover import assert_no_ide_running, move_directory
from idea_migrate.paths import validate_move

RUNNING = "/Applications/IntelliJ IDEA.app/Contents/MacOS/idea\n"
NOT_RUNNING = "/usr/sbin/cfprefsd\n"


class TestAssertNoIdeRunning(unittest.TestCase):
    def test_passes_when_nothing_is_running(self):
        assert_no_ide_running(NOT_RUNNING)  # must not raise

    def test_raises_and_names_the_ide(self):
        with self.assertRaises(IdeRunningError) as ctx:
            assert_no_ide_running(RUNNING)
        self.assertIn("IntelliJ IDEA", str(ctx.exception))


class TestMoveDirectory(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        self.source = self.home / "WebstormProjects"
        (self.source / "alpha").mkdir(parents=True)
        (self.source / "alpha" / "file.txt").write_text("hello", encoding="utf-8")
        (self.home / "Projects").mkdir()
        self.dest = self.home / "Projects" / "WebstormProjects"

    def tearDown(self):
        self._tmp.cleanup()

    def test_moves_the_directory_and_its_contents(self):
        spec = validate_move(self.source, self.dest, self.home)
        move_directory(spec)
        self.assertFalse(self.source.exists())
        self.assertTrue(self.dest.is_dir())
        self.assertEqual(
            (self.dest / "alpha" / "file.txt").read_text(encoding="utf-8"), "hello"
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_mover -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'idea_migrate.mover'`

- [ ] **Step 3: Write the implementation**

`src/idea_migrate/mover.py`:

```python
"""Preflight checks and the move itself.

Within one filesystem a move is an instant atomic rename regardless of how large
the directory is. Across filesystems there is no such guarantee, so the fallback
copies, verifies the copy exists, and only then removes the original.
"""

from __future__ import annotations

import os
import shutil

from .errors import IdeRunningError
from .paths import MoveSpec
from .processes import running_ides


def assert_no_ide_running(ps_output: str | None = None) -> None:
    """Raise IdeRunningError if any JetBrains IDE is currently running.

    A running IDE holds its settings in memory and writes them out when it quits,
    which would silently overwrite the repairs this tool makes.
    """
    running = running_ides(ps_output)
    if running:
        names = ", ".join(running)
        raise IdeRunningError(
            f"These JetBrains applications are running: {names}. "
            "Quit them before migrating, or their settings will be overwritten "
            "when they exit."
        )


def move_directory(spec: MoveSpec) -> None:
    """Move the source directory to the destination."""
    if spec.same_device:
        os.rename(spec.source, spec.dest)
        return

    shutil.copytree(spec.source, spec.dest, symlinks=True)
    if not spec.dest.is_dir():
        raise OSError(f"Copy to {spec.dest} did not produce a directory")
    shutil.rmtree(spec.source)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_mover -v`
Expected: PASS, 3 tests

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/idea-migrate
git add src/idea_migrate/mover.py tests/test_mover.py
git commit -m "feat: add preflight IDE check and the directory move"
```

---

### Task 10: The backups listing command

**Model:** Sonnet 5

**Files:**
- Create: `src/idea_migrate/listing.py`
- Test: `tests/test_listing.py`

**Interfaces:**
- Consumes: `idea_migrate.manifest.Manifest`, `read_manifest`
- Produces:
  - `idea_migrate.listing.BackupSummary` — frozen dataclass with fields `directory: Path`, `manifest: Manifest`, `size_bytes: int`
  - `idea_migrate.listing.find_backups(backup_root: Path) -> list[BackupSummary]` — newest first
  - `idea_migrate.listing.format_backups(summaries: Sequence[BackupSummary], backup_root: Path) -> str`

- [ ] **Step 1: Write the failing test**

`tests/test_listing.py`:

```python
import tempfile
import unittest
from pathlib import Path

from idea_migrate.listing import find_backups, format_backups
from idea_migrate.manifest import Manifest, write_manifest


def make_backup(root: Path, stamp: str, undone: str | None = None) -> Path:
    backup_dir = root / stamp
    (backup_dir / "config").mkdir(parents=True)
    (backup_dir / "config" / "x.xml").write_text("<a/>", encoding="utf-8")
    write_manifest(
        backup_dir,
        Manifest(
            version=1,
            tool_version="0.1.0",
            created_at=stamp,
            home="/Users/tester",
            source="/Users/tester/WebstormProjects",
            dest="/Users/tester/Projects/WebstormProjects",
            move_status="moved",
            backed_up_products=["IntelliJIdea2026.2"],
            rewritten_files={"/x.xml": 2},
            undone_at=undone,
        ),
    )
    return backup_dir


class TestFindBackups(unittest.TestCase):
    def test_returns_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_backup(root, "2026-08-29_100000")
            make_backup(root, "2026-08-30_100000")
            found = [s.directory.name for s in find_backups(root)]
            self.assertEqual(found, ["2026-08-30_100000", "2026-08-29_100000"])

    def test_reports_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_backup(root, "2026-08-29_100000")
            self.assertGreater(find_backups(root)[0].size_bytes, 0)

    def test_missing_root_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(find_backups(Path(tmp) / "absent"), [])

    def test_directory_without_manifest_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_backup(root, "2026-08-29_100000")
            (root / "junk").mkdir()
            self.assertEqual(len(find_backups(root)), 1)


class TestFormatBackups(unittest.TestCase):
    def test_empty_listing_mentions_the_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = format_backups([], root)
            self.assertIn(str(root), text)
            self.assertIn("No backups", text)

    def test_listing_shows_paths_and_undo_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_backup(root, "2026-08-29_100000")
            text = format_backups(find_backups(root), root)
            self.assertIn("WebstormProjects", text)
            self.assertIn("undo", text)

    def test_undone_backups_are_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_backup(root, "2026-08-29_100000", undone="2026-08-30T09:00:00")
            text = format_backups(find_backups(root), root)
            self.assertIn("rolled back", text.lower())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_listing -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'idea_migrate.listing'`

- [ ] **Step 3: Write the implementation**

`src/idea_migrate/listing.py`:

```python
"""List the backups on disk.

Everything shown here is read from each backup's own manifest, so the listing
stays correct without the tool keeping state anywhere else.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .manifest import MANIFEST_NAME, Manifest, read_manifest


@dataclass(frozen=True)
class BackupSummary:
    directory: Path
    manifest: Manifest
    size_bytes: int


def _directory_size(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                continue
    return total


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def find_backups(backup_root: Path) -> list[BackupSummary]:
    """Return every backup under the root, newest first."""
    if not backup_root.is_dir():
        return []

    summaries: list[BackupSummary] = []
    for entry in backup_root.iterdir():
        if not entry.is_dir() or not (entry / MANIFEST_NAME).is_file():
            continue
        try:
            manifest = read_manifest(entry)
        except (OSError, ValueError, TypeError):
            continue
        summaries.append(
            BackupSummary(
                directory=entry,
                manifest=manifest,
                size_bytes=_directory_size(entry),
            )
        )
    return sorted(summaries, key=lambda s: s.directory.name, reverse=True)


def format_backups(
    summaries: Sequence[BackupSummary], backup_root: Path
) -> str:
    """Render the backups listing for the terminal."""
    if not summaries:
        return (
            f"No backups found in {backup_root}\n"
            "Backups are created the first time you run a migration."
        )

    lines = [f"Backups in {backup_root}", ""]
    for summary in summaries:
        manifest = summary.manifest
        state = (
            f"already rolled back on {manifest.undone_at}"
            if manifest.undone_at
            else "active"
        )
        lines.extend(
            [
                f"  {summary.directory.name}   ({_human_size(summary.size_bytes)}, {state})",
                f"    moved:  {manifest.source}",
                f"        ->  {manifest.dest}",
                f"    files repaired: {sum(manifest.rewritten_files.values())} "
                f"across {len(manifest.backed_up_products)} products",
            ]
        )
        if not manifest.undone_at:
            lines.append(f"    undo:   {summary.directory / 'undo.sh'}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_listing -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/idea-migrate
git add src/idea_migrate/listing.py tests/test_listing.py
git commit -m "feat: add the backups listing command"
```

---

### Task 11: The undo command

**Model:** Sonnet 5

**Files:**
- Create: `src/idea_migrate/undo.py`
- Test: `tests/test_undo.py`

**Interfaces:**
- Consumes: `idea_migrate.manifest`, `idea_migrate.errors.UndoError`, `idea_migrate.mover.assert_no_ide_running`
- Produces: `idea_migrate.undo.undo_backup(backup_dir: Path, jetbrains_root: Path, now: datetime, ps_output: str | None = None) -> list[str]` — returns human-readable lines describing what it did

- [ ] **Step 1: Write the failing test**

`tests/test_undo.py`:

```python
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from idea_migrate.errors import IdeRunningError, UndoError
from idea_migrate.manifest import Manifest, read_manifest, write_manifest
from idea_migrate.undo import undo_backup

QUIET = "/usr/sbin/cfprefsd\n"
RUNNING = "/Applications/IntelliJ IDEA.app/Contents/MacOS/idea\n"
NOW = datetime(2026, 8, 30, 9, 0, 0)


class UndoTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.jetbrains = self.home / "Library" / "Application Support" / "JetBrains"
        self.product = self.jetbrains / "IntelliJIdea2026.2"
        (self.product / "options").mkdir(parents=True)
        (self.product / "options" / "recentProjects.xml").write_text(
            "AFTER", encoding="utf-8"
        )

        self.source = self.home / "WebstormProjects"
        self.dest = self.home / "Projects" / "WebstormProjects"
        self.dest.mkdir(parents=True)
        (self.dest / "alpha.txt").write_text("hello", encoding="utf-8")

        self.backup_dir = self.tmp / "backups" / "2026-08-29_143005"
        saved = self.backup_dir / "config" / "IntelliJIdea2026.2" / "options"
        saved.mkdir(parents=True)
        (saved / "recentProjects.xml").write_text("BEFORE", encoding="utf-8")
        write_manifest(
            self.backup_dir,
            Manifest(
                version=1,
                tool_version="0.1.0",
                created_at="2026-08-29T14:30:05",
                home=str(self.home),
                source=str(self.source),
                dest=str(self.dest),
                move_status="moved",
                backed_up_products=["IntelliJIdea2026.2"],
                rewritten_files={"x.xml": 2},
                undone_at=None,
            ),
        )

    def tearDown(self):
        self._tmp.cleanup()


class TestUndoBackup(UndoTestCase):
    def test_moves_the_directory_back(self):
        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        self.assertTrue(self.source.is_dir())
        self.assertFalse(self.dest.exists())
        self.assertEqual(
            (self.source / "alpha.txt").read_text(encoding="utf-8"), "hello"
        )

    def test_restores_the_configuration(self):
        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        restored = self.product / "options" / "recentProjects.xml"
        self.assertEqual(restored.read_text(encoding="utf-8"), "BEFORE")

    def test_marks_the_manifest_as_undone(self):
        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        self.assertEqual(read_manifest(self.backup_dir).undone_at, NOW.isoformat())

    def test_refuses_to_undo_twice(self):
        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        with self.assertRaises(UndoError) as ctx:
            undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        self.assertIn("already", str(ctx.exception).lower())

    def test_refuses_while_an_ide_is_running(self):
        with self.assertRaises(IdeRunningError):
            undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=RUNNING)

    def test_missing_backup_dir_raises(self):
        with self.assertRaises(UndoError):
            undo_backup(self.tmp / "absent", self.jetbrains, NOW, ps_output=QUIET)

    def test_backup_is_not_deleted(self):
        undo_backup(self.backup_dir, self.jetbrains, NOW, ps_output=QUIET)
        self.assertTrue(self.backup_dir.is_dir())
        self.assertTrue((self.backup_dir / "manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_undo -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'idea_migrate.undo'`

- [ ] **Step 3: Write the implementation**

`src/idea_migrate/undo.py`:

```python
"""Roll back one migration run.

This is the Python route. Every backup also carries a standalone undo.sh that
does the same job without importing this package, for the case where the tool
itself is what broke.

Undoing never deletes the backup.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from .errors import UndoError
from .manifest import MANIFEST_NAME, mark_undone, read_manifest
from .mover import assert_no_ide_running


def undo_backup(
    backup_dir: Path,
    jetbrains_root: Path,
    now: datetime,
    ps_output: str | None = None,
) -> list[str]:
    """Reverse the run recorded in ``backup_dir``. Returns what was done."""
    if not (backup_dir / MANIFEST_NAME).is_file():
        raise UndoError(f"No {MANIFEST_NAME} found in {backup_dir}")

    manifest = read_manifest(backup_dir)
    if manifest.undone_at:
        raise UndoError(
            f"This backup was already rolled back on {manifest.undone_at}. "
            "Refusing to undo it twice."
        )

    assert_no_ide_running(ps_output)

    done: list[str] = []
    source = Path(manifest.source)
    dest = Path(manifest.dest)

    if dest.is_dir() and not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dest), str(source))
        done.append(f"Moved {dest} back to {source}")
    elif source.exists():
        done.append(f"Source {source} already exists; left the directory alone.")
    else:
        done.append(f"Destination {dest} not found; left the directory alone.")

    config_root = backup_dir / "config"
    if config_root.is_dir():
        for product_backup in sorted(config_root.iterdir()):
            if not product_backup.is_dir():
                continue
            for subdir in ("options", "workspace"):
                saved = product_backup / subdir
                if not saved.is_dir():
                    continue
                target = jetbrains_root / product_backup.name / subdir
                target.mkdir(parents=True, exist_ok=True)
                shutil.copytree(saved, target, dirs_exist_ok=True)
            done.append(f"Restored settings for {product_backup.name}")

    mark_undone(backup_dir, now.isoformat())
    done.append(f"Backup kept at {backup_dir}")
    return done
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_undo -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/idea-migrate
git add src/idea_migrate/undo.py tests/test_undo.py
git commit -m "feat: add the undo command"
```

---

### Task 12: Report formatting

**Model:** Sonnet 5

**Files:**
- Create: `src/idea_migrate/report.py`
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces:
  - `idea_migrate.report.format_plan(source: Path, dest: Path, reference_count: int, product_count: int, backup_root: Path) -> str`
  - `idea_migrate.report.format_result(source: Path, dest: Path, rewritten: dict[str, int], remaining: int, hardcoded_warnings: Sequence[str], backup_dir: Path) -> str`

**Context:** The result report's **last line** must be the undo command for this run, with the backup path immediately above it. That placement is a requirement, not a preference — it is what the user reads when they need to reverse something.

- [ ] **Step 1: Write the failing test**

`tests/test_report.py`:

```python
import unittest
from pathlib import Path

from idea_migrate.report import format_plan, format_result

SOURCE = Path("/Users/tester/WebstormProjects")
DEST = Path("/Users/tester/Projects/WebstormProjects")
BACKUP = Path("/Users/tester/Idea-Migration-Backups/2026-08-29_143005")


class TestFormatPlan(unittest.TestCase):
    def test_shows_both_paths_and_counts(self):
        text = format_plan(SOURCE, DEST, 41, 15, BACKUP.parent)
        self.assertIn(str(SOURCE), text)
        self.assertIn(str(DEST), text)
        self.assertIn("41", text)
        self.assertIn("15", text)

    def test_mentions_where_the_backup_will_go(self):
        text = format_plan(SOURCE, DEST, 41, 15, BACKUP.parent)
        self.assertIn(str(BACKUP.parent), text)


class TestFormatResult(unittest.TestCase):
    def test_last_line_is_the_undo_command(self):
        text = format_result(SOURCE, DEST, {"/a.xml": 3}, 0, [], BACKUP)
        last = text.rstrip().splitlines()[-1]
        self.assertIn("undo.sh", last)
        self.assertIn(str(BACKUP), last)

    def test_backup_path_appears_near_the_end(self):
        text = format_result(SOURCE, DEST, {"/a.xml": 3}, 0, [], BACKUP)
        self.assertIn(str(BACKUP), text)

    def test_reports_replacement_totals(self):
        text = format_result(SOURCE, DEST, {"/a.xml": 3, "/b.xml": 2}, 0, [], BACKUP)
        self.assertIn("5", text)
        self.assertIn("2 files", text)

    def test_surviving_references_are_flagged(self):
        text = format_result(SOURCE, DEST, {"/a.xml": 3}, 4, [], BACKUP)
        self.assertIn("4", text)
        self.assertIn("still", text.lower())

    def test_hardcoded_path_warnings_are_shown(self):
        text = format_result(
            SOURCE, DEST, {"/a.xml": 3}, 0, ["/x/.idea/runConfigurations/a.xml"], BACKUP
        )
        self.assertIn("runConfigurations", text)

    def test_mentions_reindexing(self):
        text = format_result(SOURCE, DEST, {"/a.xml": 3}, 0, [], BACKUP)
        self.assertIn("index", text.lower())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_report -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'idea_migrate.report'`

- [ ] **Step 3: Write the implementation**

`src/idea_migrate/report.py`:

```python
"""Format what the tool is about to do, and what it did.

The result report ends with the backup location and the undo command for this
run. That placement is deliberate: it is the thing the user needs when something
has gone wrong, so it must not be buried in the middle of a wall of output.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


def format_plan(
    source: Path,
    dest: Path,
    reference_count: int,
    product_count: int,
    backup_root: Path,
) -> str:
    """Describe the migration that is about to run."""
    return "\n".join(
        [
            "Planned migration",
            "",
            f"  Move:    {source}",
            f"      ->   {dest}",
            "",
            f"  Found {reference_count} stored path references across "
            f"{product_count} installed JetBrains products.",
            f"  A full backup of their settings will be written under {backup_root}",
            "",
        ]
    )


def format_result(
    source: Path,
    dest: Path,
    rewritten: dict[str, int],
    remaining: int,
    hardcoded_warnings: Sequence[str],
    backup_dir: Path,
) -> str:
    """Describe a completed migration, ending with the undo command."""
    total = sum(rewritten.values())
    lines = [
        "Migration complete",
        "",
        f"  Moved:   {source}",
        f"      ->   {dest}",
        f"  Repaired {total} path references in {len(rewritten)} files.",
    ]

    if remaining:
        lines.append(
            f"  WARNING: {remaining} references to the old path still remain. "
            "Inspect them before opening the IDEs."
        )

    if hardcoded_warnings:
        lines.extend(["", "  These project files contain hardcoded absolute paths"])
        lines.append("  and may need editing by hand:")
        lines.extend(f"    {path}" for path in hardcoded_warnings)

    lines.extend(
        [
            "",
            "  Each IDE will re-index the moved projects the first time it starts,",
            "  because its caches are keyed by path. This is slow but self-healing.",
            "",
            "-" * 72,
            f"Backup saved to: {backup_dir}",
            "This backup is never deleted automatically. To reverse this migration:",
            f"  {backup_dir / 'undo.sh'}",
        ]
    )
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_report -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Commit**

```bash
cd ~/Projects/idea-migrate
git add src/idea_migrate/report.py tests/test_report.py
git commit -m "feat: format the plan and result reports"
```

---

### Task 13: Command-line interface

**Model:** Sonnet 5

**Files:**
- Create: `src/idea_migrate/cli.py`
- Create: `src/idea_migrate/__main__.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: every module built so far
- Produces:
  - `idea_migrate.cli.build_parser() -> argparse.ArgumentParser`
  - `idea_migrate.cli.main(argv: Sequence[str] | None = None) -> int`
  - `idea_migrate.cli.run_migration(args, home: Path, now: datetime, ps_output: str | None = None) -> int`

**Context:** Three invocation shapes must all work:

```
idea-migrate --source PATH --dest PATH
idea-migrate backups
idea-migrate undo <backup-dir>
```

Use argparse subparsers with `required=False` so a bare invocation with only `--source` and `--dest` falls through to the migration path. Top-level flags: `--source`, `--dest`, `--dry-run`, `--config`, `--yes` (skip the confirmation prompt, for non-interactive use).

Every `MigrateError` is caught in `main` and printed as a single clean line to stderr, returning exit code 1. Tracebacks are for bugs, not for expected conditions.

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py`:

```python
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

from idea_migrate.cli import build_parser, main, run_migration

QUIET = "/usr/sbin/cfprefsd\n"
RUNNING = "/Applications/IntelliJ IDEA.app/Contents/MacOS/idea\n"
NOW = datetime(2026, 8, 29, 14, 30, 5)


class TestParser(unittest.TestCase):
    def test_bare_flags_parse_as_a_migration(self):
        args = build_parser().parse_args(["--source", "/a", "--dest", "/b"])
        self.assertIsNone(args.command)
        self.assertEqual(args.source, "/a")
        self.assertEqual(args.dest, "/b")

    def test_backups_subcommand(self):
        args = build_parser().parse_args(["backups"])
        self.assertEqual(args.command, "backups")

    def test_undo_subcommand_takes_a_directory(self):
        args = build_parser().parse_args(["undo", "/some/backup"])
        self.assertEqual(args.command, "undo")
        self.assertEqual(args.backup_dir, "/some/backup")

    def test_dry_run_flag(self):
        args = build_parser().parse_args(["--source", "/a", "--dest", "/b", "--dry-run"])
        self.assertTrue(args.dry_run)


class MigrationTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        self.source = self.home / "WebstormProjects"
        (self.source / "alpha").mkdir(parents=True)
        (self.home / "Projects").mkdir()
        self.dest = self.home / "Projects" / "WebstormProjects"

        product = self.home / "Library" / "Application Support" / "JetBrains" / "WebStorm2026.2"
        (product / "options").mkdir(parents=True)
        (product / "workspace").mkdir(parents=True)
        (product / "options" / "recentProjects.xml").write_text(
            '<application><entry key="$USER_HOME$/WebstormProjects/alpha" /></application>',
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _args(self, **overrides):
        argv = ["--source", str(self.source), "--dest", str(self.dest), "--yes"]
        for key, value in overrides.items():
            argv.extend([key, value] if value is not None else [key])
        return build_parser().parse_args(argv)


class TestRunMigration(MigrationTestCase):
    def test_dry_run_changes_nothing(self):
        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(self.dest), "--dry-run", "--yes"]
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = run_migration(args, self.home, NOW, ps_output=QUIET)
        self.assertEqual(code, 0)
        self.assertTrue(self.source.is_dir())
        self.assertFalse(self.dest.exists())
        self.assertFalse((self.home / "Idea-Migration-Backups").exists())

    def test_apply_moves_rewrites_and_backs_up(self):
        args = self._args()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = run_migration(args, self.home, NOW, ps_output=QUIET)
        self.assertEqual(code, 0)
        self.assertFalse(self.source.exists())
        self.assertTrue(self.dest.is_dir())

        config = (
            self.home
            / "Library"
            / "Application Support"
            / "JetBrains"
            / "WebStorm2026.2"
            / "options"
            / "recentProjects.xml"
        )
        self.assertIn(
            "$USER_HOME$/Projects/WebstormProjects/alpha",
            config.read_text(encoding="utf-8"),
        )

        backups = list((self.home / "Idea-Migration-Backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "manifest.json").is_file())
        self.assertTrue((backups[0] / "undo.sh").is_file())

    def test_output_ends_with_the_undo_command(self):
        args = self._args()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            run_migration(args, self.home, NOW, ps_output=QUIET)
        self.assertIn("undo.sh", buffer.getvalue().rstrip().splitlines()[-1])

    def test_refuses_while_an_ide_is_running(self):
        args = self._args()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = run_migration(args, self.home, NOW, ps_output=RUNNING)
        self.assertEqual(code, 1)
        self.assertTrue(self.source.is_dir())


class TestMainErrorHandling(unittest.TestCase):
    def test_invalid_path_prints_one_line_and_returns_one(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            code = main(["--source", "/definitely/not/here", "--dest", "/tmp/x", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("does not exist", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_cli -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'idea_migrate.cli'`

- [ ] **Step 3: Write the implementation**

`src/idea_migrate/cli.py`:

```python
"""Command-line interface.

Three shapes are supported:

    idea-migrate --source PATH --dest PATH
    idea-migrate backups
    idea-migrate undo <backup-dir>

Errors the tool raises deliberately are printed as a single line. A traceback
means a bug, not a user mistake.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from . import __version__
from .backup import back_up_products, new_backup_dir, write_undo_script
from .completion import prompt_for_path
from .config import load_config
from .errors import MigrateError
from .listing import find_backups, format_backups
from .manifest import Manifest, write_manifest
from .mover import assert_no_ide_running, move_directory
from .paths import validate_move
from .products import find_product_dirs
from .report import format_plan, format_result
from .rewrite import count_references, prefix_variants, rewrite_products
from .undo import undo_backup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="idea-migrate",
        description=(
            "Move a JetBrains project directory and repair the stored path "
            "references in every installed IDE."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--source", help="Directory to move. Prompted for if omitted.")
    parser.add_argument("--dest", help="Where it should go. Prompted for if omitted.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and write nothing.",
    )
    parser.add_argument("--config", help="Path to a TOML configuration file.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt.",
    )

    sub = parser.add_subparsers(dest="command", required=False)
    sub.add_parser("backups", help="List every backup and its undo command.")
    undo_parser = sub.add_parser("undo", help="Roll back a previous migration.")
    undo_parser.add_argument("backup_dir", help="The backup directory to roll back.")
    return parser


def _find_hardcoded_paths(root: Path, home: Path) -> list[str]:
    """Return .idea files under ``root`` that contain absolute paths."""
    warnings: list[str] = []
    needle = str(home)
    for idea_dir in root.rglob(".idea"):
        if not idea_dir.is_dir():
            continue
        for xml_file in idea_dir.rglob("*.xml"):
            try:
                if needle in xml_file.read_text(encoding="utf-8"):
                    warnings.append(str(xml_file))
            except (OSError, UnicodeDecodeError):
                continue
    return sorted(warnings)


def run_migration(
    args: argparse.Namespace,
    home: Path,
    now: datetime,
    ps_output: str | None = None,
) -> int:
    """Run a migration. Returns a process exit code."""
    config = load_config(Path(args.config) if args.config else None, home)

    source = args.source or prompt_for_path("Source directory: ")
    dest = args.dest or prompt_for_path("Destination directory: ")

    try:
        spec = validate_move(source, dest, home)
        assert_no_ide_running(ps_output)
    except MigrateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    products = find_product_dirs(config.jetbrains_root, config.exclude_products)
    variants = prefix_variants(spec.source, spec.dest, spec.home)
    reference_count = count_references(products, variants)

    print(format_plan(spec.source, spec.dest, reference_count, len(products), config.backup_root))

    if args.dry_run:
        changed = rewrite_products(products, variants, dry_run=True)
        print(f"Dry run: {len(changed)} files would be modified. Nothing was written.")
        return 0

    if not args.yes:
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled. Nothing was changed.")
            return 0

    try:
        backup_dir = new_backup_dir(config.backup_root, now)
        backed_up = back_up_products(products, backup_dir)
        write_undo_script(backup_dir)

        manifest = Manifest(
            version=1,
            tool_version=__version__,
            created_at=now.isoformat(),
            home=str(spec.home),
            source=str(spec.source),
            dest=str(spec.dest),
            move_status="pending",
            backed_up_products=backed_up,
            rewritten_files={},
            undone_at=None,
        )
        write_manifest(backup_dir, manifest)

        move_directory(spec)
        rewritten = rewrite_products(products, variants)
        remaining = count_references(products, variants)

        write_manifest(
            backup_dir,
            Manifest(
                version=manifest.version,
                tool_version=manifest.tool_version,
                created_at=manifest.created_at,
                home=manifest.home,
                source=manifest.source,
                dest=manifest.dest,
                move_status="moved",
                backed_up_products=backed_up,
                rewritten_files=rewritten,
                undone_at=None,
            ),
        )
    except MigrateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    warnings = _find_hardcoded_paths(spec.dest, spec.home)
    print(
        format_result(
            spec.source, spec.dest, rewritten, remaining, warnings, backup_dir
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    home = Path.home()
    now = datetime.now()

    try:
        if args.command == "backups":
            config = load_config(Path(args.config) if args.config else None, home)
            print(format_backups(find_backups(config.backup_root), config.backup_root))
            return 0
        if args.command == "undo":
            config = load_config(Path(args.config) if args.config else None, home)
            for line in undo_backup(Path(args.backup_dir), config.jetbrains_root, now):
                print(line)
            return 0
        return run_migration(args, home, now)
    except MigrateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

`src/idea_migrate/__main__.py`:

```python
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_cli -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Run the whole suite**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest discover -s tests -t . -v`
Expected: PASS, all tests from Tasks 1-13

- [ ] **Step 6: Commit**

```bash
cd ~/Projects/idea-migrate
git add src/idea_migrate/cli.py src/idea_migrate/__main__.py tests/test_cli.py
git commit -m "feat: add the command-line interface"
```

---

### Task 14: Round-trip integration test

**Model:** Opus 5

**Files:**
- Test: `tests/test_roundtrip.py`

**Interfaces:**
- Consumes: `idea_migrate.cli.run_migration`, `idea_migrate.undo.undo_backup`
- Produces: nothing — this task adds only a test

**Context:** This is the test that proves the safety promise. It builds a complete synthetic home directory with several products and realistic configuration, runs a migration, then undoes it, and asserts the tree is byte-identical to how it started. If this passes, the undo genuinely works.

Compare by walking the tree and hashing every file, so the assertion covers content and layout, not just a spot check. Exclude the backup directory from the comparison, since it is expected to exist afterwards — the tool never deletes backups.

- [ ] **Step 1: Write the test**

`tests/test_roundtrip.py`:

```python
import hashlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from idea_migrate.cli import build_parser, run_migration
from idea_migrate.undo import undo_backup

QUIET = "/usr/sbin/cfprefsd\n"
NOW = datetime(2026, 8, 29, 14, 30, 5)
LATER = datetime(2026, 8, 30, 9, 0, 0)

RECENT_PROJECTS = """\
<application>
  <component name="RecentProjectsManager">
    <option name="additionalInfo">
      <map>
        <entry key="$USER_HOME$/WebstormProjects/alpha">
          <value><RecentProjectMetaInfo projectWorkspaceId="AAA" /></value>
        </entry>
        <entry key="$USER_HOME$/WebstormProjectsArchive/beta">
          <value><RecentProjectMetaInfo projectWorkspaceId="BBB" /></value>
        </entry>
        <entry key="$USER_HOME$/Downloads/gamma">
          <value><RecentProjectMetaInfo projectWorkspaceId="CCC" /></value>
        </entry>
      </map>
    </option>
  </component>
</application>
"""

WORKSPACE = (
    '<project><component name="X" path="$USER_HOME$/WebstormProjects/alpha" />'
    "</project>\n"
)


def snapshot(root: Path, skip: Path) -> dict[str, str]:
    """Hash every file under ``root``, ignoring anything under ``skip``."""
    digest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if skip in path.parents or path == skip:
            continue
        key = str(path.relative_to(root))
        if path.is_dir():
            digest[key] = "<dir>"
        elif path.is_file():
            digest[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()

        for project in ("alpha", "beta"):
            (self.home / "WebstormProjects" / project / ".idea").mkdir(parents=True)
            (self.home / "WebstormProjects" / project / "index.js").write_text(
                f"// {project}\n", encoding="utf-8"
            )
        (self.home / "WebstormProjectsArchive" / "beta").mkdir(parents=True)
        (self.home / "Downloads" / "gamma").mkdir(parents=True)
        (self.home / "Projects").mkdir()

        jetbrains = self.home / "Library" / "Application Support" / "JetBrains"
        for name in ("WebStorm2026.2", "IntelliJIdea2026.2", "PyCharm2025.3"):
            product = jetbrains / name
            (product / "options").mkdir(parents=True)
            (product / "workspace").mkdir(parents=True)
            (product / "plugins").mkdir(parents=True)
            (product / "options" / "recentProjects.xml").write_text(
                RECENT_PROJECTS, encoding="utf-8"
            )
            (product / "workspace" / "AAA.xml").write_text(WORKSPACE, encoding="utf-8")
            (product / "plugins" / "big.jar").write_text("x" * 50, encoding="utf-8")

        self.jetbrains = jetbrains
        self.backup_root = self.home / "Idea-Migration-Backups"
        self.source = self.home / "WebstormProjects"
        self.dest = self.home / "Projects" / "WebstormProjects"

    def tearDown(self):
        self._tmp.cleanup()

    def test_migrate_then_undo_restores_everything(self):
        before = snapshot(self.home, self.backup_root)

        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(self.dest), "--yes"]
        )
        import io
        from contextlib import redirect_stdout

        with redirect_stdout(io.StringIO()):
            code = run_migration(args, self.home, NOW, ps_output=QUIET)
        self.assertEqual(code, 0)

        # The migration really happened.
        self.assertFalse(self.source.exists())
        self.assertTrue(self.dest.is_dir())
        config = self.jetbrains / "WebStorm2026.2" / "options" / "recentProjects.xml"
        text = config.read_text(encoding="utf-8")
        self.assertIn("$USER_HOME$/Projects/WebstormProjects/alpha", text)
        # The near-miss sibling and the unrelated path were left alone.
        self.assertIn("$USER_HOME$/WebstormProjectsArchive/beta", text)
        self.assertIn("$USER_HOME$/Downloads/gamma", text)

        backups = sorted(self.backup_root.iterdir())
        self.assertEqual(len(backups), 1)

        with redirect_stdout(io.StringIO()):
            undo_backup(backups[0], self.jetbrains, LATER, ps_output=QUIET)

        after = snapshot(self.home, self.backup_root)
        self.assertEqual(before, after)

    def test_backup_survives_the_undo(self):
        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(self.dest), "--yes"]
        )
        import io
        from contextlib import redirect_stdout

        with redirect_stdout(io.StringIO()):
            run_migration(args, self.home, NOW, ps_output=QUIET)
        backup = sorted(self.backup_root.iterdir())[0]
        with redirect_stdout(io.StringIO()):
            undo_backup(backup, self.jetbrains, LATER, ps_output=QUIET)
        self.assertTrue(backup.is_dir())
        self.assertTrue((backup / "manifest.json").is_file())
        self.assertTrue((backup / "undo.sh").is_file())

    def test_all_products_are_repaired_not_just_one(self):
        args = build_parser().parse_args(
            ["--source", str(self.source), "--dest", str(self.dest), "--yes"]
        )
        import io
        from contextlib import redirect_stdout

        with redirect_stdout(io.StringIO()):
            run_migration(args, self.home, NOW, ps_output=QUIET)

        for name in ("WebStorm2026.2", "IntelliJIdea2026.2", "PyCharm2025.3"):
            config = self.jetbrains / name / "options" / "recentProjects.xml"
            self.assertIn(
                "$USER_HOME$/Projects/WebstormProjects/alpha",
                config.read_text(encoding="utf-8"),
                f"{name} was not repaired",
            )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest tests.test_roundtrip -v`
Expected: PASS, 3 tests. If the round trip fails, the undo is broken — fix `undo.py` or `backup.py`, do not weaken the assertion.

- [ ] **Step 3: Run the whole suite**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m unittest discover -s tests -t . -v`
Expected: PASS, every test

- [ ] **Step 4: Commit**

```bash
cd ~/Projects/idea-migrate
git add tests/test_roundtrip.py
git commit -m "test: add migrate-then-undo round trip integration test"
```

---

### Task 15: README

**Model:** Haiku 4.5

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: the finished tool
- Produces: nothing code depends on

- [ ] **Step 1: Write the README**

Write `README.md` covering, in this order:

1. **What it does**, in two sentences: moves a JetBrains project directory to a new location and repairs the stored path references in every installed IDE so Recent Projects keeps working.
2. **Requirements**: macOS, Python 3.11 or newer, no third-party dependencies.
3. **Install**: `pip install -e .` from the repository root, or run without installing via `PYTHONPATH=src python3 -m idea_migrate`.
4. **Usage**, showing all four forms with a one-line explanation each:
   ```
   idea-migrate --source ~/WebstormProjects --dest ~/Projects/WebstormProjects
   idea-migrate                     # prompts for both paths, with tab completion
   idea-migrate --dry-run --source ~/X --dest ~/Y
   idea-migrate backups
   idea-migrate undo ~/Idea-Migration-Backups/2026-08-29_143005
   ```
5. **Quit your IDEs first** — explain that a running IDE writes its settings on exit and would overwrite the repairs, which is why the tool refuses to run while one is open.
6. **Backups** — where they go (`~/Idea-Migration-Backups`), that the tool never deletes them, that each contains a standalone `undo.sh` which takes an optional path argument so it works after the folder is moved, and that `idea-migrate backups` lists them if you forget.
7. **What it does not touch** — projects' own `.idea` directories (they use the `$PROJECT_DIR$` placeholder and are already portable), IDE caches (path-keyed, they rebuild themselves, so expect one slow re-index after the move).
8. **Configuration** — the optional TOML file, with an example showing `backup_root` and `exclude_products`.
9. **Running the tests**: `PYTHONPATH=src python3 -m unittest discover -s tests -t . -v`.

Write it as prose with code blocks, not as a bulleted outline. Do not include badges, a licence section, or a contributing section.

- [ ] **Step 2: Verify the commands in the README actually work**

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m idea_migrate --help`
Expected: the help text prints, exit code 0

Run: `cd ~/Projects/idea-migrate && PYTHONPATH=src python3 -m idea_migrate backups`
Expected: prints "No backups found in ~/Idea-Migration-Backups" (or lists real backups if any exist), exit code 0

- [ ] **Step 3: Commit**

```bash
cd ~/Projects/idea-migrate
git add README.md
git commit -m "docs: add README"
```

---

## Verification

After all tasks, the full suite must pass:

```bash
cd ~/Projects/idea-migrate
PYTHONPATH=src python3 -m unittest discover -s tests -t . -v
```

Then a real dry run against the actual machine, which writes nothing:

```bash
cd ~/Projects/idea-migrate
PYTHONPATH=src python3 -m idea_migrate --dry-run --yes \
  --source ~/WebstormProjects --dest ~/Projects/WebstormProjects
```

Expected: reports roughly 11 stored references (9 in WebStorm's own recent list, 2 in PyCharm's, plus any in workspace files) across 15 products, and confirms nothing was written. Do **not** run without `--dry-run` until the user asks.
