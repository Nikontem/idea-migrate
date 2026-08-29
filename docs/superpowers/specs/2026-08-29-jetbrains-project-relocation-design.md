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

Moving the folders is the easy half. The hard half is that each IDE stores absolute paths to
every project it knows about, so after a move the Recent Projects list points at folders that
no longer exist. `idea-migrate` performs the move and repairs those references, with a backup
and a working undo.

## 2. Goals

- Relocate selected per-IDE project roots into a destination directory, preserving the
  per-IDE grouping.
- Update every JetBrains IDE's stored references so Recent Projects, trusted paths, and
  per-project window state all resolve correctly after the move.
- Make the whole operation reversible, with backups the tool never deletes.
- Be reusable: the tool discovers what is installed rather than hardcoding this one migration.

## 3. Non-goals

- Flattening projects into a single directory. The user explicitly wants IDE grouping.
- Touching the `* copy` folders that already exist in `~/Projects`. These are the user's own
  manual backups and are left entirely alone for manual cleanup later.
- Moving projects that already live in the destination, or projects outside any IDE root
  (for example ones opened from `~/Downloads`).
- Backing up or repairing IDE caches. Those are keyed by path and rebuild themselves.

## 4. What the investigation established

These facts were verified on the target machine and drive the design. Anyone implementing
against this spec should treat them as constraints, not assumptions.

**Projects are already portable.** The `.idea` directory inside each project uses the
`$PROJECT_DIR$` placeholder rather than absolute paths. Moving a project folder does not break
anything inside it. The one place people do hardcode absolute paths is run configurations, so
the tool warns about those rather than assuming they are clean.

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

**IDEs cross-reference each other's roots.** PyCharm's recent list contains two entries under
`WebstormProjects`; IntelliJ's contains entries under both `WebstormProjects` and `Projects`.
The rewrite therefore cannot be "each IDE fixes its own root" — it must be a single global map
of old prefix to new prefix, applied to every product directory.

**Two spellings, one directory.** `~/PycharmProjects` and `~/PyCharmProjects` report the same
inode (10549211). The filesystem is case-insensitive, so these are one physical directory that
answers to either spelling. Discovery must deduplicate by inode or it will try to move the same
folder twice.

**Some entries are not project roots.** Recent lists also reference `~/Downloads`, `~/offline`,
and `~/.claude`. These must be visible but not selected by default.

**Sizes.** The project roots total roughly 11 GB, all on the same APFS volume with ample free
space. Because source and destination share a volume, each move is an atomic instant rename
rather than a copy. The IDE configuration directories total 23 GB, but almost all of that is
plugins; the `options/` and `workspace/` directories that we actually touch total about 36 MB
across all 15 products, so backing up all of them every run is effectively free.

## 5. Design

Five phases. Each is inspectable, and nothing is written until the user confirms a plan.

### Phase 1 — Discover

Find installed IDEs by scanning `~/Library/Application Support/JetBrains/` for any directory
containing an `options/` subdirectory. This is deliberately structural rather than a hardcoded
list of product names, so a newly installed product or version is picked up automatically and
non-product directories (`Toolbox`, `consentOptions`, `PrivacyPolicy`) are excluded without
needing to be named.

For each product, parse `options/recentProjects.xml` and collect every project path from the
`<entry key="...">` attributes, expanding `$USER_HOME$` to the real home directory.

Group those paths by parent directory to derive candidate roots. **The home directory itself is
never a candidate root.** Some projects sit directly in the home directory — the target machine
has `~/PyCharmMiscProject` — and grouping by parent would otherwise nominate `~` for relocation,
which would be catastrophic. Such projects are reported in a separate "not part of any root"
list and left alone.

For each candidate, record its
real path, its inode, its true on-disk spelling (found by listing the parent directory and
matching case-insensitively, since the filesystem itself will answer to any casing), the number
of projects it contains, its size, and which IDEs reference it.

Deduplicate candidates by `(device, inode)`.

### Phase 2 — Select

Present the candidate roots with their project count, size, referencing IDEs, and the proposed
destination path. The user selects which to migrate.

Defaults, stated as an explicit rule rather than a judgement call: a candidate is pre-selected
when it is a direct child of the home directory *and* its name ends with `Projects`,
case-insensitively. That matches `IdeaProjects`, `PycharmProjects`, `WebstormProjects`,
`GolandProjects`, and `DataGripProjects`, and excludes `Downloads`, `offline`, and `.claude`,
which are still listed so the user can select them deliberately if they want. Anything already
inside the destination is marked as already done and cannot be selected.

