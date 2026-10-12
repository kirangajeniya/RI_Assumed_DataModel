#!/usr/bin/env python3
"""
diff.py -- visual data-model diff for erwin CSV exports.

Compares two snapshots of the erwin exports:

    dictionary.csv      one row per attribute / column
    relationships.csv   one row per relationship

and writes a Markdown report containing:

    1. a one-line summary,
    2. Mermaid erDiagram(s) covering the changed entities plus their
       direct neighbours,
    3. a Markdown change table,
    4. possible renames flagged by a heuristic.

The report is designed to be posted as a GitHub PR comment
(see .github/workflows/model-diff.yml). GitHub renders Mermaid
code blocks natively.

Actual erwin export columns (do NOT rename these without updating the
constants below):

    dictionary.csv:
        Entity/Table Name, Table Name, Attribute Name, Column Name,
        Column Comment, Attribute Definition, Attribute/Column Is FK,
        Attribute/Column Is PK, Column Null_Option, Column Data Type

    relationships.csv:
        Relationship Name, Relationship Parent_To_Child_Verb_Phrase,
        Child Entity Name, Parent Entity Name

Adaptations to the real export (vs. the generic spec):
    * Dictionary key  = Table Name + Column Name (the physical identifiers).
      Every other column is compared, so logical renames of
      "Attribute Name" surface as MODIFIED instead of being missed.
    * Entity identity = Table Name.
    * The export carries no Cardinality / Identifying / FK-column fields,
      so relationship lines use a neutral connector and only the verb
      phrase is diffed. If a future export adds such columns they are
      picked up automatically (every non-key column is compared).
    * GitHub's Mermaid renderer does not apply `classDef` inside erDiagram
      blocks, so emoji markers are the PRIMARY change signal and work
      everywhere. `--style classdef` additionally emits classDef lines
      for renderers that honour them.

Usage:
    python diff.py --old-dir /tmp/old_model --new-dir model \
        --out report.md [--max-chars 60000] [--style emoji|classdef]
"""

import argparse
import os
import re
import sys

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    sys.exit("diff.py requires pandas: pip install pandas")

# ---------------------------------------------------------------------------
# Column maps -- literal erwin export headers. Change here, not in the logic.
# ---------------------------------------------------------------------------
DICT_FILE = "dictionary.csv"
REL_FILE = "relationships.csv"

D_ENTITY_LOGICAL = "Entity/Table Name"
D_TABLE = "Table Name"
D_ATTR_LOGICAL = "Attribute Name"
D_COLUMN = "Column Name"
D_COMMENT = "Column Comment"
D_DEFINITION = "Attribute Definition"
D_IS_FK = "Attribute/Column Is FK"
D_IS_PK = "Attribute/Column Is PK"
D_NULLABLE = "Column Null_Option"
D_DATATYPE = "Column Data Type"

DICT_REQUIRED = [
    D_ENTITY_LOGICAL, D_TABLE, D_ATTR_LOGICAL, D_COLUMN, D_COMMENT,
    D_DEFINITION, D_IS_FK, D_IS_PK, D_NULLABLE, D_DATATYPE,
]
# Physical identifiers form the stable key; everything else is compared.
DICT_KEY_COLS = [D_TABLE, D_COLUMN]

R_NAME = "Relationship Name"
R_VERB = "Relationship Parent_To_Child_Verb_Phrase"
R_CHILD = "Child Entity Name"
R_PARENT = "Parent Entity Name"

REL_REQUIRED = [R_NAME, R_VERB, R_CHILD, R_PARENT]
REL_KEY_COLS = [R_PARENT, R_CHILD, R_NAME]

# Fields shown (in this order) when an attribute is MODIFIED.
FIELD_ORDER = [
    D_ATTR_LOGICAL, D_DATATYPE, D_NULLABLE, D_IS_PK, D_IS_FK,
    D_DEFINITION, D_COMMENT, D_ENTITY_LOGICAL,
]

EMOJI = {"added": "\U0001f7e2", "removed": "\U0001f534", "modified": "\U0001f7e0"}
ARROW = "\u2192"  # ->

# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def norm(value):
    """Trim whitespace and collapse internal runs of whitespace."""
    if value is None:
        return ""
    return " ".join(str(value).split())


def norm_cmp(value):
    """Comparison form: normalised + case-insensitive."""
    return norm(value).casefold()


def norm_key(value):
    """Key form: normalised + upper-cased for stable matching."""
    return norm(value).upper()


