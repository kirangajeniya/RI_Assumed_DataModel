[README.md](https://github.com/user-attachments/files/33313231/README.md)
# Data-model diff for pull requests

A GitHub-native workflow that visualises erwin data-model changes in pull
requests. On every PR that touches `model/*.csv` it posts a sticky comment
with a Mermaid `erDiagram` plus a Markdown change table, diffed between the
base branch and the PR branch.

## Repo layout

Copy these into your repository:

| This bundle | Destination in your repo |
|---|---|
| `diff.py` | repo root (or update the workflow's `python diff.py` path) |
| `.github/workflows/model-diff.yml` | `.github/workflows/model-diff.yml` |
| `model/dictionary.csv`, `model/relationships.csv` | your erwin exports (see below) |

`samples/` contains old/new example CSVs plus `expected_report.md`, the
report `diff.py` produces for them — useful as a regression check.

## Exporting from erwin

1. In erwin Data Modeler, build a report (Tools → Report Designer, or your
   Mart report template) with one row per attribute and one row per
   relationship.
2. Export each report to CSV with **exactly** these headers (order matters
   only for readability — `diff.py` matches by name):

   `dictionary.csv`:
   `Entity/Table Name, Table Name, Attribute Name, Column Name, Column Comment,`
   `Attribute Definition, Attribute/Column Is FK, Attribute/Column Is PK,`
   `Column Null_Option, Column Data Type`

   `relationships.csv`:
   `Relationship Name, Relationship Parent_To_Child_Verb_Phrase,`
   `Child Entity Name, Parent Entity Name`

3. Commit them to `model/` on your main branch. Every PR that changes them
   will now get an automatic visual diff.

Value encodings observed in real exports (handled by normalisation):
`Attribute/Column Is PK` / `Is FK` are `TRUE`/`FALSE`,
`Column Null_Option` is `Not Null`/`Null`. Comparisons are
case-insensitive after trimming, so `true` vs `TRUE` never shows as a diff.

## How the diff works

**Normalisation first:** every cell is trimmed, internal whitespace is
collapsed, keys are matched case-insensitively, rows are sorted. Cosmetic
whitespace/case edits never appear as changes.

**Keys and rules:**

| Area | Key | Compared |
|---|---|---|
| Attribute | `Table Name` + `Column Name` | every other column (datatype, null option, PK/FK, definition, comment, logical names…) |
| Entity | `Table Name` | appears/disappears entirely → ENTITY ADDED / ENTITY REMOVED |
| Relationship | `Parent Entity Name` + `Child Entity Name` + `Relationship Name` | every other column (verb phrase, …) |

Note: this erwin export carries no Cardinality / Identifying / FK-column
fields, so relationship lines use a neutral `||--||` connector (no
cardinality is asserted) and only the verb phrase is diffed. If a future
export adds such columns, they are compared automatically.

**Rename heuristic:** when a removed and an added attribute in the *same*
entity share datatype *and* a non-empty definition, the report lists it
under "Possible renames" — flagged only, never auto-merged.

## The report

1. **One-line summary** — e.g. `1 entities added, 1 removed, 3 attributes
   added, 3 removed, 2 modified, 4 relationships changed.`
2. **Mermaid `erDiagram`** — only changed entities plus their direct
   neighbours. 🟢 added / 🔴 removed / 🟠 modified markers appear in entity
   aliases and attribute comments; changed relationship lines are labelled
   (`🟢 NEW R/21`, `🔴 REMOVED R/11`, `🟠 R/20 …`). More than ~15 changed
   entities are split into multiple diagrams. Names are sanitised for
   Mermaid (no spaces/special characters).
3. **Change table** — `Entity | Attribute | Change type | Old | New`.

**Styling note:** GitHub's Mermaid renderer ignores `classDef` inside
`erDiagram`, so emoji markers are the primary (and default) mechanism and
work everywhere. `diff.py --style classdef` additionally emits `classDef`
lines for renderers that honour them.

**Size limit:** GitHub caps comment bodies at 65,536 characters. The report
is budgeted to 60,000 (`--max-chars`), truncated on line boundaries with
the summary and first diagrams kept at the head; the full report is always
uploaded as the `model-diff-report` workflow artifact and mirrored in the
job summary (visible even when commenting fails, e.g. PRs from forks with
a read-only token).

**Empty diffs** post `No model changes`. **New files** (no base-branch
version) are treated as empty on the base side, so everything counts as added.

## Running locally

```bash
pip install pandas
python diff.py --old-dir /tmp/old_model --new-dir model --out report.md
python diff.py --old-dir samples/old --new-dir samples/new --out /tmp/demo.md  # demo
python diff.py --help  # --max-chars, --style emoji|classdef
```

## Workflow notes

- Triggers on `pull_request` with `paths: model/*.csv`; `fetch-depth: 0`
  so `git show origin/<base>:model/...` can retrieve the base version.
- Posts via `marocchino/sticky-pull-request-comment@v2` using the `path:`
  input (comment body read from `report.md`, no shell-quoting issues) with
  `header: model-diff`, updating one sticky comment in place per PR. Prefer
  a fresh comment per push? Uncomment `recreate: true` in the workflow.
- Needs `pull-requests: write` (+ `contents: read`).
- The comment step has `continue-on-error: true`: PRs from forks get a
  read-only token, so the report still lands in the job summary + artifact.
