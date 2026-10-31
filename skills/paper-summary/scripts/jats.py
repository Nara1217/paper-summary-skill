"""JATS XML -> plain Markdown for the paper-summary skill.

PubMed Central and bioRxiv/medRxiv publish full text as JATS XML. For
biomedical papers this is the equivalent of the arXiv LaTeX source: section
structure, rendered figure/table labels ("Figure 2", "Table 1"), table cells as
text, and a structured reference list all survive, none of which a PDF text dump
keeps reliably.

Stdlib only, so ingest.py can import it without new dependencies.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}

# Inline elements whose text should be wrapped to keep meaning in plain text.
_SUP = {"sup"}
_SUB = {"sub"}
# Elements that are never body text.
_SKIP_INLINE = {"fig", "table-wrap", "disp-formula", "graphic", "media",
                "supplementary-material", "alternatives", "object-id"}


@dataclass
class Figure:
    id: str
    label: str
    caption: str
    hrefs: list[str] = field(default_factory=list)


@dataclass
class JatsDoc:
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: str | None = None
    journal: str | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    abstract: str | None = None
    has_body: bool = False
    markdown: str = ""
    references: str = ""
    figures: list[Figure] = field(default_factory=list)


def _local(tag: object) -> str:
    """Tag name without namespace ('{ns}math' -> 'math')."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def sanitize(xml_text: str) -> str:
    """Make publisher JATS parseable by ElementTree.

    Drops the DOCTYPE (ElementTree never fetches the DTD, so named entities it
    defines, e.g. &nbsp; or &ndash;, would otherwise be fatal) and resolves those
    named entities through the HTML table.
    """
    xml_text = re.sub(r"<!DOCTYPE[^>\[]*(\[[^\]]*\])?\s*>", "", xml_text, count=1)

    def fix(m: re.Match) -> str:
        name = m.group(1)
        if name in _XML_ENTITIES:
            return m.group(0)
        ch = html.unescape(f"&{name};")
        if ch == f"&{name};":
            return " "  # unknown entity: drop rather than fail the whole parse
        return html.escape(ch, quote=False)

    return re.sub(r"&([A-Za-z][A-Za-z0-9]*);", fix, xml_text)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def inline(el: ET.Element | None) -> str:
    """Flatten mixed content to text, keeping super/subscripts legible."""
    if el is None:
        return ""
    parts: list[str] = [el.text or ""]
    for child in el:
        name = _local(child.tag)
        if name in _SKIP_INLINE:
            pass
        elif name in _SUP:
            parts.append("^" + inline(child))
        elif name in _SUB:
            parts.append("_" + inline(child))
        elif name == "inline-formula":
            parts.append(formula(child))
        elif name == "math":
            parts.append(_norm("".join(child.itertext())))
        else:
            parts.append(inline(child))
        parts.append(child.tail or "")
    return "".join(parts)


def formula(el: ET.Element) -> str:
    """Prefer the TeX alternative when a formula carries one."""
    for node in el.iter():
        if _local(node.tag) == "tex-math":
            tex = (node.text or "").strip()
            tex = re.sub(r"\\documentclass.*?\\begin\{document\}", "", tex, flags=re.S)
            tex = tex.replace("\\end{document}", "").strip()
            return f"${tex.strip('$').strip()}$"
    return _norm("".join(el.itertext()))


def _caption(el: ET.Element) -> str:
    cap = next((c for c in el if _local(c.tag) == "caption"), None)
    if cap is None:
        return ""
    chunks = []
    for c in cap:
        if _local(c.tag) in {"title", "p"}:
            chunks.append(_norm(inline(c)))
    return " ".join(x for x in chunks if x) or _norm(inline(cap))


def _label(el: ET.Element) -> str:
    lab = next((c for c in el if _local(c.tag) == "label"), None)
    return _norm(inline(lab)) if lab is not None else ""


def _table_rows(el: ET.Element) -> list[str]:
    rows = []
    for tr in el.iter():
        if _local(tr.tag) != "tr":
            continue
        cells = [_norm(inline(td)) for td in tr if _local(td.tag) in {"td", "th"}]
        if any(cells):
            rows.append("| " + " | ".join(cells) + " |")
    return rows


class _Renderer:
    def __init__(self) -> None:
        self.out: list[str] = []
        self.figures: list[Figure] = []

    def block(self, el: ET.Element, depth: int) -> None:
        name = _local(el.tag)
        if name in {"sec", "ack", "app"}:
            label, title = _label(el), ""
            t = next((c for c in el if _local(c.tag) == "title"), None)
            if t is not None:
                title = _norm(inline(t))
            heading = " ".join(x for x in (label, title) if x)
            if heading:
                self.out.append(f"\n{'#' * min(depth, 6)} {heading}\n")
            for c in el:
                if _local(c.tag) not in {"title", "label"}:
                    self.block(c, depth + 1)
        elif name == "p":
            self.out.append(_norm(inline(el)) + "\n")
            # figures/tables are often nested inside <p>
            for c in el.iter():
                if c is not el and _local(c.tag) in {"fig", "table-wrap", "disp-formula"}:
                    self.block(c, depth)
        elif name == "fig":
            self.fig(el)
        elif name == "fig-group":
            for c in el:
                self.block(c, depth)
        elif name == "table-wrap":
            label, cap = _label(el), _caption(el)
            self.out.append(f"\n[{label or 'Table'}] {cap}\n")
            self.out.extend(r + "\n" for r in _table_rows(el))
            for foot in el.iter():
                if _local(foot.tag) == "table-wrap-foot":
                    self.out.append(_norm(inline(foot)) + "\n")
        elif name == "disp-formula":
            label = _label(el)
            self.out.append(f"\n$${formula(el).strip('$')}$$ {label}\n")
        elif name in {"list"}:
            for item in el:
                if _local(item.tag) == "list-item":
                    self.out.append("- " + _norm(inline(item)) + "\n")
        elif name == "supplementary-material":
            label, cap = _label(el), _caption(el)
            self.out.append(f"\n[Supplementary: {label}] {cap}\n")
        elif name in {"title", "label"}:
            pass
        else:
            for c in el:
                self.block(c, depth)

    def fig(self, el: ET.Element) -> None:
        fid = el.get("id") or f"fig{len(self.figures) + 1}"
        label, cap = _label(el), _caption(el)
        hrefs = [g.get(XLINK_HREF) for g in el.iter()
                 if _local(g.tag) == "graphic" and g.get(XLINK_HREF)]
        self.figures.append(Figure(id=fid, label=label, caption=cap, hrefs=hrefs))
        self.out.append(f"\n[{label or 'Figure'} | id={fid}] {cap}\n")


