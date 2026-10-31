"""Tests for the PubMed / PMC / bioRxiv ingestion paths.

Network-free: every remote service is replaced by a local HTTP server that
serves fixture responses, by repointing ingest's module-level base URLs.
"""

import http.server
import io
import json
import sys
import tarfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ingest  # noqa: E402
import jats  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NXML = (FIXTURES / "pmc_sample.nxml").read_text()
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


# --------------------------------------------------------------------------- #
# JATS conversion
# --------------------------------------------------------------------------- #
def test_jats_metadata_is_extracted():
    doc = jats.parse(NXML)
    assert doc.title.startswith("Kinase X inhibition")
    assert doc.authors == ["Nara Kim", "Taeyoon Park", "The Test Consortium"]
    assert (doc.year, doc.pmid, doc.pmcid, doc.doi) == (
        "2019", "31043725", "PMC6483969", "10.1234/tjb.2019.0042")
    assert doc.journal == "Test Journal of Biochemistry"
    assert doc.has_body


def test_jats_named_entities_do_not_break_parsing():
    """&nbsp; / &ndash; come from the DTD, which ElementTree never loads."""
    doc = jats.parse(NXML)
    assert "62" in doc.abstract and "Background:" in doc.abstract
    assert "–" in doc.markdown  # &ndash;


def test_jats_body_keeps_numbers_labels_and_tables_as_text():
    md = jats.parse(NXML).markdown
    assert "## 1 Introduction" in md
    assert "### 2.1 KX-7 inhibits tumour growth" in md
    assert "K_m is 2.5" in md and "10^3-fold" in md
    assert "[Figure 1 | id=F1] Tumour volume over time." in md
    assert "[Table 1] IC50 values." in md
    assert "| KX-7 | 14.2 |" in md
    assert "Measured in triplicate." in md
    assert "$$V = \\frac{L W^2}{2}$$ (1)" in md


def test_jats_references_are_listed():
    refs = jats.parse(NXML).references.splitlines()
    assert refs[0].startswith("[R1] 1 Lee S. Substrate Y is a target of Kinase X. Cell Rep. 2015")
    assert "Nature. 2017;541:100-105" in refs[1]


def test_jats_figures_carry_graphic_hrefs():
    figs = jats.parse(NXML).figures
    assert [(f.id, f.label, f.hrefs) for f in figs] == [("F1", "Figure 1", ["tjb-42-g001"])]


# --------------------------------------------------------------------------- #
# input detection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("arg, expected", [
    ("PMC6483969", ("pmc", "PMC6483969")),
    ("https://pmc.ncbi.nlm.nih.gov/articles/PMC6483969/", ("pmc", "PMC6483969")),
    ("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC6483969", ("pmc", "PMC6483969")),
    ("https://europepmc.org/article/PMC/PMC6483969", ("pmc", "PMC6483969")),
    ("PMID:31043725", ("pubmed", "31043725")),
    ("31043725", ("pubmed", "31043725")),
    ("https://pubmed.ncbi.nlm.nih.gov/31043725/", ("pubmed", "31043725")),
    ("10.1038/s41586-020-2012-7", ("doi", "10.1038/s41586-020-2012-7")),
    ("https://doi.org/10.1038/s41586-020-2012-7", ("doi", "10.1038/s41586-020-2012-7")),
    ("10.1101/2020.03.22.002386", ("biorxiv", "|10.1101/2020.03.22.002386|")),
    ("https://www.biorxiv.org/content/10.1101/2020.03.22.002386v2",
     ("biorxiv", "biorxiv|10.1101/2020.03.22.002386|2")),
    ("https://www.medrxiv.org/content/10.1101/2020.02.11.20021493v1.full",
     ("biorxiv", "medrxiv|10.1101/2020.02.11.20021493|1")),
    ("10.48550/arXiv.1706.03762", ("arxiv", "1706.03762")),
    ("1706.03762", ("arxiv", "1706.03762")),
    ("https://arxiv.org/abs/1706.03762", ("arxiv", "1706.03762")),
])
def test_detect_input_biomedical(arg, expected):
    assert ingest.detect_input(arg) == expected


def test_title_match_guards_against_a_cited_doi():
    text = "Kinase X inhibition reduces tumour growth in a mouse xenograft model\n..."
    assert ingest.title_matches("Kinase X inhibition reduces tumour growth", text)
    assert not ingest.title_matches("Deep residual learning for image recognition", text)


# --------------------------------------------------------------------------- #
# end-to-end against a fake service
# --------------------------------------------------------------------------- #
def _serve(routes):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            for prefix, (ctype, body) in routes.items():
                if path.startswith(prefix) and (callable(body) or True):
                    data = body(query) if callable(body) else body
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.end_headers()
                    self.wfile.write(data)
                    return
            self.send_response(404)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


EPMC_RECORD = {"resultList": {"result": [{
    "source": "MED", "pmid": "31043725", "pmcid": "PMC6483969",
    "doi": "10.1234/tjb.2019.0042",
    "title": "Kinase X inhibition reduces tumour growth in a mouse xenograft model.",
    "authorList": {"author": [{"fullName": "Kim N"}, {"fullName": "Park T"}]},
    "pubYear": "2019", "abstractText": "<h4>Background</h4>Kinase X ...",
    "journalInfo": {"journal": {"title": "Test Journal of Biochemistry"}},
    "isOpenAccess": "Y", "inPMC": "Y",
}]}}


