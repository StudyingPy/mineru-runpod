r"""Render MinerU `content_list` blocks into clean Markdown.

This is where most of the hard-won cleanup lives. Each fix addresses a real artifact
seen on large VLM-parsed technical documents:

  * code lives in `code_body` (pre-fenced) + `code_caption`, NOT `text`
  * `[Example: … end example]` markers must bracket the code, not land inside/before it
  * directly-adjacent code blocks are page-break-split halves of one example -> merge
  * fences mislabelled (```txt/asp/hcl) but holding XML -> relabel to ```xml
  * tables: HTML `table_body`; inline XML wrapped as `$<ns:…>$` math gets stripped by
    tag-removal unless protected; fully-empty (illustration) columns dropped
  * lists: drop a kept source bullet glyph ("- - foo"); keep ordered items numbered
  * a `text_level` block that is long or ends in `.!?` is figure/prose, not a heading
  * `$§1.2.3$` / `$…$`-wrapped refs and `\-` escaped-dash bullets cleaned

Domain-specifics (which tokens are garbles, how a cross-ref looks) are injected via
`RenderConfig`; the renderer itself is generic.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Callable

from corrections import apply_corrections

# MinerU block types that are never body content. `aside_text` = margin asides — most
# often the line-number gutter of a code/schema listing ("82\n83\n…") or a stacked
# watermark; left in, those numbers land between content blocks as junk lines.
DROP_TYPES = {"page_number", "header", "aside_text"}
_FENCE_RE = re.compile(r"^```([^\n]*)\n(.*)\n```\s*$", re.S)
_NS_TAG_RE = re.compile(r"</?[A-Za-z_][\w.]*:")              # namespaced XML tag <ns:elem>
_TOC_ITEM_RE = re.compile(r"^\s*(?:[A-Z]\.)?(?:\d+\.)*\d+\s+\S.*?(?:\.{2,}\s*|\s)\d+\s*$")
_REF_DOLLAR_RE = re.compile(r"\$([^$]+)\$")
_ORDERED_RE = re.compile(r"^\d+[.)]\s")
_LEAD_BULLET_RE = re.compile(r"^(?:[-–—•·▪◦*]|\\-)\s+")
# A text block that is nothing but closing XML tags — MinerU sometimes emits the tail of
# an example (`</xdr:twoCellAnchor>`) as its own block after the fence, stranding it
# outside the code (§4D). Folded back into the preceding XML fence.
_ORPHAN_CLOSE_RE = re.compile(r"^\s*(?:</[A-Za-z_][\w.\-]*(?::[\w.\-]+)?>\s*)+$")


@dataclass
class RenderConfig:
    """Knobs for rendering. All optional — defaults give faithful Markdown."""
    corrections: dict[str, str] = field(default_factory=dict)   # garble -> correct token
    attr_only: set[str] = field(default_factory=set)            # correct only in attr/table ctx
    linkify: Callable[[str], str] | None = None                 # text -> text (cross-links)
    drop_internal_tocs: bool = True
    heading_max_len: int = 80
    code_fence_lang: str = "xml"
    # full-line (case-insensitive) regexes for boilerplate to drop, e.g. running
    # "Table of Contents" headers or a spec's "End of informative text" marker. Empty by
    # default — supply your document's conventions; nothing standard-specific is baked in.
    noise_phrases: tuple[str, ...] = ()


def clean_text(s: str) -> str:
    """Unwrap `$…$` math that is really a section ref or inline XML; tidy OCR artifacts."""
    def _unwrap(m: "re.Match") -> str:
        inner = m.group(1)
        if "<" in inner and ">" in inner:                       # inline XML example
            return "`" + re.sub(r"\s+", " ", inner.replace("\\", "")).strip() + "`"
        if "§" in inner or re.fullmatch(r"\s*(?:\d+\.)*\d+\s*", inner):
            return inner.strip()
        return m.group(0)
    return _REF_DOLLAR_RE.sub(_unwrap, s).replace("�", "§")


# --- tables ----------------------------------------------------------------
def _clean_cell(c: str) -> str:
    """Cell text: recover `$`-wrapped inline XML as code (sentinel-protected from the
    tag strip), drop real HTML tags, unescape."""
    def unwrap(m):
        inner = m.group(1)
        if "<" in inner:
            x = re.sub(r"\s+", " ", inner.replace("\\", "")).strip()
            return "`" + x.replace("<", "\x01").replace(">", "\x02") + "`"
        if "§" in inner or re.fullmatch(r"\s*(?:\d+\.)*\d+\s*", inner):
            return inner.strip()
        return m.group(0)
    c = re.sub(r"\$([^$]*)\$", unwrap, c)
    c = re.sub(r"<[^>]+>", "", c)
    c = html.unescape(c).replace("\x01", "<").replace("\x02", ">")
    return re.sub(r"\s+", " ", c).strip()


def html_table_to_md(table_html: str) -> str:
    rows = re.findall(r"<tr>(.*?)</tr>", table_html, re.S | re.I)
    grid = []
    for r in rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S | re.I)
        grid.append([_clean_cell(c) for c in cells])
    grid = [r for r in grid if r]
    if not grid:
        return table_html.strip()
    w = max(len(r) for r in grid)
    grid = [r + [""] * (w - len(r)) for r in grid]
    keep = [j for j in range(w) if any(r[j].strip() for r in grid)]   # drop empty columns
    if keep and len(keep) < w:
        grid = [[r[j] for j in keep] for r in grid]
        w = len(keep)
    out = ["| " + " | ".join(grid[0]) + " |", "| " + " | ".join(["---"] * w) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in grid[1:]]
    return "\n".join(out)


# --- code fences -----------------------------------------------------------
def _relabel_xml_fence(body: str, lang: str) -> str:
    m = _FENCE_RE.match(body)
    if not m:
        return body
    cur, inner = m.group(1).strip().lower(), m.group(2)
    if cur != lang and _NS_TAG_RE.search(inner):
        return f"```{lang}\n{inner}\n```"
    return body


def _strip_fence(body: str) -> str:
    m = _FENCE_RE.match(body or "")
    return m.group(2) if m else (body or "")


# --- noise -----------------------------------------------------------------
def is_internal_toc(blk: dict) -> bool:
    if blk.get("type") != "list":
        return False
    items = [i.strip() for i in (blk.get("list_items") or []) if i.strip()]
    return bool(items) and sum(1 for it in items if _TOC_ITEM_RE.search(it)) >= max(2, len(items) // 2)


def is_noise(blk: dict, cfg: "RenderConfig") -> bool:
    if blk.get("type") in DROP_TYPES:
        return True
    if cfg.drop_internal_tocs and is_internal_toc(blk):
        return True
    txt = (blk.get("text") or "").strip()
    return any(re.fullmatch(p, txt, re.I) for p in cfg.noise_phrases)


def _render_one(blk: dict, cfg: RenderConfig) -> str:
    t = blk.get("type")
    if t == "text":
        text = clean_text((blk.get("text") or "").strip())
        lvl = blk.get("text_level")
        if lvl and len(text) <= cfg.heading_max_len and not text.rstrip().endswith((".", "!", "?")):
            return "#" * min(int(lvl) + 1, 6) + " " + text
        return _LEAD_BULLET_RE.sub("- ", text) if text.startswith("\\-") else text
    if t == "list":
        lines = []
        for it in (blk.get("list_items") or []):
            s = _LEAD_BULLET_RE.sub("", clean_text(it).strip())
            lines.append(s if _ORDERED_RE.match(s) else f"- {s}")
        return "\n".join(lines)
    if t == "code":
        caps = blk.get("code_caption") or []
        body = (blk.get("code_body") or "").rstrip() or (
            f"```{cfg.code_fence_lang}\n{(blk.get('text') or '').strip()}\n```" if blk.get("text") else "")
        trailing = ""
        fm = _FENCE_RE.match(body)
        if fm:
            inner = fm.group(2)
            em = re.search(r"\n?\s*(end (?:example|note)\]?)\s*$", inner, re.I)
            if em:
                trailing, inner = em.group(1), inner[: em.start()].rstrip()
                body = f"```{fm.group(1).strip() or cfg.code_fence_lang}\n{inner}\n```"
        body = _relabel_xml_fence(body, cfg.code_fence_lang)
        pre, post = [], []
        for c in caps:
            (post if re.match(r"^\s*end (example|note)\]?\s*$", c.strip(), re.I) else pre).append(c)
        if trailing:
            post.append(trailing)
        return "\n\n".join(p for p in (" ".join(pre).strip(), body, " ".join(post).strip()) if p)
    if t == "equation":
        return (blk.get("text") or "").strip()
    if t == "table":
        parts = [f"**{clean_text(c.strip())}**" for c in (blk.get("table_caption") or [])]
        if blk.get("table_body"):
            parts.append(html_table_to_md(blk["table_body"]))
        parts += [f"> {clean_text(c.strip())}" for c in (blk.get("table_footnote") or [])]
        return "\n\n".join(parts)
    if t == "image":
        desc = (blk.get("content") or "").strip()
        caps = " ".join(blk.get("image_caption") or []).strip()
        return f"> *Figure{(' — ' + caps) if caps else ''}:* {desc}" if desc else \
               f"*(figure omitted{(': ' + caps) if caps else ''})*"
    return (blk.get("text") or "").strip()


def render_blocks(blocks: list[dict], cfg: RenderConfig | None = None) -> str:
    """Render MinerU blocks to Markdown, applying every cleanup + (optionally) OCR
    corrections and cross-link normalization."""
    cfg = cfg or RenderConfig()
    # merge directly-adjacent code blocks (page-break split halves of one example)
    merged: list[dict] = []
    for b in blocks:
        if b.get("type") == "code" and merged and merged[-1].get("type") == "code":
            prev = merged[-1]
            inner = _strip_fence(prev.get("code_body") or "") + "\n" + _strip_fence(b.get("code_body") or "")
            prev["code_body"] = f"```{cfg.code_fence_lang}\n{inner.strip(chr(10))}\n```"
        elif (b.get("type") == "text" and merged and merged[-1].get("type") == "code"
              and _ORPHAN_CLOSE_RE.match(b.get("text") or "")
              and _NS_TAG_RE.search(_strip_fence(merged[-1].get("code_body") or ""))):
            prev = merged[-1]                                   # fold orphan close-tag(s) back in
            inner = _strip_fence(prev.get("code_body") or "").rstrip()
            prev["code_body"] = f"```{cfg.code_fence_lang}\n{inner}\n{(b.get('text') or '').strip()}\n```"
        else:
            merged.append(dict(b) if b.get("type") == "code" else b)
    text = "\n\n".join(c for c in (_render_one(b, cfg) for b in merged) if c).strip()
    if cfg.corrections:
        text = apply_corrections(text, cfg.corrections, cfg.attr_only)
    if cfg.linkify:
        text = cfg.linkify(text)
    return text
