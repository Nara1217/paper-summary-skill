# paper-summary

A [Claude Code](https://claude.com/claude-code) skill that turns a research paper
into a structured, grounded summary — and renders it to PDF.

Give it an arXiv id, a local PDF, or a URL. It fetches the paper (preferring the
**LaTeX source**, so equations and figures survive intact), writes a sectioned
summary with every number traced back to the source, and produces a typeset PDF.

## Why the LaTeX source matters

Most tools read the PDF and get mangled equations and no usable figures. This one
downloads the arXiv e-print source first, so:

- equations come from the real `\begin{equation}`, not from OCR'd glyphs
- figures are the original files, not screenshots — including `.eps`, which
  older hep-ph/hep-th papers use almost exclusively
- `\cite{}` keys resolve against the shipped `.bbl`, so related-work claims name
  actual papers

PDF and web extraction are the fallbacks, not the default path.

## Biomedical papers (PubMed / PMC / bioRxiv / medRxiv)

Life-science papers rarely live on arXiv, so the same idea is applied to their
**JATS full-text XML** — the structured source that PubMed Central and
bioRxiv/medRxiv publish:

| Input | Source tried, in order |
|---|---|
| `PMC6483969`, PMC URL | PMC open-access package (XML + original figures) → Europe PMC full-text XML |
| `PMID:31043725`, pubmed URL, bare number | Europe PMC lookup → the PMC paths above → abstract only |
| DOI (`10.xxxx/...`) | Europe PMC lookup → PMC paths → the publisher page |
| bioRxiv / medRxiv DOI or URL | bioRxiv API JATS XML + figures → preprint PDF → abstract only |
| local PDF with a DOI | the matching open JATS full text, **only if the title matches** → the PDF itself |

The XML keeps rendered labels (`Figure 2`, `Table 1`), table cells as text (so
`verify.py` can check table numbers) and a structured reference list. When a
paper is not open access only the abstract is ingested, and the skill asks for
the PDF instead of summarising from the abstract. Preprints that have since
been published are flagged. These papers use the `bio` domain checklist
(model system, biological vs. technical replicates, controls, statistics,
clinical design, translational relevance).

## Install

In Claude Code:

```
/plugin marketplace add jinheungkim1216/paper-summary-skill
/plugin install paper-summary@paper-summary-skill
```

Update later with `/plugin marketplace update paper-summary-skill`.

### Dependencies

**Required** — `uv` (runs `ingest.py` with its own deps), `pandoc` and `typst`
(the PDF step).

**Recommended** — `ghostscript` (converts EPS/PS figures; without it those
figures are skipped), and a Hangul font for Korean output (macOS ships Apple SD
Gothic Neo; on Linux install Noto Sans CJK KR or NanumGothic).

```bash
brew install uv pandoc typst ghostscript
```

`skills/paper-summary/scripts/setup.sh` reports exactly what is missing and how
to install it.

## Usage

Once installed, just ask Claude Code:

```
이 논문 요약해줘 https://arxiv.org/abs/1706.03762
PMID 31043725 정리해줘
https://www.biorxiv.org/content/10.1101/2020.03.22.002386v1 요약해줘
summarize arxiv 2005.14165
(drop a PDF) 이거 정리해줘
```

Output lands in a per-paper folder in your current directory:

```
attention-is-all-you-need/
├── manifest.json     # what was ingested, and how
├── source/           # the paper's .tex / .pdf / JATS .xml
├── fulltext.md       # JATS ingests: the paper as text (+ references.txt)
├── figures/          # extracted figures, normalized to PNG
├── summary.md        # the deliverable
└── summary.pdf       # typeset via pandoc → typst
```

## How it works

The skill splits acquisition from writing. Scripts fetch and verify; the model
reads and writes. It never re-implements downloading or parsing.

```
input → ingest.py → [model reads + writes] → verify.py → render_pdf.sh
         manifest       summary.md            grounding      PDF
```

| Step | What happens |
|---|---|
| 1. Ingest | `ingest.py` resolves the input and writes `manifest.json` |
| 2. Read | Two passes: skim for structure, then ground every claim |
| 3. Domain | Apply the field-specific checklist from `domains/` |
| 4. Write | `summary.md` from a fixed 10-section template |
| 5. Verify | `verify.py` traces every number back to the source |
| 6. Render | `md → typ → pdf` |
| 7. Report | Paths, ingest method, warnings, accepted exceptions |

### Grounding

The summary format is built around not making things up:

- numbers and quotes are copied verbatim; anything inferred is tagged `(추론)` /
  `(inferred)` so it is never presented as the paper's own claim
- each key claim carries a reference tag (`§4.1, Table 3`)
- `verify.py` then checks the result — it extracts every number in the summary
  and reports the ones it cannot find in the ingested source

`verify.py` is deliberately conservative: reference tags, figure paths, layout
attributes and inference-marked lines are excluded, because a false alarm costs
more attention than a miss. It reports; it does not block (`--strict` changes
that).

```
$ uv run skills/paper-summary/scripts/verify.py ./attention-is-all-you-need
verify: 7 number(s) checked, 1 not found in source

  summary.md:8          99.7  |  - 정확도가 **99.7** 로 향상되었다 (§6.2, Table 3).
```

### Domain supplements

`manifest.domain` is inferred from the arXiv category and selects a checklist:

- **hep** — branches further on hep-ex / hep-ph / hep-th / hep-lat (luminosity
  and systematics vs. EFT parameter space vs. lattice ensembles)
- **ai** — architecture, training setup, compute, ablations, reproducibility
- **physics** — regime of validity, governing equations, uncertainties
- **bio** — model system, replicates, controls, statistics, clinical design,
  translational relevance (PubMed / PMC / bioRxiv / medRxiv, arXiv q-bio)
- **general** — claim scope, evidence type, threats to validity

## Development

```
paper-summary-skill/
├── .claude-plugin/
│   ├── plugin.json          # plugin manifest
│   └── marketplace.json     # lets this repo be added as a marketplace on its own
└── skills/paper-summary/
    ├── SKILL.md             # the skill itself
    ├── domains/             # per-field supplement checklists
    ├── scripts/             # ingest.py, jats.py, verify.py, render_pdf.sh, setup.sh
    └── tests/
```

Edit a clone of this repo, not the installed copy:

```bash
git clone https://github.com/jinheungkim1216/paper-summary-skill.git
cd paper-summary-skill

./skills/paper-summary/tests/run.sh              # deps come from uv, no venv needed
./skills/paper-summary/tests/run.sh -q -k figure # a single test
```

There is no version field — the git SHA is the version, so pushing here is the
release. Pick the change up with `/plugin marketplace update paper-summary-skill`.

The suite covers figure-name collisions, EPS conversion, font selection across
platforms, the stdout contract, remote-PDF routing, and the grounding checker's
exclusion rules.

## License

MIT — see [LICENSE](LICENSE).