def esc_cell(value):
    """Escape a value for a Markdown table cell."""
    return norm(value).replace("|", "\\|").replace("\n", " ")


def esc_mermaid_comment(value):
    """Escape a value for a double-quoted Mermaid attribute comment."""
    return norm(value).replace('"', "'")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_csv(path, required_cols):
    """Load a CSV as strings; return None when the file does not exist.

    A missing old file (e.g. brand-new model/*.csv on the base branch)
    is treated as "no old version" -> everything counts as added.
    """
    if not os.path.exists(path):
        return None

    # Try UTF-8 first, then fall back to common Windows encodings
    for enc in ("utf-8-sig", "cp1252", "latin1"):
        try:
            df = pd.read_csv(
                path,
                dtype=str,
                keep_default_na=False,
                encoding=enc
            )
            print(f"Loaded {path} using encoding: {enc}")
            break
        except UnicodeDecodeError:
            continue
    else:
        sys.exit(f"ERROR: Unable to decode {path}")

    # Remove accents and non-ASCII characters
    import unicodedata

    def clean_text(val):
        if not isinstance(val, str):
            return val
        return (
            unicodedata.normalize("NFKD", val)
            .encode("ascii", "ignore")
            .decode("ascii")
        )

    df.columns = [norm(clean_text(c)) for c in df.columns]

    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        sys.exit(
            "ERROR: %s is missing expected column(s): %s\nFound columns: %s"
            % (path, missing, list(df.columns))
        )

    for col in df.columns:
        df[col] = df[col].map(lambda x: norm(clean_text(x)))

    return df


def keyed(df, key_cols):
    """Index rows by normalised key tuple; keep last on duplicates."""
    warnings = []
    index = {}
    for _, row in df.iterrows():
        key = tuple(norm_key(row[c]) for c in key_cols)
        if key in index:
            warnings.append(
                "duplicate key %s in %s -- kept last occurrence"
                % (key, ",".join(key_cols))
            )
        index[key] = row
    return index, warnings


# ---------------------------------------------------------------------------
# Dictionary diff
# ---------------------------------------------------------------------------

def diff_dictionary(old_df, new_df):
    """Diff attribute rows. Key = (Table Name, Column Name)."""
    old_idx, warn_old = keyed(old_df, DICT_KEY_COLS)
    new_idx, warn_new = keyed(new_df, DICT_KEY_COLS)

    old_keys, new_keys = set(old_idx), set(new_idx)
    added_keys = sorted(new_keys - old_keys)
    removed_keys = sorted(old_keys - new_keys)

    attrs_added, attrs_removed, attr_mods = [], [], []
    modified_attr_keys = set()

    for key in sorted(old_keys & new_keys):
        old_row, new_row = old_idx[key], new_idx[key]
        for col in FIELD_ORDER:
            if col in DICT_KEY_COLS:
                continue
            if col not in old_df.columns or col not in new_df.columns:
                continue
            o, n = norm(old_row[col]), norm(new_row[col])
            if norm_cmp(o) != norm_cmp(n):
                attr_mods.append({
                    "ekey": key[0], "akey": key[1],
                    "entity": norm(new_row[D_TABLE]),
                    "attribute": norm(new_row[D_COLUMN]),
                    "field": col, "old": o, "new": n,
                })
                modified_attr_keys.add(key)

    def entity_of(key, idx):
        return norm(idx[key][D_TABLE])

    old_entities = {norm_key(r[D_TABLE]) for _, r in old_idx.items()}
    new_entities = {norm_key(r[D_TABLE]) for _, r in new_idx.items()}
    ent_display = {}
    for key, row in list(old_idx.items()) + list(new_idx.items()):
        ent_display[norm_key(row[D_TABLE])] = norm(row[D_TABLE])

    return {
        "entities_added": sorted(new_entities - old_entities),
        "entities_removed": sorted(old_entities - new_entities),
        "ent_display": ent_display,
        "attrs_added": [
            {"ekey": k[0], "akey": k[1],
             "entity": entity_of(k, new_idx),
             "attribute": norm(new_idx[k][D_COLUMN]),
             "row": new_idx[k]}
            for k in added_keys
        ],
        "attrs_removed": [
            {"ekey": k[0], "akey": k[1],
             "entity": entity_of(k, old_idx),
             "attribute": norm(old_idx[k][D_COLUMN]),
             "row": old_idx[k]}
            for k in removed_keys
        ],
        "attr_mods": attr_mods,
        "modified_attr_keys": modified_attr_keys,
        "new_idx": new_idx,
        "old_idx": old_idx,
        "warnings": warn_old + warn_new,
    }


