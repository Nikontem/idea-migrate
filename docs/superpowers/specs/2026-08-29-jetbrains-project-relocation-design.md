# idea-migrate — design

**Date:** 2026-08-29
**Status:** approved design, pending implementation plan

## 1. The problem

JetBrains IDEs (IntelliJ IDEA, PyCharm, WebStorm, GoLand, DataGrip) each create their own
project folder in the user's home directory — `~/IdeaProjects`, `~/PycharmProjects`,
`~/WebstormProjects`, and so on. The goal is to consolidate these under a single home for all
projects, `~/Projects`, while keeping them grouped by IDE rather than flattened together.

So `~/WebstormProjects` becomes `~/Projects/WebstormProjects`, `~/IdeaProjects` becomes
`~/Projects/IdeaProjects`, and so on.

Moving the folder is the easy half. The hard half is that each IDE stores absolute paths to
every project it knows about, so after a move the Recent Projects list points at folders that no
longer exist. `idea-migrate` performs the move and repairs those references, with a backup and a
working undo.

## 2. Goals

- Move one directory to a new location, specified by the user.
- Update every JetBrains IDE's stored references so Recent Projects, trusted paths, and
  per-project window state all resolve correctly after the move.
- Make path entry painless, with terminal-style tab completion.
- Make the whole operation reversible, with backups the tool never deletes.

## 3. Non-goals

- Guessing what should be moved. The user says what moves and where it goes. The tool does not
  scan for candidates or make recommendations.
- Flattening projects together. Grouping by IDE is the point.
- Touching the `* copy` folders that already exist in `~/Projects`. These are the user's own
  manual backups and are left entirely alone for manual cleanup later.
- Backing up or repairing IDE caches. Those are keyed by path and rebuild themselves.

## 4. What the investigation established

These facts were verified on the target machine and are constraints on the implementation, not
assumptions.

**Projects are already portable.** The `.idea` directory inside each project uses the
`$PROJECT_DIR$` placeholder rather than absolute paths. Moving a project folder does not break
anything inside it. The one place people do hardcode absolute paths is run configurations, so the
tool warns about those rather than assuming they are clean.

**Everything that breaks is IDE-level configuration**, stored as XML under
`~/Library/Application Support/JetBrains/<Product><Version>/`. On the target machine 15 such
product directories exist across 5 products and 3 versions each. The files holding paths are
`options/recentProjects.xml` (the Recent Projects list itself), `options/trusted-paths.xml`,
`options/other.xml`, `options/vcs-inputs.xml`, and roughly 20 files under `workspace/` holding
per-project window and editor state. Each Recent Projects entry carries a `projectWorkspaceId`
that names its corresponding file in `workspace/`.

**Paths appear in a placeholder form.** Entries are keyed like
`$USER_HOME$/IdeaProjects/my-project`, where `$USER_HOME$` stands for the home directory. Both
the placeholder form and the fully expanded form must be handled.

**Every IDE must be rewritten, not just the obvious one.** PyCharm's recent list contains two
entries under `WebstormProjects`; IntelliJ's contains entries under both `WebstormProjects` and
`Projects`. So moving a single root still requires sweeping all 15 product directories — there
is no shortcut of "this root belongs to that IDE".

**The filesystem is case-insensitive.** `~/PycharmProjects` and `~/PyCharmProjects` report the
same inode (10549211) — one physical directory answering to either spelling. This has a direct
consequence for the rewrite, covered in Phase 4.

**Sizes.** The candidate roots total roughly 11 GB, all on the same APFS volume with ample free
space. Because source and destination share a volume, the move is an atomic instant rename rather
than a copy. The IDE configuration directories total 23 GB, but almost all of that is plugins;
the `options/` and `workspace/` directories we actually touch total about 36 MB across all 15
products, so backing up all of them on every run is effectively free.

## 5. Design

Four phases. One source and one destination per run — to move several roots, run it several
times. Nothing is written until the user confirms.

### Phase 1 — Input

