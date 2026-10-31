---
name: paper-summary
description: Use when the user wants to summarize, digest, or take structured notes on an academic/research paper supplied as an arXiv id or URL, a PubMed id/URL, a PMC id, a DOI, a bioRxiv/medRxiv DOI or URL, a local PDF file, or a web article URL. Triggers include "이 논문 요약해줘", "summarize this paper", "arxiv ... 정리", "PMID ... 정리", "bioRxiv 논문 요약", dropping a .pdf and asking for a summary.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent
---

# Paper Summary

Turn a paper (arXiv id/URL, PubMed/PMC id, DOI, bioRxiv/medRxiv, local PDF, or web URL) into a structured, faithful
summary written to a per-paper work folder, then render it to PDF.

**Core split:** `ingest.py` (a `uv` script) acquires the paper and writes a
`manifest.json`; YOU read the manifest and content and write the summary. Do not
re-implement downloading/parsing — that is the script's job.

**Skill layout:** `<SKILL_DIR>` is the directory containing this SKILL.md (the
skill's base directory, given to you when the skill is invoked). Helper scripts
live in `<SKILL_DIR>/scripts/` (`ingest.py`, `verify.py`, `render_pdf.sh`,
`setup.sh`) and domain guides in `<SKILL_DIR>/domains/`. Substitute the real
path for `<SKILL_DIR>`.

## Workflow

### 1. Ingest

Run the ingestion engine, with the user's current working directory as base:

```bash
uv run <SKILL_DIR>/scripts/ingest.py "<INPUT>" --base-dir "$(pwd)"
```

`<INPUT>` is the arXiv id/URL, PubMed/PMC id, DOI, bioRxiv/medRxiv DOI/URL, local PDF path, or web URL the user gave. Use a
different `--base-dir` if the user wants output somewhere specific. Add
`--domain hep|ai|physics|bio|general` only if the user explicitly overrides the
domain. The last stdout line is the absolute path of `manifest.json`. Read it.

The script tries **arXiv e-print source (.tex + figures) first** — including for
local PDFs *and remote PDF URLs* that turn out to be arXiv papers — and falls
back to PDF/URL text extraction. The work folder it creates holds `source/`,
`figures/`, any `extracted.txt`, and the manifest. Everything you produce goes
in this folder.

`manifest.ingest_method` is one of `arxiv-source` (best), `arxiv-pdf`,
`local-pdf`, `url-pdf` (a URL that served a PDF), or `url`.

**Biomedical inputs** — a PubMed id (`PMID:31043725`, a pubmed URL, or a bare
number), a PMC id (`PMC6483969`), a DOI, or a bioRxiv/medRxiv DOI/URL — go
through the open **JATS full-text XML**, the biomedical analogue of the arXiv
source. Methods for these:

| `ingest_method` | Meaning |
|---|---|
| `pmc-package` | PMC open-access package: XML + original figures (best) |
| `pmc-xml` | Europe PMC full-text XML; figures downloaded by reference |
| `biorxiv-xml` / `medrxiv-xml` | Preprint JATS XML from the bioRxiv API |
| `biorxiv-pdf` / `medrxiv-pdf` | Preprint PDF (XML unavailable) |
| `pubmed-abstract` / `biorxiv-abstract` / `medrxiv-abstract` | **Abstract only** |

A local PDF whose DOI resolves to open JATS full text is also routed here, but
only when the resolved title matches the PDF (the user's PDF is kept in
`source/`). For JATS ingests, read `manifest.extracted_text` (`fulltext.md`);
references are in `references.txt` (`manifest.bibliography`), and
`figures_map` keys are the figure label (`"Figure 1"`), the XML id (`"F1"`), and
the graphic href. Extra fields: `doi`, `pmid`, `pmcid`, `journal`, `server`,
`published_doi`, `fulltext_links`.

**If the method ends in `-abstract`**, the paper is not open access. Tell the
user only the abstract was available and ask for the PDF before writing a full
summary. Write an abstract-only summary only if they ask for it, and label it
as such in the header line.

Fields worth knowing before you start reading:

| Field | Use |
|---|---|
| `content_chars` | Size of what you must read. See the fan-out rule in step 2. |
| `all_tex` | Every `.tex` in the source — the main file `\input`s these. |
| `bibliography` | `.bbl`/`.bib` paths. Resolve `\cite{}` keys here for related work. |
| `figures` | Collected figure paths, relative to the work folder. |
| `figures_map` | `\includegraphics` path → collected file. Use it; do not guess. |
| `warnings` | Anything that degraded the ingest. Surface these in step 8. |

**If ingest fails** (no `work_dir`, or `main_content` is null): report the
`manifest.warnings` to the user and stop — do not write a summary from nothing.

### 2. Read the paper (2-pass + grounding)

- **Pass 1 — skim:** read the main content for structure, claims, and
  contributions. For `arxiv-source`, the main file is `manifest.main_content`
  (a `.tex`); it usually `\input`s the files in `manifest.all_tex` — read those
  too. For every other method (PDF, URL, and the JATS `fulltext.md`), read
  `manifest.extracted_text`.
- **Pass 2 — deep:** go section by section and **ground every nontrivial claim
  in the source**, tagging it with a reference (see grounding rules below).
- **Related work:** resolve `\cite{}` keys against `manifest.bibliography` so
  you name the actual prior papers instead of paraphrasing the citation.
- **Long papers (incl. appendices):** when `manifest.content_chars` exceeds
  ~150,000, dispatch one subagent per major section to read and return
  structured notes, then synthesize. This preserves your context budget.

### 3. Build the background primer (배경지식)

Papers assume their field's background. Before writing the expert-level body,
write a **배경지식 (Background)** section that explains the established
knowledge the paper takes for granted, so a reader from an adjacent field (e.g.
a biochemist reading an immunology paper) can follow the argument.

1. **Pick the key entities.** From the Abstract, Introduction, and the Results
   that carry the main claim, list the actors the argument depends on:
   - bio: cell types (Treg, Th17), molecules / receptors / transcription
     factors (Nurr1/NR4A2, Foxp3, RORγt), drugs and compounds (chloroquine),
     signalling pathways, the disease and its models (EAE, CIA), and any
     non-standard technique the conclusion hinges on;
   - ai / physics / hep / general: the prior models, concepts, and methods the
     paper builds on (e.g. RNN, attention; a standard model or formalism).

   Keep it to the **4–8 entities** a reader must know to follow the main claim.
   Skip universal basics (DNA, PCR, gradient descent) unless the paper uses them
   in a non-standard way.
2. **For each entity, write 2–4 sentences on its *general* role**, independent
   of this paper:
   - **정체 (What it is)** — class, origin / where it is expressed, defining
     markers or properties.
   - **정상 기능 (Normal role)** — what it does in normal physiology (or, for
     methods, what problem it solves).
   - **질환·맥락에서의 역할 (Role in disease / context)** — its known role in the
     disease or setting the paper studies, if any.
   - **이 논문에서 (In this paper)** — one line on why it matters here, tagged to
     the paper (`(§1 Introduction)`, `(Fig. 2)`). This line links the background
     to the paper; it must not preview the paper's new results in detail.
3. **Close with how the pieces fit together** (개체 간 관계): one short paragraph
   on the established model before this paper (e.g. the Th17/Treg balance in
   autoimmunity, what was already known about Nurr1 in immune cells), ending
   with the open question this paper addresses — this leads straight into
   문제 정의 & 동기.

**Sourcing rules for the background (hard rules):**

- Background is textbook / review-level consensus, **not the paper's findings**.
  Never put the paper's new results here — they belong in 주요 결과.
- Prefer what the paper's own Introduction says, with its tag. When the
  Introduction cites a review or a foundational paper for a fact, name that
  reference from `manifest.bibliography` (e.g. "Kim et al., 2015 (ref. 23)").
- Anything taken from your own general knowledge rather than the paper gets the
  tag `(배경)` (or `(background)` in English). Write only well-established facts;
  if something is contested or you are unsure, say so instead of asserting it.
- **No numbers from memory** (frequencies, affinities, doses, prevalence) and
  **no invented citations.** If a number is essential, take it from the paper
  with its tag.
- If the PubMed tool is available and you are unsure of an entity's established
  role, you may check one review article and cite it; this is optional.

Example entry (shape only — for a paper on Nurr1 agonists such as chloroquine
in inflammatory autoimmune disease):

```
### Th17 세포 (T helper 17 cell) — CD4⁺ T 세포 아형
- **정체:** IL-17A/F를 분비하는 CD4⁺ helper T 세포로, 전사인자 RORγt가 분화를 결정한다. TGF-β와 IL-6에 의해 분화가 유도되고 IL-23이 유지에 관여한다. (배경)
- **정상 기능:** 세포외 세균·진균에 대한 점막 방어에서 호중구를 끌어들이는 염증 반응을 매개한다. (배경)
- **질환에서의 역할:** 과도하게 활성화되면 다발성 경화증(동물 모델 EAE), 류마티스 관절염, 건선 등 자가면역 염증을 유발하는 핵심 세포로 알려져 있다. (배경)
- **이 논문에서:** 약물 처리 후 Th17 분화 억제 여부가 핵심 판독 지표다. (§1 Introduction)
```

### 4. Apply the domain supplement

Read `<SKILL_DIR>/domains/<manifest.domain>.md` and follow it to fill the domain
supplement section. For `hep`, branch on `manifest.domain_subcategory` (hep-ex /
hep-ph / hep-th / hep-lat). For `bio`, branch on it too (`biorxiv:*` /
`medrxiv:*` preprint vs. journal; clinical vs. basic research).

### 5. Write `summary.md`

Write to `<work_dir>/summary.md` using the template below. The first line must be
`# <Paper Title> — 요약` (or `— Summary` in English) so the PDF picks up the title.

### 6. Verify the numbers

Trace every number in the summary back to the source:

```bash
uv run <SKILL_DIR>/scripts/verify.py "<work_dir>"
```

It prints each number it could not find in the ingested source, with the line
it appears on. For every one reported, do one of:

- **Fix it** — you mistyped or misread; correct it against the source.
- **Mark it** — it is genuinely your own derivation; tag the claim `(추론)` /
  `(해석)` so it is not presented as the paper's own number.
- **Keep it** — the checker is deliberately conservative and can miss a value
  that is split across a table cell or written in a different notation. Confirm
  it in the source yourself before deciding this.

Re-run until every remaining report is one you have consciously accepted. The
checker never blocks: it exits 0 regardless (use `--strict` to make it exit 1).

### 7. Render the PDF (best-effort)

Run the bundled Typst renderer on the summary:

```bash
<SKILL_DIR>/scripts/render_pdf.sh "<work_dir>/summary.md"
```

This goes `md → typ → pdf` (pandoc → typst), producing `summary.pdf` (and
`summary.typ`) in the work folder. No browser/MathJax needed: math is
Typst-native and Korean uses a CJK font fallback. Relative `figures/...` paths
resolve because the renderer sets Typst's root to the work folder.

**If rendering fails** (e.g. `pandoc` or `typst` not installed — run
`<SKILL_DIR>/scripts/setup.sh` to check what's missing): do NOT abort. Keep the completed
`summary.md`, tell the user the PDF was skipped and why, and that they can install
the missing tool and re-run. The Markdown is the primary deliverable; the PDF is
a convenience.

### 8. Report

Tell the user the work-folder path, the `summary.md` (and `summary.pdf` if
produced) paths, the ingest method and domain used, and surface any
`manifest.warnings` and whether the PDF step succeeded. If you accepted any
number that `verify.py` flagged, say which and why.

## Summary method (non-negotiable)

- **Faithful, expert-level, with intuition.** Assume a researcher reader. Skip
  textbook basics in the body; explain the *key* ideas and equations down to
  their intuition. The **배경지식** section (step 3) is the one deliberate
  exception: it covers the basics the paper assumes, so the body need not.
- **Reproduce key equations** in `$...$`/`$$...$$`, defining every symbol the
  first time it appears. Use Typst/pandoc-compatible LaTeX — write `\mathrm{...}`,
  not the bare `\rm`/`\bf` (the renderer normalizes common `\rm`→`\mathrm`, but
  other deprecated TeX may fail to convert and render as raw text).
- **Figures:** if `manifest.figures` is non-empty, pick the genuinely
  load-bearing figures (architecture, main result) and embed them with a relative
  path **and a width attribute** so they don't dominate the page, e.g.
  `![인코더-디코더 구조](figures/modalnet-21.png){ width=60% }`. The `{ width=NN% }`
  is REQUIRED — without it figures render uncomfortably large. The width attribute
  carries through pandoc into Typst (`image(..., width: 60%)`). Default to ~60%;
  use ~45% for simple/wide plots and up to ~75% only for dense multi-panel
  figures. To resolve `\includegraphics{Figures/ModalNet-21}`, look the path up
  in `manifest.figures_map` — do not guess the slugified filename. Do not dump
  every figure. **If `manifest.figures` is empty, skip figures entirely** — do
  not invent or hunt for them.
  **Do not start the alt text with "Figure 1:"** — the renderer already numbers
  captions, so you would get "그림 1: Figure 1: ...".
- **Markdown hygiene for clean PDF:** put a blank line before every list and
  table — a bullet list placed right after a `:` line gets flattened into one
  paragraph by the renderer. Avoid a `## ` heading immediately after a `---`.
- **Anti-hallucination (hard rules):**
  - Copy numbers and quotes **verbatim** from the source. Never invent or round
    silently. If a value is unclear, say so.
  - Mark anything not stated by the paper — your inference or reading — with
    `(추론)` / `(해석)` (or `(inferred)` / `(interpretation)`). General
    background knowledge from outside the paper is marked `(배경)` /
    `(background)`.
  - Put a reference tag on each key claim. Prefer rendered numbers when the
    source has them: `(§4.1, Table 3, Fig. 5)`. **For `arxiv-source` ingests the
    `.tex` usually has only `\label{}` names, not rendered numbers** — then tag
    with the section number plus the label or a short locator, e.g.
    `(§4, eq. "punch")`, and never fabricate a number.
  - Keep ambiguous technical terms in the original language alongside any
    translation.
- **Language:** match the user's request. Korean request → Korean body with
  English term annotation, e.g. 어텐션(attention). English request → all English
  (translate the template headings). No clear signal → follow the conversation
  language.

## Output template

Headings below show `한국어 (English)` — keep both for a Korean summary's term
annotation, or use the English side alone for an English summary.

```
# <Title> — 요약

> 저자 · 출처(arXiv id / 저널 · DOI · PMID / 프리프린트 여부, 링크) · 연도 · 도메인

## TL;DR
2–3문장: 무엇을, 왜, 핵심 결과.

## 배경지식 (Background)
이 논문을 따라가는 데 필요한 기존 지식 (4–8개 핵심 개체). 논문의 새 결과는 넣지 않는다.

### <개체명> (<English / 약어>) — <분류: 세포 / 분자 / 약물 / 질환 모델 / 기법 …>
- **정체:** …
- **정상 기능:** …
- **질환·맥락에서의 역할:** …
- **이 논문에서:** … (§ 태그)

### 개체 간 관계 (How they fit together)
이 논문 이전의 기존 모델과, 남아 있던 질문 → 문제 정의로 연결.

## 문제 정의 & 동기 (Problem & Motivation)
## 기존 연구와의 차이 (Related Work & Gap)
## 핵심 기여 (Key Contributions)
- bullet 형태

## 방법론 (Method)
핵심 수식 + 기호 정의. 필요한 곳에 직관.

## 실험 설정 & 데이터셋 (Experiments & Data)
## 주요 결과 (Results)
표/수치는 원문 그대로.

## 도메인 보충 (Domain Supplement): <domain/subcategory>
domains/<domain>.md 의 체크리스트로 작성.

## 한계 & 향후 과제 (Limitations & Future Work)
저자가 말한 한계 + (해석) 표시한 추가 관찰.

## 비판적 검토 (Critical Review)
중립 요약과 분리된 평가: 강점 · 약점 · 신규성 · 주의점.

## 핵심 용어 정리 (Glossary)
배경지식에서 다루지 않은 나머지 용어만 짧게.

| 용어 | 뜻 |
```

## When NOT to use

- The user wants a one-line answer or a quick question about a paper, not a
  written summary → just answer.
- The user wants you to *implement* a paper's method → that's a coding task.

## Common mistakes

- **Re-downloading by hand.** Always go through `ingest.py`; it handles arXiv
  source, the PDF→arXiv shortcut, and web extraction.
- **Reading only the main `.tex`.** It `\input`s other files — read
  `manifest.all_tex` too, or the body will be missing.
- **Embedding figures by absolute path, embedding all of them, embedding them at
  full page width (always add `{ width=~60% }`), or hunting for figures when
  `manifest.figures` is empty.**
- **Guessing a figure's filename** instead of looking it up in
  `manifest.figures_map`.
- **Inventing numbers or fabricating reference numbers** the source doesn't
  render. Trace every metric to the source with an honest tag.
- **Skipping `verify.py`, or silencing it by deleting the number** instead of
  correcting it or marking it `(추론)`.
- **Aborting when PDF rendering fails.** Keep `summary.md` and report — the
  Markdown is the deliverable.
- **Writing the summary outside the work folder.** Keep all artifacts together.
- **Leaking results into the background.** 배경지식 explains what was known
  *before* the paper; the paper's own findings go in 주요 결과.
- **Untagged or invented background.** Facts from outside the paper carry
  `(배경)`; no numbers from memory and no made-up citations. Pick the entities
  the main claim depends on, not every term in the paper.
```