# ---------------------------------------------------------------------------
# Relationship diff
# ---------------------------------------------------------------------------

def diff_relationships(old_df, new_df):
    """Diff relationships. Key = (Parent, Child, Relationship Name)."""
    old_idx, warn_old = keyed(old_df, REL_KEY_COLS)
    new_idx, warn_new = keyed(new_df, REL_KEY_COLS)

    old_keys, new_keys = set(old_idx), set(new_idx)
    rel_added = [new_idx[k] for k in sorted(new_keys - old_keys)]
    rel_removed = [old_idx[k] for k in sorted(old_keys - new_keys)]
    rel_modified = []
    for key in sorted(old_keys & new_keys):
        o, n = old_idx[key], new_idx[key]
        changes = []
        for col in new_df.columns:
            if col in REL_KEY_COLS:
                continue
            if norm_cmp(o[col]) != norm_cmp(n[col]):
                changes.append((col, norm(o[col]), norm(n[col])))
        if changes:
            rel_modified.append({"row": n, "changes": changes})

    union = {}
    for key, row in list(old_idx.items()) + list(new_idx.items()):
        union[key] = row
    return {
        "added": rel_added, "removed": rel_removed,
        "modified": rel_modified, "union": union,
        "warnings": warn_old + warn_new,
    }


# ---------------------------------------------------------------------------
# Rename heuristic
# ---------------------------------------------------------------------------

def find_renames(d):
    """Flag a *possible* rename when a removed and an added attribute in the
    same entity share datatype AND definition. Reported only -- never merged."""
    renames = []
    removed_by_entity, added_by_entity = {}, {}
    for r in d["attrs_removed"]:
        removed_by_entity.setdefault(r["ekey"], []).append(r)
    for a in d["attrs_added"]:
        added_by_entity.setdefault(a["ekey"], []).append(a)
    for ekey in sorted(set(removed_by_entity) & set(added_by_entity)):
        for rem in removed_by_entity[ekey]:
            for add in added_by_entity[ekey]:
                dt_old = norm_cmp(rem["row"][D_DATATYPE])
                dt_new = norm_cmp(add["row"][D_DATATYPE])
                df_old = norm_cmp(rem["row"][D_DEFINITION])
                df_new = norm_cmp(add["row"][D_DEFINITION])
                # Definition must be non-empty to avoid matching on blanks.
                if dt_old and dt_old == dt_new and df_old and df_old == df_new:
                    renames.append({
                        "entity": rem["entity"],
                        "old_attr": rem["attribute"],
                        "new_attr": add["attribute"],
                        "datatype": norm(add["row"][D_DATATYPE]),
                        "definition": norm(add["row"][D_DEFINITION]),
                    })
    return renames


# ---------------------------------------------------------------------------
# Mermaid erDiagram
# ---------------------------------------------------------------------------

def sanitize_id(name, used):
    """Make a Mermaid-safe identifier (no spaces / special chars), deduped."""
    base = re.sub(r"[^A-Za-z0-9_]", "_", norm(name)).upper() or "ENTITY"
    if base[0].isdigit():
        base = "_" + base
    candidate, i = base, 2
    while candidate in used:
        candidate = "%s_%d" % (base, i)
        i += 1
    used.add(candidate)
    return candidate


def sanitize_type(datatype):
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", norm(datatype)).strip("_").upper()
    return cleaned or "STRING"


def entity_alias(ekey, display, change):
    """Display name with emoji marker, e.g. PAYEE["🟢 PAYEE"]."""
    marker = EMOJI[change] + " " if change else ""
    return '%s["%s%s"]' % (ekey, marker, esc_mermaid_comment(display))


def attribute_line(row, comment, is_new=True):
    """erDiagram attribute line: TYPE name [PK[, FK]] ["comment"].

    Note: Mermaid requires multiple key constraints comma-separated
    ("PK, FK") -- "PK FK" without the comma is a parse error.
    """
    ref = row  # row is a pandas Series from the relevant snapshot
    dtype = sanitize_type(ref[D_DATATYPE])
    name = re.sub(r"[^A-Za-z0-9_]", "_", norm(ref[D_COLUMN])) or "col"
    keys = []
    if norm_cmp(ref[D_IS_PK]) == "true":
        keys.append("PK")
    if norm_cmp(ref[D_IS_FK]) == "true":
        keys.append("FK")
    parts = ["        %s %s" % (dtype, name)]
    if keys:
        parts.append(", ".join(keys))
    if comment:
        parts.append('"%s"' % esc_mermaid_comment(comment))
    return " ".join(parts)