The user supplies two paths:

```
idea-migrate --source ~/WebstormProjects --dest ~/Projects/WebstormProjects
```

If either flag is omitted, the tool prompts for it interactively **with tab completion, the way
a shell behaves**. This is the part that has to feel right, so it is specified precisely:

- Completion is driven by the standard library `readline` module, with a custom completer that
  completes filesystem paths.
- `~` is expanded before completion and before use.
- Only directories are offered, since only directories are being moved, and a completed
  directory gets a trailing `/` appended so the user can keep typing deeper without retyping the
  separator.
- Completion must work for a partially typed final component, not only for whole directories —
  typing `~/Webst` then Tab completes to `~/WebstormProjects/`.
- Two readline implementations exist and they need different key bindings. GNU readline needs
  `readline.parse_and_bind("tab: complete")`; the libedit build that ships with some macOS
  Pythons needs `readline.parse_and_bind("bind ^I rl_complete")`. Detect which is present by
  checking whether `"libedit"` appears in `readline.__doc__` and bind accordingly. The target
  machine has GNU readline, but the tool must not break on a Python that has libedit.
- If `readline` cannot be imported at all, fall back to plain `input()` without completion rather
  than failing.

Validation, all of which abort the run with a clear message:

- The source exists and is a directory.
- The source is not the home directory itself.
- The destination does not already exist.
- The destination's parent exists and is writable.
- The destination is not inside the source. Without this check,
  `--source ~/Projects --dest ~/Projects/sub` would attempt to move a directory into itself.
- The source is not already inside the destination — that migration has already happened.
- Source and destination are on the same filesystem device, so the move is an atomic rename. If
  they are not, fall back to copy, verify, then remove.

Then the tool prints exactly what it will do — the move, the number of configuration files that
reference the source, and where the backup will go — and asks for confirmation.

### Phase 2 — Back up

Before anything is modified, copy `options/` and `workspace/` from *every* product directory
(not just the ones that will change) into a fresh timestamped directory:

```
~/Idea-Migration-Backups/
  2026-08-29_143005/
    manifest.json
    undo.sh
    config/
      IntelliJIdea2026.2/options/…
      IntelliJIdea2026.2/workspace/…
      …
```

The backup root is a single fixed, visible location so the undo script always has a standard
place to start from. It defaults to `~/Idea-Migration-Backups` and is configurable.

**The tool never deletes a backup.** There is no retention policy, no pruning, no cleanup flag.
Each run adds a directory; removing them is a manual action by the user.

The generated `undo.sh` is a standalone shell script that does not depend on the Python tool
being functional. By default it resolves its own directory and works from there. It also accepts
an optional path argument (`./undo.sh /some/other/path`) so it still works if the backup folder
has been moved or renamed.

Product directories are found by scanning `~/Library/Application Support/JetBrains/` for any
directory containing an `options/` subdirectory. This is structural rather than a hardcoded list
of product names, so a newly installed product or version is picked up automatically and
non-product directories such as `Toolbox`, `consentOptions`, and `PrivacyPolicy` are excluded
without needing to be named.

### Phase 3 — Move

Preflight: refuse to run if any JetBrains process is alive. A running IDE holds this
configuration in memory and writes it out on exit, which would silently overwrite our repairs.

Then `os.rename(source, dest)`.

### Phase 4 — Rewrite

For each product directory, scan `options/*.xml` and `workspace/*.xml` for the old path. The
source generates several prefix variants to search for: the placeholder form
(`$USER_HOME$/WebstormProjects`), the expanded form (`/Users/<user>/WebstormProjects`), and
`file://` URL forms of both. Each is replaced with the corresponding form of the destination.

**Matching must be boundary-anchored.** A prefix only matches when followed by `/` or by the
closing quote of the attribute, so `WebstormProjects` never matches `WebstormProjectsArchive`.

