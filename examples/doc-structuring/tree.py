"""Write a Section hierarchy to a folder/file tree of small Markdown files.

Rules:
  * a section WITH children (or one your `is_folder` marks special) -> a folder + a
    barrel file (`*-0-index.md` style) holding the section's own intro + a child index;
  * a leaf section -> one file (the "golden rule": never split a leaf into parts);
  * an agent navigates root -> barrel -> barrel -> leaf, reading one small file per level.

Everything domain-specific (file/folder naming, the body renderer, how a child is listed,
optional special-splitting of a huge leaf) is injected via `TreeConfig` callbacks, so the
writer has no idea what a "clause" or a "schema" is.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from model import Section


@dataclass
class TreeConfig:
    render: Callable[[Section, str], str]        # (section, cur_dir_relposix) -> body markdown
    folder_name: Callable[[Section], str]        # section -> folder dir name
    file_name: Callable[[Section], str]          # section -> leaf file name
    barrel_name: Callable[[Section], str]        # section -> barrel file name (inside its folder)
    child_line: Callable[[Section], str]         # child section -> one "## Contents" bullet
    is_folder: Callable[[Section], bool] | None = None        # default: has children
    title_line: Callable[[Section], str] | None = None        # default: "# {id} {title}"
    # optional: a leaf the source gave no content. Its file is NOT written (the parent
    # barrel still lists it — your `child_line` should render it without a link — and
    # SectionIndex redirects links to it to the parent barrel).
    is_empty: Callable[[Section], bool] | None = None
    # optional: mark a section for special-splitting (e.g. an authoritative schema dump).
    # Such a section becomes a folder even with no children, and may ALSO have children.
    is_special: Callable[[Section], bool] | None = None
    # optional: split a special section into a folder of sub-files; return link lines or None
    split_leaf: Callable[[Section, Path, str], "list[str] | None"] | None = None


def write_tree(roots: list[Section], out_dir: Path, cfg: TreeConfig) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    is_folder = cfg.is_folder or (lambda s: s.has_children)
    title_line = cfg.title_line or (lambda s: f"# {s.id} {s.title}\n")
    stats = {"folders": 0, "files": 0, "barrels": 0, "empty": []}

    def emit(node: Section, parent_dir: Path, rel: str) -> None:
        special = bool(cfg.split_leaf) and (
            cfg.is_special(node) if cfg.is_special else not node.has_children)
        if is_folder(node) or special:
            folder = parent_dir / cfg.folder_name(node)
            folder.mkdir(parents=True, exist_ok=True)
            frel = f"{rel}/{cfg.folder_name(node)}" if rel else cfg.folder_name(node)
            body = cfg.render(node, frel)
            sections = []
            split_links = cfg.split_leaf(node, folder, frel) if special and cfg.split_leaf else None
            if split_links:
                sections.append("## Schema\n\n" + "\n".join(split_links))
            if node.has_children:
                sections.append("## Contents\n\n" + "\n".join(cfg.child_line(c) for c in node.children))
            barrel = title_line(node) + (f"\n{body}\n" if body else "") + \
                ("\n" + "\n\n".join(sections) + "\n" if sections else "")
            (folder / cfg.barrel_name(node)).write_text(barrel, encoding="utf-8")
            stats["folders"] += 1
            stats["barrels"] += 1
            for c in node.children:
                emit(c, folder, frel)
        elif cfg.is_empty and cfg.is_empty(node):
            stats["omitted"] = stats.get("omitted", 0) + 1   # no file; parent index lists it
            stats["empty"].append(node.id)
        else:
            body = cfg.render(node, rel)
            (parent_dir / cfg.file_name(node)).write_text(
                title_line(node) + (f"\n{body}\n" if body else "\n"), encoding="utf-8")
            stats["files"] += 1
            if not body:
                stats["empty"].append(node.id)

    for r in roots:
        emit(r, out_dir, "")
    return stats