def build_diagrams(d, r, style):
    """One erDiagram per chunk of <=15 changed entities, each with neighbours."""
    ent_display = d["ent_display"]
    # display names for relationship-only entities (no dictionary rows)
    for row in r["union"].values():
        for col, keycol in ((R_PARENT, None), (R_CHILD, None)):
            name = norm(row[col])
            ent_display.setdefault(norm_key(name), name)

    changed_ekeys = sorted(
        {a["ekey"] for a in d["attrs_added"]}
        | {x["ekey"] for x in d["attrs_removed"]}
        | {m["ekey"] for m in d["attr_mods"]}
        | set(d["entities_added"]) | set(d["entities_removed"])
    )
    if not changed_ekeys:
        return []

    # adjacency from the union of old+new relationships
    neighbours = {e: set() for e in changed_ekeys}
    rel_edges = []  # (parent_key, child_key, name, verb, full_key)
    for key, row in r["union"].items():
        p = norm_key(row[R_PARENT])
        c = norm_key(row[R_CHILD])
        rel_edges.append((p, c, norm(row[R_NAME]), norm(row[R_VERB]), key))

    added_keys = {tuple(norm_key(rr[c]) for c in REL_KEY_COLS) for rr in r["added"]}
    removed_keys = {tuple(norm_key(rr[c]) for c in REL_KEY_COLS) for rr in r["removed"]}
    mod_keys = {tuple(norm_key(rr["row"][c]) for c in REL_KEY_COLS) for rr in r["modified"]}

    for p, c, name, verb, key in rel_edges:
        if p in neighbours:
            neighbours[p].add(c)
        if c in neighbours:
            neighbours[c].add(p)

    # per-attribute change comments for modified entities
    mod_comments = {}
    for m in d["attr_mods"]:
        mod_comments.setdefault((m["ekey"], m["akey"]), []).append(m)

    added_lookup = {(a["ekey"], a["akey"]): a for a in d["attrs_added"]}
    removed_lookup = {(x["ekey"], x["akey"]): x for x in d["attrs_removed"]}

    diagrams = []
    for chunk_no, start in enumerate(range(0, len(changed_ekeys), 15), 1):
        chunk = changed_ekeys[start:start + 15]
        chunk_set = set(chunk)
        in_diagram = set(chunk)
        for e in chunk:
            in_diagram |= neighbours[e]
        # Changed relationships must stay visible even when neither endpoint
        # is itself a changed entity (e.g. a removed rel between neighbours).
        for key in added_keys | removed_keys | mod_keys:
            row = r["union"][key]
            in_diagram.add(norm_key(row[R_PARENT]))
            in_diagram.add(norm_key(row[R_CHILD]))

        used_ids = set()
        eid = {e: sanitize_id(ent_display.get(e, e), used_ids) for e in in_diagram}
        entity_change = {}
        for e in chunk:
            if e in d["entities_added"]:
                entity_change[e] = "added"
            elif e in d["entities_removed"]:
                entity_change[e] = "removed"
            else:
                entity_change[e] = "modified"

        lines = ["erDiagram"]
        class_lines = []
        # entity blocks for changed entities
        for e in chunk:
            change = entity_change[e]
            lines.append("    %s {" % entity_alias(eid[e], ent_display.get(e, e), change))
            if style == "classdef":
                class_lines.append("    class %s %s;" % (eid[e], change))
            if change == "added":
                rows = [a["row"] for a in d["attrs_added"] if a["ekey"] == e]
                for row in rows[:30]:
                    lines.append(attribute_line(row, "%s ADDED" % EMOJI["added"]))
                if len(rows) > 30:
                    lines.append('        STRING _more "... +%d more"' % (len(rows) - 30))
            elif change == "removed":
                rows = [x["row"] for x in d["attrs_removed"] if x["ekey"] == e]
                for row in rows[:30]:
                    lines.append(attribute_line(row, "%s REMOVED" % EMOJI["removed"]))
                if len(rows) > 30:
                    lines.append('        STRING _more "... +%d more"' % (len(rows) - 30))
            else:
                # context: PK attributes unmarked, then changed attributes
                for key2, row in sorted(d["new_idx"].items()):
                    if key2[0] == e and norm_cmp(row[D_IS_PK]) == "true":
                        if key2 not in added_lookup and key2 not in mod_comments:
                            lines.append(attribute_line(row, ""))
                for (ek, ak), mods in sorted(mod_comments.items()):
                    if ek != e:
                        continue
                    row = d["new_idx"][(ek, ak)]
                    detail = "; ".join(
                        "%s: %s %s %s" % (mm["field"], mm["old"], ARROW, mm["new"])
                        for mm in mods
                    )
                    lines.append(attribute_line(
                        row, "%s MODIFIED %s" % (EMOJI["modified"], detail)))
                for key2, a in sorted(added_lookup.items()):
                    if key2[0] == e:
                        lines.append(attribute_line(
                            a["row"], "%s ADDED" % EMOJI["added"]))
                for key2, x in sorted(removed_lookup.items()):
                    if key2[0] == e:
                        lines.append(attribute_line(
                            x["row"], "%s REMOVED" % EMOJI["removed"]))
            lines.append("    }")
        # neighbour blocks: PK attributes only, for context
        for e in sorted(in_diagram - chunk_set):
            pk_rows = [row for key2, row in sorted(d["new_idx"].items())
                       if key2[0] == e and norm_cmp(row[D_IS_PK]) == "true"]
            if not pk_rows:
                pk_rows = [row for key2, row in sorted(d["new_idx"].items())
                           if key2[0] == e][:3]
            if pk_rows:
                lines.append("    %s {" % entity_alias(eid[e], ent_display.get(e, e), None))
                for row in pk_rows[:5]:
                    lines.append(attribute_line(row, ""))
                lines.append("    }")
        # relationship lines touching this chunk (neutral connector: the
        # export carries no cardinality, so none is asserted)
        for p, c, name, verb, key in sorted(rel_edges):
            if p not in in_diagram or c not in in_diagram:
                continue
            changed_rel = key in added_keys or key in removed_keys or key in mod_keys
            if p not in chunk_set and c not in chunk_set and not changed_rel:
                continue
            if key in added_keys:
                label = "%s NEW %s" % (EMOJI["added"], name)
            elif key in removed_keys:
                # erDiagram has no dashed-line directive; label it instead.
                label = "%s REMOVED %s" % (EMOJI["removed"], name)
            elif key in mod_keys:
                mod = next(m for m in r["modified"]
                           if tuple(norm_key(m["row"][cc]) for cc in REL_KEY_COLS) == key)
                detail = "; ".join("%s: %s %s %s" % (col, o, ARROW, n)
                                   for col, o, n in mod["changes"])
                label = "%s %s %s" % (EMOJI["modified"], name, detail)
            else:
                label = name
            lines.append('    %s ||--|| %s : "%s"'
                         % (eid[p], eid[c], esc_mermaid_comment(label)))
        if style == "classdef":
            lines.append("    classDef added fill:#d4edda,stroke:#28a745;")
            lines.append("    classDef removed fill:#f8d7da,stroke:#dc3545;")
            lines.append("    classDef modified fill:#fff3cd,stroke:#ffc107;")
            lines.extend(class_lines)
        diagrams.append("\n".join(lines))
    return diagrams


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def summary_line(d, r):
    return (
        "%d entities added, %d entities removed, "
        "%d attributes added, %d attributes removed, %d attributes modified, "
        "%d relationships changed."
        % (len(d["entities_added"]), len(d["entities_removed"]),
           len(d["attrs_added"]), len(d["attrs_removed"]),
           len(d["modified_attr_keys"]),
           len(r["added"]) + len(r["removed"]) + len(r["modified"]))
    )