**Matching must be case-insensitive, and replacement must preserve the rest of the path.**
Because the filesystem is case-insensitive, different IDEs may have recorded the same directory
under different spellings — the `PycharmProjects` / `PyCharmProjects` case above is exactly this.
A case-sensitive search for the spelling the user happened to type would silently miss entries
written with the other spelling, leaving broken references behind with no error. So the prefix is
matched case-insensitively, and only the matched prefix is replaced; everything after it is left
byte-for-byte alone.

**Editing is done as text, not by re-serializing XML.** Parsing with an XML library and writing
the tree back reorders attributes and normalizes whitespace across the entire file, turning a
three-line change into an unreviewable diff and risking loss of formatting the IDE may care
about. Instead: perform targeted text replacement on the raw file content, then parse the
*result* to confirm it is still well-formed XML, and only write if that check passes. The safety
gain over a `sed` one-liner is the verification step and the anchored matching, not the parser.

After rewriting, re-scan and report any surviving references to the old path, and separately warn
about any project whose own `.idea` directory contains a hardcoded absolute path — most commonly
in run configurations.

### Final output

The run ends with the backup location set apart as the last thing printed: the absolute path of
the backup directory, and the exact undo command for that specific run, ready to copy. It is the
final line, not a passing mention earlier in the report.

The report also notes that each IDE will re-index the moved projects on next launch, because the
caches under `~/Library/Caches/JetBrains/` are keyed by path. This is slow but self-healing.

## 6. The manifest

`manifest.json` in each backup directory records everything needed to undo the run and to
describe it later:

- Tool version, timestamp, and the home directory the run targeted.
- The source and destination of the move, and the status actually achieved.
- Every product configuration directory backed up, with its relative path inside `config/`.
- Every file modified during the rewrite phase, with a count of replacements made.
- `undone_at`: null initially, written with a timestamp when the run is rolled back.

That last field matters for the `backups` command — a backup that has already been rolled back
must be visibly marked, rather than offering an undo that would do something surprising.

## 7. Command-line surface

```
idea-migrate --source PATH --dest PATH   # move and repair; prompts for whichever is missing
idea-migrate --dry-run --source … --dest …   # print the plan, write nothing
idea-migrate backups                     # list all backups: when, what moved, size, undone or not
idea-migrate undo <backup-dir>           # roll back a specific run
```

Running with no arguments prompts for both paths with tab completion. `--dry-run` prints the full
plan including which configuration files would change, and writes nothing.

`backups` reads `manifest.json` from every directory under the backup root, so it stays accurate
without the tool maintaining state anywhere else. For each backup it shows the timestamp, what
that run moved and where to, the size on disk, whether it has been undone, and the undo command
for that specific backup.

## 8. Configuration

An optional TOML file read with the standard library's `tomllib`, holding the backup root and any
product directories to exclude. Every setting has a working default, so the file is not required.

## 9. Constraints on the implementation

- Python 3.11 or newer (3.14 is installed). Standard library only — no third-party dependencies.
- Nothing writes without explicit confirmation, and `--dry-run` never writes.
- Every phase is a separate, independently testable module.
- Refuse to run while any JetBrains IDE is alive.
- Never delete a backup.

## 10. Testing

- Unit tests for the rewrite function against fixture XML files copied from the real
  configuration with paths anonymized. These must include the near-miss cases that must *not*
  match: a longer sibling directory name sharing a prefix, the path appearing inside unrelated
  attribute text, both placeholder and expanded forms in the same file, and the same directory
  recorded under two different capitalizations.
- A malformed-XML fixture to confirm the post-rewrite well-formedness check actually rejects.
- Path validation tested against a synthetic tree: destination inside source, source inside
  destination, source is home, destination already exists, cross-device.
- The completer tested directly as a function — given a partial path, it returns the expected
  directory candidates — so the tests do not need a terminal.
- Move and preflight tested against the synthetic tree, including the refuse-if-IDE-running and
  destination-exists paths.
- A full round trip: run against the synthetic tree, then undo, then assert the tree is
  byte-identical to its starting state.
- No test touches the real home directory or the real JetBrains configuration.
