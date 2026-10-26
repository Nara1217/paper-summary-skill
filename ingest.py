#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "httpx",
#     "pymupdf",
#     "trafilatura",
#     "feedparser",
# ]
# ///
"""Ingestion engine for the paper-summary skill.

Resolves a paper from one of three input kinds and produces a self-contained
work folder plus a manifest.json that the skill reads to write the summary.

  1. arXiv ID / URL  -> download the e-print SOURCE (.tex + figures). Highest
     fidelity: real equations and original figures.
  2. Local PDF       -> if an arXiv ID is detectable in the metadata / first
     page, fall through to (1); otherwise extract text + images with PyMuPDF.
  3. Web URL         -> extract the main article text with trafilatura.
  4. Biomedical       -> PubMed id, PMC id, DOI, or bioRxiv/medRxiv DOI/URL:
     fetch the open JATS full-text XML (PMC OA package / Europe PMC /
     bioRxiv API) with its figures; falls back to the preprint PDF, then to
     the abstract. A local PDF whose DOI resolves to verified open JATS full
     text is routed here too.

Usage:
    uv run ingest.py <input> [--base-dir DIR] [--domain DOMAIN]

<input> is an arXiv id (2301.12345 / hep-ph/9901001), an arXiv/web URL, a
PubMed id (PMID:31043725 or a pubmed.ncbi.nlm.nih.gov URL), a PMC id
(PMC6483969), a DOI (10.xxxx/...), a bioRxiv/medRxiv DOI or URL, or a path to
a local .pdf file. The resolved work folder is created under --base-dir
(default: current working directory). On success the script prints the absolute
path of manifest.json on the last line of stdout.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import httpx

UA = ("paper-summary-skill/1.0 "
      "(+https://github.com/jinheungkim1216/paper-summary-skill; "
      "mailto:jinheung.kim1216@gmail.com)")
ARXIV_API = "http://export.arxiv.org/api/query"
ARXIV_EPRINT = "https://arxiv.org/e-print/{id}"

# arXiv id patterns: new style (2301.12345 / 2301.12345v3) and old style
# (hep-ph/9901001, math.AG/0309001 ...).
ARXIV_NEW = re.compile(r"\b(\d{4}\.\d{4,5})(v\d+)?\b")
ARXIV_OLD = re.compile(r"\b([a-z][a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?\b")

RASTER_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}

# Biomedical sources. Module-level so tests can point them at a local server.
EPMC_REST = "https://www.ebi.ac.uk/europepmc/webservices/rest"
NCBI_OA = "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi"
EPMC_FIG = "https://europepmc.org/articles/{pmcid}/bin/{href}"
BIORXIV_API = "https://api.biorxiv.org/details/{server}/{doi}"
BIORXIV_PDF = "https://www.{server}.org/content/{doi}v{version}.full.pdf"

PMCID_RE = re.compile(r"\bPMC(\d{4,9})\b", re.IGNORECASE)
PMID_RE = re.compile(
    r"pubmed\.ncbi\.nlm\.nih\.gov/(\d{1,9})"
    r"|europepmc\.org/(?:abstract|article)/MED/(\d{1,9})"
    r"|\bPMID:?\s*(\d{1,9})\b",
    re.IGNORECASE,
)
# bioRxiv/medRxiv DOIs: 10.1101/2020.03.22.002386 (2019-12 onward) or 10.1101/123456.
BIO_DOI_RE = re.compile(r"10\.1101/(\d{4}\.\d{2}\.\d{2}\.\d{6,8}|\d{6})(?:v(\d+))?")
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>]+)")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def slugify(text: str, fallback: str = "paper") -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^\w\s\-]", "", text)
    text = re.sub(r"[\s_]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    return (text[:80] or fallback).strip("-") or fallback


def domain_for_category(cat: str | None) -> str:
    """Map an arXiv primary category to a summary domain guide."""
    if not cat:
        return "general"
    c = cat.lower()
    if c.startswith("hep-"):
        return "hep"
    ai_cats = {
        "cs.lg", "cs.ai", "cs.cl", "cs.cv", "cs.ne", "cs.ir", "cs.ro", "stat.ml",
    }
    if c in ai_cats:
        return "ai"
    phys_prefixes = (
        "astro-ph", "cond-mat", "gr-qc", "quant-ph", "nucl-", "physics.",
        "math-ph", "nlin",
    )
    if any(c.startswith(p) for p in phys_prefixes):
        return "physics"
    if c.startswith("q-bio"):
        return "bio"
    return "general"


def extract_doi(text: str) -> str | None:
    m = DOI_RE.search(text or "")
    if not m:
        return None
    return m.group(1).rstrip(".,;:)]}")


def detect_input(arg: str) -> tuple[str, str]:
    """Return (kind, value) where kind in
    {'arxiv', 'pdf', 'url', 'biorxiv', 'pmc', 'pubmed', 'doi'}.

    For 'biorxiv' the value is '<server>|<doi>|<version>' where server is
    'biorxiv', 'medrxiv' or '' (unknown: try both) and version may be ''.
    """
    p = Path(arg).expanduser()
    if p.is_file() and p.suffix.lower() == ".pdf":
        return "pdf", str(p.resolve())
    s = arg.strip()
    low = s.lower()
    m = BIO_DOI_RE.search(s)
    if m and ("biorxiv.org" in low or "medrxiv.org" in low or "10.1101/" in s):
        server = ("medrxiv" if "medrxiv.org" in low
                  else "biorxiv" if "biorxiv.org" in low else "")
        return "biorxiv", f"{server}|10.1101/{m.group(1)}|{m.group(2) or ''}"
    m = PMCID_RE.search(s)
    if m and ("ncbi.nlm.nih.gov" in low or "europepmc.org" in low
              or re.fullmatch(r"PMC\d{4,9}", s, re.IGNORECASE)):
        return "pmc", f"PMC{m.group(1)}"
    m = PMID_RE.search(s)
    if m:
        return "pubmed", next(g for g in m.groups() if g)
    if re.fullmatch(r"\d{1,8}", s):
        return "pubmed", s  # bare number: arXiv ids always carry a dot
    doi = extract_doi(s)
    if doi and (s.startswith("10.") or "doi.org/" in low):
        if doi.lower().startswith("10.48550/arxiv."):
            return "arxiv", doi.split(".", 2)[-1]
        return "doi", doi
    if "arxiv.org" in arg:
        aid = extract_arxiv_id(arg)
        if aid:
            return "arxiv", aid
    if arg.startswith(("http://", "https://")):
        return "url", arg
    if extract_arxiv_id(arg):
        return "arxiv", extract_arxiv_id(arg)  # bare id
    # last resort: treat as a path that may not exist yet
    return "url", arg


def extract_arxiv_id(text: str) -> str | None:
    m = ARXIV_NEW.search(text)
    if m:
        return m.group(1) + (m.group(2) or "")
    m = ARXIV_OLD.search(text)
    if m:
        return m.group(1) + (m.group(2) or "")
    return None


# --------------------------------------------------------------------------- #
# arXiv metadata + source
# --------------------------------------------------------------------------- #
def fetch_arxiv_metadata(arxiv_id: str, warnings: list[str]) -> dict:
    """Query the arXiv API for title/authors/year/primary_category/abstract."""
    import feedparser

    bare = re.sub(r"v\d+$", "", arxiv_id)
    meta: dict = {
        "title": None, "authors": [], "year": None,
        "primary_category": None, "abstract": None,
    }
    try:
        with httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True) as c:
            r = c.get(ARXIV_API, params={"id_list": bare, "max_results": 1})
            r.raise_for_status()
        feed = feedparser.parse(r.text)
        if not feed.entries:
            warnings.append(f"arXiv API returned no entry for {bare}")
            return meta
        e = feed.entries[0]
        meta["title"] = re.sub(r"\s+", " ", e.get("title", "")).strip() or None
        meta["authors"] = [a.get("name") for a in e.get("authors", []) if a.get("name")]
        published = e.get("published", "")
        meta["year"] = published[:4] if published[:4].isdigit() else None
        tag = e.get("arxiv_primary_category") or (e.get("tags") or [{}])[0]
        meta["primary_category"] = tag.get("term") if tag else None
        meta["abstract"] = re.sub(r"\s+", " ", e.get("summary", "")).strip() or None
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"arXiv metadata fetch failed: {exc}")
    return meta


def download_arxiv_source(arxiv_id: str, warnings: list[str]) -> tuple[str, bytes] | None:
    """Download the e-print blob. Returns (kind, bytes) where kind is one of
    'tar', 'gz-tex', 'pdf', or None on failure."""
    url = ARXIV_EPRINT.format(id=arxiv_id)
    try:
        with httpx.Client(headers={"User-Agent": UA}, timeout=120, follow_redirects=True) as c:
            r = c.get(url)
            r.raise_for_status()
            data = r.content
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"e-print download failed: {exc}")
        return None

    if data[:5] == b"%PDF-":
        return "pdf", data
    # tarfile transparently handles gzip/bzip/xz tarballs.
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*"):
            return "tar", data
    except tarfile.TarError:
        pass
    # single gzipped file (usually one .tex)
    try:
        return "gz-tex", gzip.decompress(data)
    except OSError:
        pass
    warnings.append("e-print blob was neither tar, gzip, nor PDF")
    return None


def safe_extract_tar(data: bytes, dest: Path) -> None:
    """Extract a tarball, refusing path-traversal members."""
    dest = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        for m in tar.getmembers():
            target = (dest / m.name).resolve()
            if not str(target).startswith(str(dest)):
                continue  # skip traversal attempt
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(m) as src, open(target, "wb") as out:
                    if src:
                        shutil.copyfileobj(src, out)


def find_main_tex(source_dir: Path) -> Path | None:
    """Pick the main .tex: must contain \\documentclass + \\begin{document};
    prefer one that also has \\title; tie-break by size."""
    tex_files = list(source_dir.rglob("*.tex"))
    if not tex_files:
        return None
    scored: list[tuple[int, int, Path]] = []
    for t in tex_files:
        try:
            txt = t.read_text(errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        score = 0
        if "\\documentclass" in txt:
            score += 4
        if "\\begin{document}" in txt:
            score += 4
        if "\\title" in txt or "\\maketitle" in txt:
            score += 2
        scored.append((score, len(txt), t))
    if not scored:
        return None
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return scored[0][2]


# --------------------------------------------------------------------------- #
# figures
# --------------------------------------------------------------------------- #
def unique_figure_target(fig_dir: Path, source_dir: Path, src: Path, ext: str,
                         used: set[str]) -> Path:
    """Pick a collision-free target name for a figure copied out of source_dir.

    Papers routinely ship figs/plot.pdf alongside plots/plot.pdf; naming targets
    by slugified stem alone made the second silently overwrite the first. Fall
    back to a parent-directory-qualified name, then to a numeric suffix.
    """
    stem = slugify(src.stem, fallback="fig")
    candidates = [stem]
    rel_parent = src.parent.relative_to(source_dir)
    if rel_parent.parts:
        candidates.append(slugify("-".join(rel_parent.parts + (stem,)), fallback="fig"))
    name = next((c for c in candidates if f"{c}{ext}" not in used), None)
    if name is None:
        i = 2
        while f"{stem}-{i}{ext}" in used:
            i += 1
        name = f"{stem}-{i}"
    used.add(f"{name}{ext}")
    return fig_dir / f"{name}{ext}"


def eps_to_png(src: Path, target: Path) -> bool:
    """Rasterize an EPS/PS figure with ghostscript. False if gs is unavailable."""
    gs = shutil.which("gs")
    if not gs:
        return False
    subprocess.run(
        [gs, "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dEPSCrop", "-sDEVICE=png16m",
         "-r150", f"-sOutputFile={target}", str(src)],
        check=True, capture_output=True,
    )
    return True


def collect_bibliography(source_dir: Path, work_dir: Path) -> list[str]:
    """Relative paths of .bbl/.bib files.

    Without these the model sees \\cite{key} in the body with no way to resolve
    what it cites, which makes a grounded related-work section impossible.
    """
    return sorted(
        str(p.relative_to(work_dir))
        for p in source_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in {".bbl", ".bib"}
    )


def content_chars(work_dir: Path, rel_paths: list[str]) -> int:
    """Total character count of the given work-folder-relative files.

    Lets the skill decide whether a paper fits one context or needs subagent
    fan-out, instead of guessing.
    """
    total = 0
    for rel in rel_paths:
        p = work_dir / rel
        try:
            total += len(p.read_text(errors="ignore"))
        except OSError:
            continue
    return total


def collect_figures(source_dir: Path, fig_dir: Path, warnings: list[str],
                    cap: int = 40) -> tuple[list[str], dict[str, str]]:
    """Copy raster figures and convert PDF/EPS figures to PNG.

    Returns (figure paths relative to the work folder, a map from the path as
    written in \\includegraphics to the collected file).
    """
    import pymupdf as fitz  # PyMuPDF

    fig_dir.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    fig_map: dict[str, str] = {}
    used: set[str] = set()

    def record(src: Path, target: Path) -> None:
        rel = f"figures/{target.name}"
        out.append(rel)
        # \includegraphics usually omits the extension, but not always.
        key = src.relative_to(source_dir).as_posix()
        fig_map[key] = rel
        fig_map[key.rsplit(".", 1)[0]] = rel

    candidates = sorted(
        p for p in source_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in RASTER_EXT | {".pdf", ".eps", ".ps"}
    )
    skipped_eps = 0
    for p in candidates:
        if len(out) >= cap:
            warnings.append(f"figure cap ({cap}) reached; remaining figures not converted")
            break
        ext = p.suffix.lower()
        try:
            if ext in RASTER_EXT:
                target = unique_figure_target(fig_dir, source_dir, p, ext, used)
                shutil.copyfile(p, target)
                record(p, target)
            elif ext == ".pdf":
                doc = fitz.open(p)
                if doc.page_count == 0:
                    doc.close()
                    continue
                pix = doc.load_page(0).get_pixmap(dpi=150)
                target = unique_figure_target(fig_dir, source_dir, p, ".png", used)
                pix.save(target)
                doc.close()
                record(p, target)
            else:  # eps / ps
                target = unique_figure_target(fig_dir, source_dir, p, ".png", used)
                if eps_to_png(p, target):
                    record(p, target)
                else:
                    used.discard(target.name)
                    skipped_eps += 1
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"figure conversion failed for {p.name}: {exc}")
    if skipped_eps:
        warnings.append(
            f"{skipped_eps} EPS/PS figure(s) skipped (ghostscript not found; "
            f"install it with: brew install ghostscript)"
        )
    return out, fig_map


# --------------------------------------------------------------------------- #
# PDF + URL extraction
# --------------------------------------------------------------------------- #
def pdf_first_pages_text(pdf_path: Path, n: int = 2) -> str:
    import pymupdf as fitz

    try:
        doc = fitz.open(pdf_path)
        text = "\n".join(doc.load_page(i).get_text() for i in range(min(n, doc.page_count)))
        doc.close()
        return text
    except Exception:  # noqa: BLE001
        return ""


def pdf_metadata_title(pdf_path: Path) -> str | None:
    import pymupdf as fitz

    try:
        doc = fitz.open(pdf_path)
        title = (doc.metadata or {}).get("title")
        doc.close()
        return title.strip() if title and title.strip() else None
    except Exception:  # noqa: BLE001
        return None


def extract_pdf(pdf_path: Path, work_dir: Path, warnings: list[str]) -> tuple[str, list[str]]:
    """Extract full text (.txt) and embedded raster images from a local PDF.
    Returns (relative text path, figure rel-paths)."""
    import pymupdf as fitz

    src_dir = work_dir / "source"
    src_dir.mkdir(parents=True, exist_ok=True)
    if pdf_path.resolve() != (src_dir / pdf_path.name).resolve():
        shutil.copyfile(pdf_path, src_dir / pdf_path.name)

    text_parts: list[str] = []
    figs: list[str] = []
    fig_dir = work_dir / "figures"
    try:
        doc = fitz.open(pdf_path)
        for i in range(doc.page_count):
            page = doc.load_page(i)
            text_parts.append(f"\n\n===== page {i + 1} =====\n")
            text_parts.append(page.get_text())
        # extract embedded images (raster) up to a cap
        seen: set[int] = set()
        for i in range(doc.page_count):
            for img in doc.load_page(i).get_images(full=True):
                xref = img[0]
                if xref in seen or len(figs) >= 30:
                    continue
                seen.add(xref)
                try:
                    base = doc.extract_image(xref)
                    ext = base.get("ext", "png")
                    if base["image"] and base.get("width", 0) >= 80:
                        fig_dir.mkdir(parents=True, exist_ok=True)
                        name = f"p{i + 1}-x{xref}.{ext}"
                        (fig_dir / name).write_bytes(base["image"])
                        figs.append(f"figures/{name}")
                except Exception:  # noqa: BLE001
                    continue
        doc.close()
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"PDF extraction error: {exc}")

    txt_path = work_dir / "extracted.txt"
    txt_path.write_text("".join(text_parts), errors="ignore")
    return "extracted.txt", figs


def is_pdf_payload(head: bytes, content_type: str | None) -> bool:
    """Whether a fetched response is a PDF rather than a web page.

    Magic bytes win: OpenReview, ACL Anthology and journal hosts often serve
    PDFs as application/octet-stream.
    """
    if head[:5] == b"%PDF-":
        return True
    if content_type and content_type.split(";")[0].strip().lower() == "application/pdf":
        return True
    return False


def download_url(url: str, warnings: list[str]) -> tuple[bytes, str | None] | None:
    """Fetch a URL once, returning (body, content-type)."""
    try:
        with httpx.Client(headers={"User-Agent": UA}, timeout=120, follow_redirects=True) as c:
            r = c.get(url)
            r.raise_for_status()
            return r.content, r.headers.get("content-type")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"URL fetch failed: {exc}")
        return None


def extract_url(url: str, work_dir: Path, warnings: list[str],
                raw_html: bytes | None = None) -> tuple[str, str | None]:
    """Extract the main article text from a web page. Returns
    (relative text path, extracted title). Reuses raw_html when the caller has
    already fetched the body, so the page is not downloaded twice."""
    import trafilatura

    title = None
    text = None
    try:
        downloaded = (raw_html.decode("utf-8", errors="ignore")
                      if raw_html is not None else trafilatura.fetch_url(url))
        if downloaded:
            text = trafilatura.extract(
                downloaded, include_comments=False, include_tables=True,
                favor_recall=True,
            )
            md = trafilatura.extract_metadata(downloaded)
            if md and md.title:
                title = md.title
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"URL extraction error: {exc}")
    if not text:
        warnings.append("trafilatura returned no text; falling back to raw fetch")
        try:
            with httpx.Client(headers={"User-Agent": UA}, timeout=60, follow_redirects=True) as c:
                text = c.get(url).text
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"raw fetch failed: {exc}")
            text = ""
    (work_dir / "extracted.txt").write_text(text or "", errors="ignore")
    return "extracted.txt", title


# --------------------------------------------------------------------------- #
# biomedical: PubMed / PubMed Central / bioRxiv / medRxiv
# --------------------------------------------------------------------------- #
# The JATS XML these services publish plays the role the LaTeX source plays for
# arXiv: real section structure, rendered "Figure 2"/"Table 1" labels, table
# cells as text, and a structured reference list.

IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"II*\x00", b"MM\x00*")


def http_get(url: str, warnings: list[str], what: str, params: dict | None = None,
             timeout: int = 60) -> httpx.Response | None:
    try:
        with httpx.Client(headers={"User-Agent": UA}, timeout=timeout,
                          follow_redirects=True) as c:
            r = c.get(url, params=params)
            r.raise_for_status()
            return r
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"{what} failed: {exc}")
        return None


def _strip_tags(text: str | None) -> str | None:
    if not text:
        return None
    import html as _html
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", text))).strip() or None


def title_matches(title: str | None, text: str) -> bool:
    """Whether a resolved title plausibly belongs to the PDF we were given.

    A DOI on a PDF's first pages is usually the paper's own, but it can be a
    cited one; summarising the wrong paper is far worse than a lower-fidelity
    PDF read, so require most of the title's words to appear in the PDF text.
    """
    if not title:
        return False
    words = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) >= 4]
    if not words:
        return False
    hay = set(re.findall(r"[a-z0-9]+", text.lower().replace("-\n", "")))
    return sum(w in hay for w in words) / len(words) >= 0.6


def epmc_search(query: str, warnings: list[str]) -> dict | None:
    r = http_get(f"{EPMC_REST}/search", warnings, "Europe PMC lookup",
                 params={"query": query, "format": "json", "resultType": "core",
                         "pageSize": 1})
    if r is None:
        return None
    try:
        results = r.json().get("resultList", {}).get("result", [])
    except ValueError:
        warnings.append("Europe PMC returned non-JSON")
        return None
    return results[0] if results else None


def meta_from_epmc(rec: dict) -> dict:
    authors = [a.get("fullName") for a in (rec.get("authorList") or {}).get("author", [])
               if a.get("fullName")]
    journal = ((rec.get("journalInfo") or {}).get("journal") or {}).get("title")
    links = [u.get("url") for u in (rec.get("fullTextUrlList") or {}).get("fullTextUrl", [])
             if u.get("url")]
    pmcid = rec.get("pmcid")
    return {
        "title": _strip_tags(rec.get("title")),
        "authors": authors,
        "year": rec.get("pubYear"),
        "abstract": _strip_tags(rec.get("abstractText")),
        "journal": journal,
        "doi": rec.get("doi"),
        "pmid": rec.get("pmid"),
        "pmcid": pmcid,
        "fulltext_links": links,
        "_open_access": rec.get("isOpenAccess") == "Y" or rec.get("inPMC") == "Y",
        "_source": rec.get("source"),
    }


def _save_image(data: bytes, target_stem: Path) -> Path | None:
    """Write image bytes as PNG/JPG/GIF; TIFF (common in PMC packages) -> PNG."""
    if data[:3] == b"\xff\xd8\xff":
        out = target_stem.with_suffix(".jpg")
    elif data[:4] == b"\x89PNG":
        out = target_stem.with_suffix(".png")
    elif data[:4] == b"GIF8":
        out = target_stem.with_suffix(".gif")
    elif data[:4] in (b"II*\x00", b"MM\x00*"):
        import pymupdf as fitz
        out = target_stem.with_suffix(".png")
        pix = fitz.Pixmap(data)
        if pix.colorspace and pix.colorspace.n > 3:  # CMYK -> RGB
            pix = fitz.Pixmap(fitz.csRGB, pix)
        pix.save(out)
        return out
    else:
        return None  # an HTML error page, not an image
    out.write_bytes(data)
    return out


def _record_jats_figure(fig, path: Path, work_dir: Path, figs: list[str],
                        fig_map: dict[str, str]) -> None:
    rel = str(path.relative_to(work_dir))
    figs.append(rel)
    fig_map[fig.id] = rel
    if fig.label:
        fig_map[fig.label] = rel
    for h in fig.hrefs:
        fig_map[h] = rel


def collect_jats_figures_local(doc, src_dir: Path, work_dir: Path,
                               warnings: list[str], cap: int = 40):
    """Figures referenced by <graphic> in a PMC OA package, matched by name.

    Only referenced graphics are taken: packages also ship the article PDF and
    supplementary files, which must not be mistaken for figures.
    """
    fig_dir = work_dir / "figures"
    by_name: dict[str, Path] = {}
    for p in src_dir.rglob("*"):
        if p.is_file():
            by_name.setdefault(p.name.lower(), p)
            by_name.setdefault(p.stem.lower(), p)
    figs: list[str] = []
    fig_map: dict[str, str] = {}
    for fig in doc.figures:
        if len(figs) >= cap:
            warnings.append(f"figure cap ({cap}) reached")
            break
        for href in fig.hrefs:
            src = by_name.get(href.lower()) or by_name.get(Path(href).stem.lower())
            if src is None:
                continue
            fig_dir.mkdir(parents=True, exist_ok=True)
            try:
                saved = _save_image(src.read_bytes(), fig_dir / slugify(fig.id, "fig"))
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"figure conversion failed for {src.name}: {exc}")
                saved = None
            if saved:
                _record_jats_figure(fig, saved, work_dir, figs, fig_map)
                break
    return figs, fig_map


def fetch_jats_figures_remote(doc, url_candidates, work_dir: Path,
                              warnings: list[str], cap: int = 40):
    """Download figures; url_candidates(fig) yields URLs to try in order."""
    fig_dir = work_dir / "figures"
    figs: list[str] = []
    fig_map: dict[str, str] = {}
    failed = 0
    with httpx.Client(headers={"User-Agent": UA}, timeout=60, follow_redirects=True) as c:
        for fig in doc.figures[:cap]:
            for url in url_candidates(fig):
                try:
                    r = c.get(url)
                    if r.status_code != 200:
                        continue
                    fig_dir.mkdir(parents=True, exist_ok=True)
                    saved = _save_image(r.content, fig_dir / slugify(fig.id, "fig"))
                except Exception:  # noqa: BLE001
                    continue
                if saved:
                    _record_jats_figure(fig, saved, work_dir, figs, fig_map)
                    break
            else:
                failed += 1
    if failed:
        warnings.append(f"{failed} figure image(s) could not be downloaded; "
                        f"their captions are still in the text")
    return figs, fig_map


def write_jats(doc, work_dir: Path, manifest: dict) -> None:
    """Write fulltext.md / references.txt and fill metadata gaps from the XML."""
    (work_dir / "fulltext.md").write_text(doc.markdown)
    manifest["main_content"] = "fulltext.md"
    manifest["extracted_text"] = "fulltext.md"
    if doc.references:
        (work_dir / "references.txt").write_text(doc.references)
        manifest["bibliography"] = ["references.txt"]
    for key in ("title", "year", "abstract", "journal", "doi", "pmid", "pmcid"):
        if not manifest.get(key) and getattr(doc, key):
            manifest[key] = getattr(doc, key)
    if not manifest.get("authors") and doc.authors:
        manifest["authors"] = doc.authors


def _parse_jats(xml_bytes: bytes, warnings: list[str]):
    import jats

    try:
        doc = jats.parse(xml_bytes.decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"JATS XML parse failed: {exc}")
        return None
    if not doc.has_body:
        warnings.append("JATS XML has no body text (abstract/metadata only)")
    return doc


def ingest_pmc_fulltext(pmcid: str, work_dir: Path, warnings: list[str],
                        manifest: dict) -> bool:
    """PMC open-access full text into work_dir. Package first, then XML."""
    src_dir = work_dir / "source"
    src_dir.mkdir(parents=True, exist_ok=True)

    # 1. OA package (.tar.gz with .nxml + original figures), the PMC analogue
    #    of an arXiv e-print.
    r = http_get(NCBI_OA, warnings, "PMC OA service lookup", params={"id": pmcid})
    if r is not None:
        m = re.search(r'format="tgz"[^>]*href="([^"]+)"', r.text) or \
            re.search(r'href="([^"]+\.tar\.gz)"', r.text)
        if m:
            url = re.sub(r"^ftp://", "https://", m.group(1))
            pkg = http_get(url, warnings, "PMC OA package download", timeout=180)
            if pkg is not None:
                try:
                    safe_extract_tar(pkg.content, src_dir)
                except tarfile.TarError as exc:
                    warnings.append(f"PMC OA package unreadable: {exc}")
                xmls = sorted(src_dir.rglob("*.nxml")) or sorted(src_dir.rglob("*.xml"))
                if xmls:
                    doc = _parse_jats(xmls[0].read_bytes(), warnings)
                    if doc is not None and doc.has_body:
                        write_jats(doc, work_dir, manifest)
                        manifest["figures"], manifest["figures_map"] = \
                            collect_jats_figures_local(doc, src_dir, work_dir, warnings)
                        manifest["ingest_method"] = "pmc-package"
                        return True
        elif "idIsNotOpenAccess" in r.text or "error" in r.text.lower():
            warnings.append(f"{pmcid} has no PMC OA package (not in the OA subset)")

    # 2. Europe PMC full-text XML; figures fetched by graphic href.
    r = http_get(f"{EPMC_REST}/{pmcid}/fullTextXML", warnings, "Europe PMC full-text XML")
    if r is None or not r.content.strip():
        return False
    (src_dir / f"{pmcid}.xml").write_bytes(r.content)
    doc = _parse_jats(r.content, warnings)
    if doc is None or not doc.has_body:
        return False
    write_jats(doc, work_dir, manifest)

    def urls(fig):
        for h in fig.hrefs:
            stem = h if Path(h).suffix else h + ".jpg"
            yield EPMC_FIG.format(pmcid=pmcid, href=stem)

    manifest["figures"], manifest["figures_map"] = \
        fetch_jats_figures_remote(doc, urls, work_dir, warnings)
    manifest["ingest_method"] = "pmc-xml"
    return True


def _write_abstract_only(work_dir: Path, manifest: dict) -> None:
    lines = [f"# {manifest.get('title') or ''}", ""]
    if manifest.get("journal"):
        lines += [f"Journal: {manifest['journal']}", ""]
    lines += ["## Abstract", "", manifest.get("abstract") or "(no abstract available)", ""]
    (work_dir / "extracted.txt").write_text("\n".join(lines))
    manifest["main_content"] = "extracted.txt"
    manifest["extracted_text"] = "extracted.txt"


def ingest_pubmed(kind: str, value: str, base_dir: Path, warnings: list[str],
                  manifest: dict, expect_text: str | None = None) -> bool:
    """PubMed id, PMC id or DOI -> PMC full text, else the abstract.

    With expect_text (called from a PDF), succeed only on verified full text so
    the caller can fall back to reading the PDF itself.
    """
    query = {"pmc": f"PMCID:{value}", "pubmed": f"EXT_ID:{value} AND SRC:MED",
             "doi": f'DOI:"{value}"'}[kind]
    rec = epmc_search(query, warnings)
    meta = meta_from_epmc(rec) if rec else {}
    if rec and meta.get("_source") == "PPR" and BIO_DOI_RE.search(meta.get("doi") or ""):
        return ingest_biorxiv("", meta["doi"], "", base_dir, warnings, manifest, expect_text)
    if expect_text is not None:
        if not (meta.get("pmcid") and title_matches(meta.get("title"), expect_text)):
            return False
    if not rec and kind != "pmc":
        warnings.append(f"{kind} {value} not found in Europe PMC")
        return False

    for k, v in meta.items():
        if not k.startswith("_") and v:
            manifest[k] = v
    if kind == "pmc":
        manifest["pmcid"] = manifest.get("pmcid") or value
    manifest["primary_category"] = None
    manifest["domain_hint"] = "bio"
    manifest["domain_subcategory"] = "journal" if manifest.get("journal") else None

    work_dir = make_work_dir(base_dir, manifest.get("title"), None,
                             manifest.get("pmcid") or f"pmid-{manifest.get('pmid') or value}")
    manifest["work_dir"] = str(work_dir)
    pmcid = manifest.get("pmcid")
    if pmcid and ingest_pmc_fulltext(pmcid, work_dir, warnings, manifest):
        return True
    if expect_text is not None:
        shutil.rmtree(work_dir, ignore_errors=True)
        manifest["work_dir"] = None
        return False
    if not manifest.get("abstract") and not manifest.get("title"):
        return False
    warnings.append(
        "No open-access full text in PubMed Central — only the title and abstract "
        "were ingested. Ask the user for the PDF to write a full summary."
    )
    _write_abstract_only(work_dir, manifest)
    manifest["ingest_method"] = "pubmed-abstract"
    return True


def biorxiv_record(doi: str, version: str, server_hint: str,
                   warnings: list[str]) -> tuple[str, dict] | None:
    for server in ([server_hint] if server_hint else ["biorxiv", "medrxiv"]):
        r = http_get(BIORXIV_API.format(server=server, doi=doi), warnings,
                     f"{server} API lookup")
        if r is None:
            continue
        try:
            coll = r.json().get("collection") or []
        except ValueError:
            continue
        if not coll:
            continue
        pick = next((c for c in coll if version and str(c.get("version")) == version), coll[-1])
        return server, pick
    return None


def ingest_biorxiv(server_hint: str, doi: str, version: str, base_dir: Path,
                   warnings: list[str], manifest: dict,
                   expect_text: str | None = None) -> bool:
    m = BIO_DOI_RE.search(doi)
    doi = f"10.1101/{m.group(1)}" if m else doi
    version = version or (m.group(2) if m and m.group(2) else "")
    found = biorxiv_record(doi, version, server_hint, warnings)
    if found is None:
        warnings.append(f"preprint {doi} not found on bioRxiv/medRxiv")
        return False
    server, rec = found
    title = re.sub(r"\s+", " ", rec.get("title") or "").strip() or None
    if expect_text is not None and not title_matches(title, expect_text):
        return False

    manifest.update({
        "title": title,
        "authors": [a.strip() for a in (rec.get("authors") or "").split(";") if a.strip()],
        "year": (rec.get("date") or "")[:4] or None,
        "abstract": _strip_tags(rec.get("abstract")),
        "doi": doi,
        "server": server,
        "version": str(rec.get("version") or ""),
        "primary_category": rec.get("category"),
        "journal": f"{server} (preprint, not peer-reviewed)",
        "domain_hint": "bio",
        "domain_subcategory": f"{server}:{rec.get('category') or ''}".rstrip(":"),
    })
    published = rec.get("published")
    if published and published != "NA":
        manifest["published_doi"] = published
        warnings.append(f"This preprint has since been published (DOI {published}); "
                        f"the peer-reviewed version may differ.")

    work_dir = make_work_dir(base_dir, title, None, doi)
    manifest["work_dir"] = str(work_dir)
    src_dir = work_dir / "source"
    src_dir.mkdir(parents=True, exist_ok=True)

    jats_url = rec.get("jatsxml")
    if jats_url:
        r = http_get(jats_url, warnings, f"{server} JATS XML download")
        doc = _parse_jats(r.content, warnings) if r is not None else None
        if doc is not None and doc.has_body:
            (src_dir / Path(jats_url).name).write_bytes(r.content)
            write_jats(doc, work_dir, manifest)
            # .../content/early/2020/03/24/2020.03.22.002386.source.xml
            #   -> .../content/biorxiv/early/2020/03/24/2020.03.22.002386/F1.large.jpg
            base = re.sub(r"\.source\.xml$", "", jats_url)
            base_fig = base.replace("/content/early/", f"/content/{server}/early/")
            xml_dir = jats_url.rsplit("/", 1)[0]

            def urls(fig):
                yield f"{base_fig}/{fig.id}.large.jpg"
                for h in fig.hrefs:
                    yield f"{xml_dir}/{h}"

            manifest["figures"], manifest["figures_map"] = \
                fetch_jats_figures_remote(doc, urls, work_dir, warnings)
            manifest["ingest_method"] = f"{server}-xml"
            return True

    if expect_text is not None:  # caller has the PDF already; let it read that
        shutil.rmtree(work_dir, ignore_errors=True)
        manifest["work_dir"] = None
        return False

    pdf_url = BIORXIV_PDF.format(server=server, doi=doi, version=manifest["version"] or "1")
    r = http_get(pdf_url, warnings, f"{server} PDF download", timeout=120)
    if r is not None and r.content[:5] == b"%PDF-":
        pdf_path = src_dir / f"{slugify(doi)}.pdf"
        pdf_path.write_bytes(r.content)
        txt, figs = extract_pdf(pdf_path, work_dir, warnings)
        manifest["main_content"] = txt
        manifest["extracted_text"] = txt
        manifest["figures"] = figs
        manifest["ingest_method"] = f"{server}-pdf"
        return True

    warnings.append("Full text could not be downloaded — only the abstract was "
                    "ingested. Ask the user for the PDF to write a full summary.")
    _write_abstract_only(work_dir, manifest)
    manifest["ingest_method"] = f"{server}-abstract"
    return True


def try_structured_from_pdf(probe: str, base_dir: Path, warnings: list[str],
                            manifest: dict) -> bool:
    """A biomedical PDF often has an open JATS version; prefer it if verified."""
    doi = extract_doi(probe)
    if not doi:
        return False
    log(f"[ingest] DOI {doi} found in PDF; looking for open full-text XML")
    scratch: list[str] = []  # lookups failing here are expected, not warnings
    snapshot = dict(manifest)
    if BIO_DOI_RE.search(doi):
        ok = ingest_biorxiv("", doi, "", base_dir, scratch, manifest, expect_text=probe)
    else:
        ok = ingest_pubmed("doi", doi, base_dir, scratch, manifest, expect_text=probe)
    if ok:
        warnings.extend(scratch)
    else:  # undo metadata from a lookup we are not going to use
        manifest.clear()
        manifest.update(snapshot)
    return ok


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def make_work_dir(base_dir: Path, title: str | None, arxiv_id: str | None,
                  input_arg: str) -> Path:
    if title:
        name = slugify(title)
    elif arxiv_id:
        name = "arxiv-" + slugify(arxiv_id)
    else:
        name = slugify(Path(input_arg).stem)
    work = base_dir / name
    suffix = 2
    while work.exists() and any(work.iterdir()):
        work = base_dir / f"{name}-{suffix}"
        suffix += 1
    work.mkdir(parents=True, exist_ok=True)
    return work


def ingest_arxiv(arxiv_id: str, base_dir: Path, warnings: list[str],
                 manifest: dict) -> dict:
    meta = fetch_arxiv_metadata(arxiv_id, warnings)
    manifest.update(meta)
    manifest["arxiv_id"] = arxiv_id

    work_dir = make_work_dir(base_dir, meta.get("title"), arxiv_id, arxiv_id)
    manifest["work_dir"] = str(work_dir)
    src_dir = work_dir / "source"
    src_dir.mkdir(parents=True, exist_ok=True)

    blob = download_arxiv_source(arxiv_id, warnings)
    if blob is None:
        # No source: fall back to downloading the PDF.
        warnings.append("falling back to arXiv PDF (no usable source)")
        pdf_path = src_dir / f"{slugify(arxiv_id)}.pdf"
        try:
            with httpx.Client(headers={"User-Agent": UA}, timeout=120, follow_redirects=True) as c:
                r = c.get(f"https://arxiv.org/pdf/{arxiv_id}")
                r.raise_for_status()
                pdf_path.write_bytes(r.content)
            manifest["ingest_method"] = "arxiv-pdf"
            txt, figs = extract_pdf(pdf_path, work_dir, warnings)
            manifest["main_content"] = txt
            manifest["extracted_text"] = txt
            manifest["figures"] = figs
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"arXiv PDF fallback failed: {exc}")
        return manifest

    kind, payload = blob
    if kind == "pdf":
        warnings.append("e-print was a PDF (no LaTeX source submitted)")
        pdf_path = src_dir / f"{slugify(arxiv_id)}.pdf"
        pdf_path.write_bytes(payload)
        manifest["ingest_method"] = "arxiv-pdf"
        txt, figs = extract_pdf(pdf_path, work_dir, warnings)
        manifest["main_content"] = txt
        manifest["extracted_text"] = txt
        manifest["figures"] = figs
        return manifest

    if kind == "gz-tex":
        main = src_dir / "main.tex"
        main.write_bytes(payload)
    else:  # tar
        safe_extract_tar(payload, src_dir)

    manifest["ingest_method"] = "arxiv-source"
    main_tex = find_main_tex(src_dir)
    if main_tex:
        manifest["main_content"] = str(main_tex.relative_to(work_dir))
    else:
        warnings.append("no main .tex found in source")
    manifest["all_tex"] = [str(p.relative_to(work_dir)) for p in src_dir.rglob("*.tex")]
    manifest["bibliography"] = collect_bibliography(src_dir, work_dir)
    manifest["figures"], manifest["figures_map"] = collect_figures(
        src_dir, work_dir / "figures", warnings
    )
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description="paper-summary ingestion engine")
    ap.add_argument("input", help="arXiv id/URL, PubMed/PMC id, DOI, bioRxiv/medRxiv DOI/URL, "
                         "local .pdf path, or web URL")
    ap.add_argument("--base-dir", default=os.getcwd(),
                    help="directory under which the work folder is created")
    ap.add_argument("--domain", default=None,
                    help="override the auto-detected domain (hep|ai|physics|bio|general)")
    args = ap.parse_args()

    base_dir = Path(args.base_dir).expanduser().resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    kind, value = detect_input(args.input)
    log(f"[ingest] input detected as: {kind} -> {value}")

    manifest: dict = {
        "input": args.input,
        "input_type": kind,
        "arxiv_id": None,
        "title": None,
        "authors": [],
        "year": None,
        "primary_category": None,
        "abstract": None,
        "ingest_method": None,
        "work_dir": None,
        "main_content": None,
        "extracted_text": None,
        "all_tex": [],
        "bibliography": [],
        "figures": [],
        "figures_map": {},
        "content_chars": 0,
        # biomedical sources (PubMed / PMC / bioRxiv / medRxiv)
        "doi": None,
        "pmid": None,
        "pmcid": None,
        "journal": None,
        "server": None,
        "published_doi": None,
        "fulltext_links": [],
        "warnings": warnings,
    }

    # A PDF may itself be an arXiv paper: prefer the source if so.
    if kind == "pdf":
        ingest_pdf_file(Path(value), base_dir, warnings, manifest, "pdf->arxiv")
    elif kind == "arxiv":
        ingest_arxiv(value, base_dir, warnings, manifest)
    elif kind == "biorxiv":
        server, doi, version = value.split("|")
        ingest_biorxiv(server, doi, version, base_dir, warnings, manifest)
    elif kind in {"pmc", "pubmed"}:
        ingest_pubmed(kind, value, base_dir, warnings, manifest)
    elif kind == "doi" and ingest_pubmed("doi", value, base_dir, warnings, manifest):
        pass
    else:  # url (or a DOI Europe PMC does not know) — may serve HTML or a PDF
        if kind == "doi":
            manifest["doi"] = manifest.get("doi") or value
            value = f"https://doi.org/{value}"
        payload = download_url(value, warnings)
        body, ctype = payload if payload else (b"", None)
        if payload and is_pdf_payload(body, ctype):
            log("[ingest] URL served a PDF; using the PDF extractor")
            manifest["input_type"] = "url->pdf"
            with tempfile.TemporaryDirectory() as td:
                tmp_pdf = Path(td) / f"{slugify(Path(value).stem, fallback='paper')}.pdf"
                tmp_pdf.write_bytes(body)
                ingest_pdf_file(tmp_pdf, base_dir, warnings, manifest, "url->arxiv")
            if manifest.get("ingest_method") == "local-pdf":
                manifest["ingest_method"] = "url-pdf"
        else:
            work_dir = make_work_dir(base_dir, None, None, value or "web-paper")
            manifest["work_dir"] = str(work_dir)
            manifest["ingest_method"] = "url"
            txt, title = extract_url(value, work_dir, warnings, raw_html=body or None)
            manifest["main_content"] = txt
            manifest["extracted_text"] = txt
            if title:
                manifest["title"] = title

    # Domain resolution (override wins).
    manifest["domain"] = (args.domain or manifest.pop("domain_hint", None)
                          or domain_for_category(manifest.get("primary_category")))
    manifest.pop("domain_hint", None)
    manifest["domain_subcategory"] = (manifest.get("domain_subcategory")
                                      or manifest.get("primary_category"))

    if not manifest.get("work_dir"):
        log("[ingest] FATAL: no work dir produced")
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 1

    # Size of whatever the skill will actually read, so it can decide between
    # reading inline and fanning out to subagents.
    work = Path(manifest["work_dir"])
    read_targets = manifest["all_tex"] or (
        [manifest["extracted_text"]] if manifest.get("extracted_text") else []
    )
    manifest["content_chars"] = content_chars(work, read_targets)

    manifest_path = Path(manifest["work_dir"]) / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    log(f"[ingest] done. method={manifest['ingest_method']} domain={manifest['domain']} "
        f"figures={len(manifest['figures'])} warnings={len(warnings)}")
    print(str(manifest_path))
    return 0


def ingest_pdf_file(pdf_path: Path, base_dir: Path, warnings: list[str],
                    manifest: dict, arxiv_tag: str) -> None:
    """Ingest a PDF on disk, preferring the arXiv source when the PDF reveals an
    arXiv id. Shared by the local-file and remote-URL entry points."""
    probe = (pdf_metadata_title(pdf_path) or "") + "\n" + pdf_first_pages_text(pdf_path)
    aid = extract_arxiv_id(probe)
    if aid:
        log(f"[ingest] arXiv id {aid} found in PDF; fetching source instead")
        manifest["input_type"] = arxiv_tag
        ingest_arxiv(aid, base_dir, warnings, manifest)
        if manifest.get("main_content"):
            return
        warnings.append("arXiv source via PDF failed; using the PDF text")
    elif try_structured_from_pdf(probe, base_dir, warnings, manifest):
        manifest["input_type"] = arxiv_tag.replace("arxiv", "jats")
        # keep the user's PDF beside the XML it was matched to
        src = Path(manifest["work_dir"]) / "source"
        src.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pdf_path, src / pdf_path.name)
        return
    _ingest_local_pdf(pdf_path, base_dir, warnings, manifest)


def _ingest_local_pdf(pdf_path: Path, base_dir: Path, warnings: list[str],
                      manifest: dict) -> None:
    title = pdf_metadata_title(pdf_path)
    work_dir = make_work_dir(base_dir, title, None, str(pdf_path))
    manifest["work_dir"] = str(work_dir)
    manifest["ingest_method"] = "local-pdf"
    if title:
        manifest["title"] = title
    txt, figs = extract_pdf(pdf_path, work_dir, warnings)
    manifest["main_content"] = txt
    manifest["extracted_text"] = txt
    manifest["figures"] = figs


if __name__ == "__main__":
    sys.exit(main())