CHANGE_TABLE_COLUMNS = ["Entity", "Attribute", "Change type", "Old", "New"]


def change_table_rows(d, r):
    """Structured change rows (used for both the Markdown and Excel outputs)."""
    rows = []
    for e in sorted(d["entities_added"]):
        rows.append([d["ent_display"][e], "—", "%s ENTITY ADDED" % EMOJI["added"], "", ""])
    for e in sorted(d["entities_removed"]):
        rows.append([d["ent_display"][e], "—", "%s ENTITY REMOVED" % EMOJI["removed"], "", ""])
    for a in sorted(d["attrs_added"], key=lambda x: (x["entity"], x["attribute"])):
        if a["ekey"] in d["entities_added"]:
            continue  # already covered by the ENTITY ADDED row
        rows.append([a["entity"], a["attribute"], "%s ADDED" % EMOJI["added"], "", ""])
    for x in sorted(d["attrs_removed"], key=lambda x: (x["entity"], x["attribute"])):
        if x["ekey"] in d["entities_removed"]:
            continue
        rows.append([x["entity"], x["attribute"], "%s REMOVED" % EMOJI["removed"], "", ""])
    for m in sorted(d["attr_mods"], key=lambda x: (x["entity"], x["attribute"], x["field"])):
        rows.append([m["entity"], m["attribute"],
                     "%s MODIFIED — %s" % (EMOJI["modified"], m["field"]),
                     m["old"], m["new"]])
    for rr in sorted(r["added"], key=lambda x: norm(x[R_NAME])):
        rows.append(["%s → %s" % (norm(rr[R_PARENT]), norm(rr[R_CHILD])),
                     norm(rr[R_NAME]), "%s RELATIONSHIP ADDED" % EMOJI["added"],
                     "", norm(rr[R_VERB])])
    for rr in sorted(r["removed"], key=lambda x: norm(x[R_NAME])):
        rows.append(["%s → %s" % (norm(rr[R_PARENT]), norm(rr[R_CHILD])),
                     norm(rr[R_NAME]), "%s RELATIONSHIP REMOVED" % EMOJI["removed"],
                     norm(rr[R_VERB]), ""])
    for mm in sorted(r["modified"], key=lambda x: norm(x["row"][R_NAME])):
        row = mm["row"]
        detail = "; ".join("%s: %s → %s" % (c, o, n) for c, o, n in mm["changes"])
        rows.append(["%s → %s" % (norm(row[R_PARENT]), norm(row[R_CHILD])),
                     norm(row[R_NAME]), "%s RELATIONSHIP MODIFIED" % EMOJI["modified"],
                     "", detail])
    return rows