def _ref_text(ref: ET.Element) -> str:
    label = _label(ref)
    cit = next((c for c in ref.iter()
                if _local(c.tag) in {"element-citation", "mixed-citation", "nlm-citation"}), None)
    if cit is None:
        return f"[{ref.get('id', '')}] {label} {_norm(inline(ref))}".strip()
    names = []
    for n in cit.iter():
        if _local(n.tag) == "name":
            sur = next((c.text for c in n if _local(c.tag) == "surname"), "") or ""
            giv = next((c.text for c in n if _local(c.tag) == "given-names"), "") or ""
            names.append(f"{sur} {giv}".strip())
    if names and _local(cit.tag) == "element-citation":
        bits = {_local(c.tag): _norm(inline(c)) for c in cit}
        who = ", ".join(names[:6]) + (" et al." if len(names) > 6 else "")
        body = ". ".join(x for x in (who, bits.get("article-title"), bits.get("source"),
                                       bits.get("year")) if x)
    else:
        body = _norm(inline(cit))
    return f"[{ref.get('id', '')}] {label} {body}".replace("  ", " ").strip()


def parse(xml_text: str) -> JatsDoc:
    root = ET.fromstring(sanitize(xml_text))
    # Europe PMC sometimes wraps the article; find it either way.
    article = root if _local(root.tag) == "article" else next(
        (e for e in root.iter() if _local(e.tag) == "article"), root)

    doc = JatsDoc()
    meta = next((e for e in article.iter() if _local(e.tag) == "article-meta"), None)
    if meta is not None:
        t = next((e for e in meta.iter() if _local(e.tag) == "article-title"), None)
        doc.title = _norm(inline(t)) or None
        for c in meta.iter():
            if _local(c.tag) == "contrib" and c.get("contrib-type", "author") == "author":
                sur = next((x.text for x in c.iter() if _local(x.tag) == "surname"), None)
                giv = next((x.text for x in c.iter() if _local(x.tag) == "given-names"), None)
                coll = next((x for x in c.iter() if _local(x.tag) == "collab"), None)
                if sur:
                    doc.authors.append(" ".join(x for x in (giv, sur) if x))
                elif coll is not None:
                    doc.authors.append(_norm(inline(coll)))
            if _local(c.tag) == "article-id":
                kind = c.get("pub-id-type")
                val = (c.text or "").strip()
                if kind == "doi":
                    doc.doi = val
                elif kind == "pmid":
                    doc.pmid = val
                elif kind in {"pmc", "pmcid"}:
                    doc.pmcid = val if val.upper().startswith("PMC") else f"PMC{val}"
        for d in meta.iter():
            if _local(d.tag) == "pub-date":
                y = next((x.text for x in d if _local(x.tag) == "year"), None)
                if y:
                    doc.year = y.strip()
                    break
        abstracts = [a for a in meta.iter() if _local(a.tag) == "abstract"
                     and a.get("abstract-type") not in {"graphical", "teaser"}]
        if abstracts:
            r = _Renderer()
            r.block(abstracts[0], 3)
            text = re.sub(r"(?m)^\s*#+\s*(.+?)\s*$", r"\1:", "".join(r.out))
            doc.abstract = _norm(text) or None
    jt = next((e for e in article.iter() if _local(e.tag) == "journal-title"), None)
    doc.journal = _norm(inline(jt)) or None if jt is not None else None

    r = _Renderer()
    if doc.title:
        r.out.append(f"# {doc.title}\n")
    if doc.abstract:
        r.out.append("\n## Abstract\n")
        r.out.append(doc.abstract + "\n")
    body = next((e for e in article if _local(e.tag) == "body"), None)
    if body is not None and len(body):
        doc.has_body = True
        r.block(body, 2)
    # floats-group holds figures/tables that the body only cross-references
    for fg in article.iter():
        if _local(fg.tag) == "floats-group":
            r.out.append("\n## Figures and tables\n")
            r.block(fg, 3)
    back = next((e for e in article if _local(e.tag) == "back"), None)
    refs: list[str] = []
    if back is not None:
        for c in back:
            name = _local(c.tag)
            if name == "ref-list":
                refs.extend(_ref_text(x) for x in c.iter() if _local(x.tag) == "ref")
            elif name in {"ack", "sec", "app-group", "notes", "fn-group"}:
                r.block(c, 2)
    doc.markdown = re.sub(r"\n{3,}", "\n\n", "".join(r.out)).strip() + "\n"
    doc.references = "\n".join(refs) + ("\n" if refs else "")
    doc.figures = r.figures
    return doc