def _manifest():
    return {"input": "x", "input_type": "pubmed", "arxiv_id": None, "title": None,
            "authors": [], "year": None, "primary_category": None, "abstract": None,
            "ingest_method": None, "work_dir": None, "main_content": None,
            "extracted_text": None, "all_tex": [], "bibliography": [], "figures": [],
            "figures_map": {}, "content_chars": 0, "warnings": []}


def test_pubmed_id_ingests_pmc_oa_package_with_figures(tmp_path, monkeypatch):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in [("PMC6483969/tjb-42.nxml", NXML.encode()),
                           ("PMC6483969/tjb-42-g001.jpg", JPEG),
                           ("PMC6483969/tjb-42.pdf", b"%PDF-1.4 not a figure")]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    srv, base = _serve({
        "/rest/search": ("application/json", json.dumps(EPMC_RECORD).encode()),
        "/oa": ("text/xml", lambda q: (
            f'<OA><records><record id="PMC6483969"><link format="tgz" '
            f'href="{base}/pkg/PMC6483969.tar.gz"/></record></records></OA>').encode()),
        "/pkg/": ("application/gzip", buf.getvalue()),
    })
    try:
        monkeypatch.setattr(ingest, "EPMC_REST", f"{base}/rest")
        monkeypatch.setattr(ingest, "NCBI_OA", f"{base}/oa")
        m = _manifest()
        assert ingest.ingest_pubmed("pubmed", "31043725", tmp_path, m["warnings"], m)
    finally:
        srv.shutdown()

    work = Path(m["work_dir"])
    assert m["ingest_method"] == "pmc-package"
    assert m["pmcid"] == "PMC6483969" and m["journal"] == "Test Journal of Biochemistry"
    assert m["domain_hint"] == "bio"
    assert m["main_content"] == "fulltext.md" and m["bibliography"] == ["references.txt"]
    assert "| KX-7 | 14.2 |" in (work / "fulltext.md").read_text()
    # only the referenced graphic is a figure — not the package's article PDF
    assert m["figures"] == ["figures/f1.jpg"]
    assert m["figures_map"]["Figure 1"] == "figures/f1.jpg"
    assert m["figures_map"]["F1"] == "figures/f1.jpg"


def test_pubmed_without_open_full_text_falls_back_to_abstract(tmp_path, monkeypatch):
    rec = json.loads(json.dumps(EPMC_RECORD))
    r = rec["resultList"]["result"][0]
    r.pop("pmcid")
    r["isOpenAccess"] = "N"
    r["inPMC"] = "N"
    srv, base = _serve({"/rest/search": ("application/json", json.dumps(rec).encode())})
    try:
        monkeypatch.setattr(ingest, "EPMC_REST", f"{base}/rest")
        m = _manifest()
        assert ingest.ingest_pubmed("pubmed", "31043725", tmp_path, m["warnings"], m)
    finally:
        srv.shutdown()
    assert m["ingest_method"] == "pubmed-abstract"
    assert any("only the title and abstract" in w for w in m["warnings"])
    assert "Kinase X" in (Path(m["work_dir"]) / "extracted.txt").read_text()


def test_biorxiv_doi_ingests_jats_and_flags_published_version(tmp_path, monkeypatch):
    srv, base = _serve({
        "/details/biorxiv/": ("application/json", lambda q: json.dumps({"collection": [
            {"doi": "10.1101/2020.03.22.002386", "title": "Kinase X inhibition reduces "
             "tumour growth in a mouse xenograft model", "authors": "Kim, N.; Park, T.",
             "date": "2020-03-24", "version": "1", "category": "cancer biology",
             "jatsxml": f"{base}/content/early/2020/03/24/2020.03.22.002386.source.xml",
             "abstract": "Kinase X ...", "published": "10.1234/tjb.2019.0042"}]}).encode()),
        "/content/early/": ("text/xml", NXML.encode()),
        "/content/biorxiv/early/2020/03/24/2020.03.22.002386/F1.large.jpg":
            ("image/jpeg", JPEG),
    })
    try:
        monkeypatch.setattr(ingest, "BIORXIV_API", base + "/details/{server}/{doi}")
        m = _manifest()
        assert ingest.ingest_biorxiv("biorxiv", "10.1101/2020.03.22.002386", "",
                                     tmp_path, m["warnings"], m)
    finally:
        srv.shutdown()
    assert m["ingest_method"] == "biorxiv-xml"
    assert m["domain_subcategory"] == "biorxiv:cancer biology"
    assert m["authors"] == ["Kim, N.", "Park, T."]
    assert m["published_doi"] == "10.1234/tjb.2019.0042"
    assert any("has since been published" in w for w in m["warnings"])
    assert m["figures_map"]["Figure 1"] == "figures/f1.jpg"


def test_pdf_with_mismatched_doi_title_is_not_rerouted(tmp_path, monkeypatch):
    srv, base = _serve({"/rest/search": ("application/json", json.dumps(EPMC_RECORD).encode())})
    try:
        monkeypatch.setattr(ingest, "EPMC_REST", f"{base}/rest")
        m = _manifest()
        before = dict(m)
        probe = "An unrelated paper about plant roots. doi:10.1234/tjb.2019.0042"
        assert not ingest.try_structured_from_pdf(probe, tmp_path, m["warnings"], m)
    finally:
        srv.shutdown()
    assert m == before
    assert not any(tmp_path.iterdir()), "no work folder should be left behind"