def build_change_table(d, r):
    lines = ["| Entity | Attribute | Change type | Old | New |",
             "|---|---|---|---|---|"]
    for row in change_table_rows(d, r):
        lines.append("| %s |" % " | ".join(esc_cell(c) for c in row))
    return "\n".join(lines)


def write_excel(path, d, r, renames):
    """Write the report tables to an .xlsx workbook (Summary/Changes/Renames)."""
    try:
        import openpyxl  # noqa: F401 -- needed as the pandas Excel engine
    except ImportError:
        sys.exit("Excel output requires openpyxl: pip install openpyxl")

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        summary = [
            ["Metric", "Count"],
            ["Entities added", len(d["entities_added"])],
            ["Entities removed", len(d["entities_removed"])],
            ["Attributes added", len(d["attrs_added"])],
            ["Attributes removed", len(d["attrs_removed"])],
            ["Attributes modified", len(d["modified_attr_keys"])],
            ["Relationships changed",
             len(r["added"]) + len(r["removed"]) + len(r["modified"])],
        ]
        pd.DataFrame(summary[1:], columns=summary[0]).to_excel(
            writer, sheet_name="Summary", index=False)
        pd.DataFrame(change_table_rows(d, r),
                     columns=CHANGE_TABLE_COLUMNS).to_excel(
            writer, sheet_name="Changes", index=False)
        if renames:
            pd.DataFrame(
                [[rn["entity"], rn["old_attr"], rn["new_attr"],
                  rn["datatype"], rn["definition"]] for rn in renames],
                columns=["Entity", "Removed attribute", "Added attribute",
                         "Datatype", "Definition"]).to_excel(
                writer, sheet_name="Possible renames", index=False)
        for sheet in writer.sheets.values():  # sensible column widths
            for col in sheet.columns:
                width = max(len(str(cell.value or "")) for cell in col)
                sheet.column_dimensions[col[0].column_letter].width = min(width + 2, 60)