Note that moving a root moves every project inside it, including projects that no IDE currently
lists in its recent history. This is intended — the root is the unit of migration.

### Phase 3 — Back up

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
place to start from. It is configurable, but defaults to `~/Idea-Migration-Backups`.

**The tool never deletes a backup.** There is no retention policy, no pruning, and no cleanup
flag. Each run adds a directory; removing them is a manual action by the user.

The generated `undo.sh` is a standalone shell script that does not depend on the Python tool
being functional. By default it resolves its own directory and works from there. It also accepts
an optional path argument (`./undo.sh /some/other/path`) so it still works if the backup folder
has been moved or renamed.

### Phase 4 — Move

Preflight checks, all of which abort the run if they fail:

- No JetBrains process is running. A running IDE holds this configuration in memory and writes
  it out on exit, which would silently overwrite our repairs.
- Source and destination are on the same filesystem device, so the move is an atomic rename.
  If not, fall back to copy-then-verify-then-remove.
- The destination path does not already exist.
- The destination's parent directory exists and is writable.

Then `os.rename` each selected root.

### Phase 5 — Rewrite

For each product directory, scan `options/*.xml` and `workspace/*.xml` for the old path
prefixes. Each moved root generates several prefix variants to search for: the placeholder form
(`$USER_HOME$/IdeaProjects`), the expanded form (`/Users/<user>/IdeaProjects`), and `file://`
URL forms of both.

**Matching must be boundary-anchored.** A prefix only matches when followed by `/` or by the
closing quote of the attribute, so `IdeaProjects` never matches `IdeaProjectsArchive`.

**Editing is done as text, not by re-serializing XML.** Parsing with an XML library and writing
the tree back reorders attributes and normalizes whitespace across the entire file, turning a
three-line change into an unreviewable diff and risking loss of formatting the IDE may care
about. Instead: perform targeted text replacement on the raw file content, then parse the
*result* to confirm it is still well-formed XML, and only write if that check passes. The safety
gain over a `sed` one-liner is the verification step and the anchored matching, not the parser.

After rewriting, re-scan and report any surviving references to old paths, and separately warn
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
- Every planned move as a source and destination pair, each with the status actually achieved
  (`moved`, `skipped`, `failed`).
- Every product configuration directory backed up, with its relative path inside `config/`.
- Every file modified during the rewrite phase, with a count of replacements made.
- `undone_at`: null initially, written with a timestamp when the run is rolled back.

That last field matters for the `backups` command described below — a backup that has already
been rolled back must be visibly marked, rather than offering an undo that would do something
surprising.

## 7. Command-line surface

```
idea-migrate plan                 # discover and print the proposed migration; writes nothing
idea-migrate apply                # interactive selection, then back up, move, rewrite
idea-migrate backups              # list all backups: when, what moved, size, undone or not
idea-migrate undo <backup-dir>    # roll back a specific run
```

`plan` is the default when no subcommand is given, so an accidental bare invocation cannot
modify anything. `apply` requires explicit confirmation of the selection before it writes.

`backups` reads `manifest.json` from every directory under the backup root, so it stays accurate
without the tool maintaining state anywhere else. For each backup it shows the timestamp, the
roots that run moved and where to, the size on disk, whether it has been undone, and the undo
command for that specific backup.

## 8. Configuration

A TOML file read with the standard library's `tomllib`, holding the destination root, the backup
root, product directories to exclude, and any extra path prefixes to rewrite. Every setting has a
default matching this migration, so the file is optional. This is what makes the tool reusable
for a future move rather than a one-shot script.

## 9. Constraints on the implementation

- Python 3.11 or newer (3.14 is installed). Standard library only — no third-party dependencies.
- `plan` is the default and nothing writes without `apply`.
- Every phase is a separate, independently testable module.
- Refuse to run while any JetBrains IDE is alive.
- Never delete a backup.

## 10. Testing

- Unit tests for the rewrite function against fixture XML files copied from the real
  configuration with paths anonymized. These must include the near-miss cases that must *not*
  match: a longer sibling directory name sharing a prefix, a path appearing inside unrelated
  attribute text, and both placeholder and expanded forms in the same file.
- A malformed-XML fixture to confirm the post-rewrite well-formedness check actually rejects.
- Discovery tested against a synthetic home directory built in a temporary folder, including
  the two-spellings-one-inode case and an unrelated root such as `Downloads`.
- Move and preflight tested against the same synthetic tree, including the refuse-if-IDE-running
  and destination-exists paths.
- A full round trip: apply against the synthetic tree, then undo, then assert the tree is
  byte-identical to its starting state.
- No test touches the real home directory or the real JetBrains configuration.
