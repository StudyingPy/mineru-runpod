"""Worked example: post-process a MinerU run of a numbered technical document into a
clean, cross-linked, section-addressable Markdown tree, using the `doctree` modules.

Generic and self-contained — no document, standard, or repository is hard-coded. You
point it at your own MinerU output via CLI flags. The only domain assumptions are
conventions common to technical specifications: dotted section ids (`1.2.3`, `A.4.1`),
`§` cross-references, and (optionally) embedded XSD/RELAX-NG schema with an authoritative
source to reconcile against.

    python example_pipeline.py \
        --outline sections.json --batches ./batches --out ./tree \
        [--plan batch_order.json] [--pdf source.pdf] [--xsd ./schemas] \
        [--corrections corrections.json] [--patches patches.json] [--benign benign.json]

Inputs
  --outline      JSON list `[{"id","title","page"}, …]` or `{"sections":[…]}` — the section
                 tree (id like "1.2.3"/"A.4.1"; page = 0-based source page; hierarchy is
                 inferred from id prefixes). Common aliases (clause/page_0based) tolerated.
  --batches      dir of MinerU `*_content_list.json` (one per parsed slice).
  --plan         JSON `[{"id","start"}, …]` giving each batch's first 0-based page; without
                 it batches are read in filename order, each starting at page 0.
  --pdf          enable the source-PDF cross-check (needs pymupdf).
  --xsd          dir of `.zip`/`.xsd`/`.rnc` authoritative schemas → enables schema-dump
                 replacement + the vocabulary name check.
  --corrections  JSON `{"corrections":{garble:correct}, "attr_only":[…]}` (OCR fixes).
  --patches      JSON `{section-id:[{find,replace,regex?}]}` (per-section overlay).
  --benign       JSON `[[garble, section-id], …]` reviewed-benign verify pairs.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import schema as schemalib
from model import Section
from render import RenderConfig, render_blocks, is_noise
from segment import attach_pages, merge_split_headings, segment_stream
from crosslink import SectionIndex, make_linkifier
from tree import TreeConfig, write_tree
from corrections import apply_overlay
from verify import Vocabulary, check_names, verify_against_pdf

# Conventions common to numbered technical specs (override for your document if needed).
STOP = {"the", "a", "an", "of", "and", "for", "to", "in"}
SECTION_HEAD = re.compile(r"^\s*((?:\d+\.)*\d+|[A-Z](?:\.\d+)+)\.?\s+\S")     # "1.2.3 Title" / "A.4.1 Title"
BARE_HEAD = re.compile(r"^\s*((?:\d+\.)*\d+|[A-Z](?:\.\d+)+)\.?\s*$")         # a block that is ONLY a section id
ANNEX_HEAD = re.compile(r"^Annex\s+([A-Z])\b")                                # "Annex B. (informative) …"
PART_TABLE = re.compile(r"Content Type\(?s?\)?\s*:", re.I)                    # opens each OOXML "Part Summary" entry
SECTION_REF = re.compile(r"§\s*((?:\d+\.)*\d+|[A-Z](?:\.\d+)+)")             # "§1.2.3" cross-reference
SCHEMA_XSD = re.compile(r'^\s{0,6}<(?:xsd|xs):(complexType|simpleType|element|group|attributeGroup)\b[^>]*?\bname="([^"]+)"', re.M)
SCHEMA_RNC = re.compile(r"^([A-Za-z_][\w.]*)\s*=", re.M)
RNC_KW = {"namespace", "default", "datatypes", "include", "div", "grammar"}
GROUP_DIR = {"complexType": "complex-types", "simpleType": "simple-types", "element": "elements",
             "group": "groups", "attributeGroup": "attribute-groups", "define": "definitions"}
SCHEMA_MIN_CHARS = 20000


# --- id / naming helpers (generic: dotted numbers + letter annexes) --------
def comps(sid):
    m = re.match(r"^Annex\s+([A-Z])$", sid, re.I)
    return ["annex", m.group(1).lower()] if m else [p for p in sid.replace(" ", "").split(".") if p]


def dash(sid):
    return "-".join(p.lower() for p in comps(sid))


def kebab_words(text):
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
    return [w for w in re.findall(r"[A-Za-z0-9]+", spaced.lower()) if w not in STOP] or ["x"]


def kebab(text, n=3):
    return "-".join(kebab_words(text)[:n])


def primary_phrase(desc):
    """Lead phrase before a ' - ' subtitle separator ('Foo - Bar Baz' -> 'Foo')."""
    segs = [s for s in re.split(r"\s+-\s+", desc) if s.strip()]
    return segs[0] if segs else desc


def shorten_desc(desc, ancestor_words, cap=3):
    """Short slug for a description: subtitle-split + drop leading words already named by
    an ancestor + cap. The unique dashed id prefix makes an empty result collision-proof."""
    segs = [s for s in re.split(r"\s+-\s+", desc) if s.strip()]
    pw = kebab_words(segs[0] if segs else desc)
    stripped = [w for w in pw if w not in ancestor_words] or pw
    if len(segs) > 1 and all(w in ancestor_words for w in pw):
        rest = kebab_words(" ".join(segs[1:]))
        stripped = [w for w in rest if w not in ancestor_words] or rest
    return "-".join(stripped[:cap])


def split_title(title):
    """('tag', 'Description') when the title leads with an element/type-like token, else
    (None, title). Generic: a leading lowercase / CT_-style / camelCase word is a tag."""
    m = re.match(r"^([A-Za-z_][\w.:-]*)\s*\((.+)\)\s*$", title.strip())
    taglike = m and (m.group(1)[:1].islower() or re.match(r"^(CT|ST|EG|AG)_", m.group(1))
                     or re.search(r"[a-z][A-Z]", m.group(1)))
    return (m.group(1), m.group(2)) if taglike else (None, title.strip())


# Short, parent-aware description slugs, keyed by section id (filled by assign_slugs).
SLUG: dict[str, str] = {}


def assign_slugs(roots):
    """Walk the tree once, giving each section a short slug that omits leading words an
    ancestor already names ('DrawingML - Main' under 'DrawingML' -> 'main')."""
    def walk(node, ancestor_words):
        desc = split_title(node.title)[1]
        SLUG[node.id] = shorten_desc(desc, ancestor_words)
        child_words = ancestor_words | set(kebab_words(primary_phrase(desc)))
        for c in node.children:
            walk(c, child_words)
    for r in roots:
        walk(r, set())


def folder_name(s): return "-".join(p for p in (dash(s.id), SLUG.get(s.id, "")) if p)
def barrel_name(s): return f"{dash(s.id)}-0-index.md"


def file_name(s):
    tag = split_title(s.title)[0]
    return "-".join(p for p in (dash(s.id), tag, SLUG.get(s.id, "")) if p) + ".md"


# --- outline -> Section tree -----------------------------------------------
def load_sections(path: Path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw.get("sections") or raw.get("clauses") if isinstance(raw, dict) else raw
    by_id = {}
    for r in rows:
        sid = r.get("id") or r.get("clause")
        by_id[sid] = Section(sid, r.get("title", ""), r.get("page", r.get("page_0based", 0)))
    def parent_of(sid):
        cc = comps(sid)
        for k in range(len(cc) - 1, 0, -1):
            if cc[0] == "annex":
                cand = None                                  # "Annex A" is top-level
            elif k == 1 and len(cc[0]) == 1 and cc[0].isalpha():
                cand = f"Annex {cc[0].upper()}"              # A.1 -> Annex A, L.4 -> Annex L
            else:
                cand = ".".join(cc[:k])                      # 1.2.3 -> 1.2
            if cand and cand in by_id:
                return by_id[cand]
        return None

    roots = []
    for sid, node in by_id.items():
        parent = parent_of(sid)
        (parent.children if parent else roots).append(node)
    for n in by_id.values():
        n.children.sort(key=lambda c: (c.page, comps(c.id)))
    roots.sort(key=lambda c: (c.page, c.id))
    return by_id, roots


def make_heading_id(valid):
    def heading_id(blk):
        t, text = blk.get("type"), (blk.get("text") or "").strip()
        if t == "code" and not (blk.get("code_body") or "").strip():
            text = text.splitlines()[0] if text else ""
        elif t != "text":
            return None
        # Annex top-level heading ("Annex B. (informative) …"): not number-led, so
        # SECTION_HEAD misses it. Reject the front-matter TOC echo (trailing page number).
        am = ANNEX_HEAD.match(text)
        if am:
            aid = f"Annex {am.group(1)}"
            if aid in valid and not re.search(r"\d{2,}\s*$", text) and (
                    blk.get("text_level") or (len(text) <= 90 and not text.endswith((".", ",", ";", ":")))):
                return aid
            return None
        m = SECTION_HEAD.match(text)
        if not m or m.group(1).rstrip(".") not in valid:
            return None
        sid = m.group(1).rstrip(".")
        if t == "code" or blk.get("text_level") or (len(text) <= 90 and not text.endswith((".", ",", ";", ":"))):
            return sid
        return None
    return heading_id


# --- schema-dump support (only used when --xsd is given) -------------------
def schema_code(section):
    parts = []
    for b in section.blocks:
        if b.get("type") == "code":
            body = re.sub(r"^```[^\n]*\n", "", b.get("code_body") or "")
            parts.append(re.sub(r"\n?```\s*$", "", body))
    return "\n".join(parts)


def schema_decls(code):
    decls = [(m.start(), m.group(1), m.group(2)) for m in SCHEMA_XSD.finditer(code)]
    return decls or [(m.start(), "define", m.group(1)) for m in SCHEMA_RNC.finditer(code)
                     if m.group(1) not in RNC_KW]


def make_is_schema(enabled):
    def is_schema(s):
        if not enabled:
            return False
        code = schema_code(s)
        return len(code) >= SCHEMA_MIN_CHARS and len(schema_decls(code)) >= 2
    return is_schema


def make_split_leaf(auth, corrections):
    def variants(name):
        return {name, name.replace(" ", "_"), corrections.get(name, name)}

    def split_leaf(section, folder, rel):
        code = schema_code(section)
        decls = sorted(schema_decls(code))
        if len(decls) < 2:
            return None
        is_rnc = bool(SCHEMA_RNC.search(code)) and not SCHEMA_XSD.search(code)
        idx = auth["rnc"] if is_rnc else auth["xsd"]
        home_map = idx.get(schemalib.pick_home_schema(idx, [n for _s, _k, n in decls], variants), {})
        groups = {}
        for i, (start, kind, name) in enumerate(decls):
            end = decls[i + 1][0] if i + 1 < len(decls) else len(code)
            m = schemalib.match_decl(home_map, name, is_rnc, variants)
            a_kind, a_name, frag = m if m else (kind, name, code[start:end].rstrip())
            groups.setdefault(GROUP_DIR.get(a_kind, "other"), []).append((a_name, frag))
        links = []
        for gdir, items in sorted(groups.items()):
            gp = folder / gdir
            gp.mkdir(parents=True, exist_ok=True)
            inner = []
            for nm, frag in items:
                (gp / f"{dash(section.id)}-{nm}.md").write_text(f"# {nm}\n\n```xml\n{frag}\n```\n", encoding="utf-8")
                inner.append(f"- [`{nm}`]({dash(section.id)}-{nm}.md)")
            gb = f"{dash(section.id)}-0-{gdir}.md"
            (gp / gb).write_text(f"# {section.id} {section.title} — {gdir}\n\n## Contents\n\n"
                                 + "\n".join(inner) + "\n", encoding="utf-8")
            links.append(f"- [{gdir} ({len(items)})]({gdir}/{gb})")
        return links
    return split_leaf


def _load(path, default):
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else default


def main():
    try:  # so --help / progress prints unicode (§, ->) on legacy consoles too
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outline", type=Path, required=True)
    ap.add_argument("--batches", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--plan", type=Path)
    ap.add_argument("--pdf", type=Path)
    ap.add_argument("--xsd", type=Path)
    ap.add_argument("--corrections", type=Path)
    ap.add_argument("--patches", type=Path)
    ap.add_argument("--benign", type=Path)
    args = ap.parse_args()

    cfg_corr = _load(args.corrections, {})
    corrections = cfg_corr.get("corrections", {})
    attr_only = set(cfg_corr.get("attr_only", []))
    patches = _load(args.patches, {})
    benign = {tuple(p) for p in _load(args.benign, [])}

    by_id, roots = load_sections(args.outline)
    assign_slugs(roots)  # parent-aware short slugs (must precede naming / path computation)

    # assemble the global block stream (with absolute pages) from the batches
    plan = _load(args.plan, None)
    order = ([(b["id"], b["start"]) for b in sorted(plan["batches"] if isinstance(plan, dict) else plan,
                                                    key=lambda x: x["start"])]
             if plan else [(p.stem.replace("_content_list", ""), 0) for p in sorted(args.batches.rglob("*_content_list.json"))])
    stream = []
    for bid, start in order:
        f = next(args.batches.rglob(f"{bid}*content_list.json"), None) or (args.batches / bid / f"{bid}_content_list.json")
        if Path(f).is_file():
            stream += attach_pages(json.loads(Path(f).read_text(encoding="utf-8")), start)

    cfg0 = RenderConfig(drop_internal_tocs=True, noise_phrases=(r"table of contents",))
    valid = set(by_id)

    def bare_id(text):                       # the block is ONLY a known section id
        m = BARE_HEAD.match(text)
        return m.group(1).rstrip(".") if m and m.group(1).rstrip(".") in valid else None

    def title_like(text):                    # the next block reads like a title
        return (0 < len(text) <= 90 and not text[0].isdigit()
                and not SECTION_HEAD.match(text) and not text.endswith((".", ",", ";", ":")))

    def natkey(sid):
        return tuple((0, int(p)) if p.isdigit() else (1, p) for p in comps(sid))

    order = [s.id for s in sorted((n for r in roots for n in r.walk()), key=lambda s: (s.page, natkey(s.id)))]

    def part_boundary(blk, current, nxt):    # OOXML: each Part-Summary entry opens with a "Content Type" table
        if current is None or not current.blocks or blk.get("type") != "table":
            return False
        if not PART_TABLE.search((blk.get("table_body") or "")[:120]):
            return False
        cc, nc = comps(current.id), comps(nxt.id)[:-1]
        return nc in (cc, cc[:-1])           # nxt is a child or a sibling of current

    stream = merge_split_headings(stream, bare_id, title_like)   # repair split "id \n title" headings
    segment_stream(stream, by_id, make_heading_id(valid), is_noise=lambda b: is_noise(b, cfg0),
                   order=order, boundary=part_boundary)

    auth = schemalib.load_authoritative_decls(sorted(args.xsd.glob("*.zip")) + list(args.xsd.rglob("*.xsd"))
                                              + list(args.xsd.rglob("*.rnc"))) if args.xsd else {"xsd": {}, "rnc": {}}
    is_schema = make_is_schema(bool(args.xsd))

    def is_empty(s):                          # a leaf the source gave no content -> omit its file
        return (not s.has_children and not is_schema(s)
                and not render_blocks(s.blocks, RenderConfig(corrections=corrections, attr_only=attr_only)).strip())

    index = SectionIndex(roots, folder_name, file_name, barrel_name,
                         is_folder=lambda s: s.has_children or is_schema(s), is_empty=is_empty)
    misses = []

    def child_line(c):
        _t, desc = split_title(c.title)
        if c.has_children or is_schema(c):
            suffix = f"{len(c.children)} sub-sections" if c.has_children else "schema"
            return f"- [`{c.id}` {desc}]({folder_name(c)}/{barrel_name(c)}) — {suffix}"
        if is_empty(c):                       # no file emitted -> list without a link
            return f"- `{c.id}` {desc} — (no content)"
        tag = split_title(c.title)[0]
        return f"- [`{c.id}` {desc}]({file_name(c)}) — {('`'+tag+'`') if tag else 'section'}"

    def render(section, cur_dir):
        linkify = make_linkifier(index, SECTION_REF, cur_dir)
        blocks = [b for b in section.blocks if b.get("type") != "code"] if is_schema(section) else section.blocks
        text = render_blocks(blocks, RenderConfig(corrections=corrections, attr_only=attr_only, linkify=linkify))
        return apply_overlay(text, patches.get(section.id), on_miss=lambda f: misses.append((section.id, f)))

    stats = write_tree(roots, args.out, TreeConfig(
        render=render, folder_name=folder_name, file_name=file_name, barrel_name=barrel_name,
        child_line=child_line, is_special=is_schema, split_leaf=make_split_leaf(auth, corrections),
        is_empty=is_empty))
    print(f"wrote {stats['folders']} folders, {stats['files']} files "
          f"({stats.get('omitted', 0)} empty leaves omitted); {len(misses)} stale patch find(s)")

    if args.xsd:
        vocab = Vocabulary.from_xsd(sorted(args.xsd.glob("*.zip")) + list(args.xsd.rglob("*.xsd")))
        print(f"name-vs-vocab suspects: {len(check_names(args.out, vocab))}")
        if args.pdf:
            page_of = {s.id: s.page for s in (n for r in roots for n in r.walk())}

            def section_of_file(f):
                m = re.match(r"^((?:[0-9]+|[a-z])(?:-[0-9]+)*)", f.stem)
                return ".".join(x.upper() if len(x) == 1 and x.isalpha() else x
                                for x in m.group(1).split("-")) if m else None

            confirmed = verify_against_pdf(args.out, args.pdf, section_of_file, page_of, vocab, benign=benign)
            print(f"PDF-confirmed garbles (actionable): {len(confirmed)}")


if __name__ == "__main__":
    main()