def build_report(d, r, renames, diagrams, style):
    parts = ["## Data model diff", "", summary_line(d, r), ""]
    if d["warnings"] or r["warnings"]:
        parts.append("<details><summary>Warnings</summary>")
        parts.append("")
        for w in sorted(set(d["warnings"] + r["warnings"])):
            parts.append("- %s" % esc_cell(w))
        parts.append("")
        parts.append("</details>")
        parts.append("")
    if diagrams:
        parts.append("### Diagram%s" % ("" if len(diagrams) == 1 else "s"))
        parts.append("")
        for i, dg in enumerate(diagrams, 1):
            if len(diagrams) > 1:
                parts.append("Diagram %d of %d" % (i, len(diagrams)))
                parts.append("")
            parts.append("```mermaid")
            parts.append(dg)
            parts.append("```")
            parts.append("")
    parts.append("### Changes")
    parts.append("")
    parts.append(build_change_table(d, r))
    parts.append("")
    if renames:
        parts.append("### Possible renames (heuristic -- verify before acting)")
        parts.append("")
        parts.append("| Entity | Removed attribute | Added attribute | Datatype | Definition |")
        parts.append("|---|---|---|---|---|")
        for rn in renames:
            parts.append("| %s | %s | %s | %s | %s |"
                        % (esc_cell(rn["entity"]), esc_cell(rn["old_attr"]),
                           esc_cell(rn["new_attr"]), esc_cell(rn["datatype"]),
                           esc_cell(rn["definition"])))
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def truncate_report(report, max_chars):
    """Truncate on line boundaries, keeping the summary/diagrams at the head."""
    if len(report) <= max_chars:
        return report
    note = (
        "\n\n> ⚠️ Report truncated at %d characters "
        "-- see the `model-diff-report` workflow artifact for the full report.\n"
        % max_chars
    )
    budget = max_chars - len(note) - 8  # leave room for a closing fence
    lines, total = [], 0
    for line in report.split("\n"):
        if total + len(line) + 1 > budget:
            break
        lines.append(line)
        total += len(line) + 1
    text = "\n".join(lines).rstrip()
    if text.count("```") % 2 == 1:  # never leave a mermaid block unclosed
        text += "\n```"
    return text + note


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Diff erwin model CSVs into a PR-ready Markdown report.")
    ap.add_argument("--old-dir", required=True, help="directory with base-branch CSVs")
    ap.add_argument("--new-dir", required=True, help="directory with PR-branch CSVs")
    ap.add_argument("--out", required=True, help="output Markdown file")
    ap.add_argument("--max-chars", type=int, default=60000,
                    help="comment budget; GitHub hard limit is 65536")
    ap.add_argument("--style", choices=["emoji", "classdef"], default="emoji",
                    help="emoji markers (works on GitHub) or classdef (other renderers)")
    ap.add_argument("--excel", default=None, metavar="OUT.xlsx",
                    help="also write Summary/Changes/Renames sheets to an Excel "
                         "workbook (requires openpyxl)")
    args = ap.parse_args()

    old_dict = load_csv(os.path.join(args.old_dir, DICT_FILE), DICT_REQUIRED)
    new_dict = load_csv(os.path.join(args.new_dir, DICT_FILE), DICT_REQUIRED)
    old_rel = load_csv(os.path.join(args.old_dir, REL_FILE), REL_REQUIRED)
    new_rel = load_csv(os.path.join(args.new_dir, REL_FILE), REL_REQUIRED)

    if new_dict is None:
        sys.exit("ERROR: %s not found in --new-dir" % DICT_FILE)
    if new_rel is None:
        sys.exit("ERROR: %s not found in --new-dir" % REL_FILE)
    if old_dict is None:  # brand-new model files: everything is added
        old_dict = new_dict.iloc[0:0].copy()
    if old_rel is None:
        old_rel = new_rel.iloc[0:0].copy()

    d = diff_dictionary(old_dict, new_dict)
    r = diff_relationships(old_rel, new_rel)
    renames = find_renames(d)

    no_changes = (
        not d["entities_added"] and not d["entities_removed"]
        and not d["attrs_added"] and not d["attrs_removed"]
        and not d["attr_mods"]
        and not r["added"] and not r["removed"] and not r["modified"]
    )
    if no_changes:
        report = "No model changes\n"
    else:
        diagrams = build_diagrams(d, r, args.style)
        report = build_report(d, r, renames, diagrams, args.style)
        report = truncate_report(report, args.max_chars)

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(report)

    print(summary_line(d, r) if not no_changes else "No model changes")
    print("wrote %s (%d chars)" % (args.out, len(report)))
    if args.excel:
        if no_changes:
            print("no changes -- skipping Excel output")
        else:
            write_excel(args.excel, d, r, renames)
            print("wrote %s" % args.excel)


if __name__ == "__main__":
    main()
