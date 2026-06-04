"""Normalize cross-references and turn resolvable ones into RELATIVE Markdown links.

Relative on purpose: the links are computed section-to-section *within the tree*, so they
are identical wherever the tree is mounted (your build dir, or some product repo) — zero
rewrite on move, and no host path is baked in.

Normalization matters too: every reference is rendered in one canonical form, so a single
regex fetches them all (linked or not — the link's display text still starts with the
reference token).
"""

from __future__ import annotations

import posixpath
import re
from typing import Callable

from model import Section


class SectionIndex:
    """Maps section id -> the section's output file path (relative to the tree root).

    Mirrors how the tree writer lays files out, so links point at the exact file/barrel.
    """

    def __init__(self, roots: list[Section], folder_name, file_name, barrel_name,
                 is_folder: Callable[[Section], bool] | None = None,
                 is_empty: Callable[[Section], bool] | None = None):
        self.paths: dict[str, str] = {}
        is_folder = is_folder or (lambda s: s.has_children)
        is_empty = is_empty or (lambda s: False)

        def walk(node: Section, parent: str, parent_barrel: str) -> None:
            if is_folder(node):
                folder = f"{parent}/{folder_name(node)}" if parent else folder_name(node)
                barrel = f"{folder}/{barrel_name(node)}"
                self.paths[node.id] = barrel
                for c in node.children:
                    walk(c, folder, barrel)
            elif is_empty(node) and parent_barrel:
                self.paths[node.id] = parent_barrel       # no file emitted -> link to parent index
            else:
                self.paths[node.id] = f"{parent}/{file_name(node)}" if parent else file_name(node)

        for r in roots:
            walk(r, "", "")

    def relpath(self, section_id: str, from_dir: str) -> "str | None":
        target = self.paths.get(section_id)
        return posixpath.relpath(target, from_dir or ".") if target else None


def make_linkifier(index: SectionIndex, ref_re: re.Pattern, cur_dir: str,
                   display: Callable[[str], str] | None = None) -> Callable[[str], str]:
    """Return a `text -> text` function that normalizes + links references in prose.

    `ref_re` must capture the bare reference token in group 1 (e.g. r"§\\s*([\\d.]+)").
    `display(token) -> shown text` (default "§{token}"). Code fences are left untouched.
    """
    display = display or (lambda tok: f"§{tok}")

    def repl(m: "re.Match") -> str:
        tok = m.group(1)
        shown = display(tok)
        rel = index.relpath(tok, cur_dir)
        return f"[{shown}]({rel})" if rel else shown

    def linkify(text: str) -> str:
        parts = re.split(r"(```.*?```)", text, flags=re.S)   # odd indices = code fences
        for i in range(0, len(parts), 2):
            parts[i] = ref_re.sub(repl, parts[i])
        return "".join(parts)

    return linkify
