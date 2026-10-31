#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Grounding checker for the paper-summary skill.

The skill's anti-hallucination rules say every number must be copied verbatim
from the paper. This traces each number in summary.md back to the ingested
source and reports the ones it cannot find, so an invented or mistyped value is
caught before the summary is handed over.

It is ADVISORY, not a gate: a miss is cheap, a false alarm wastes the reader's
attention. Anything the skill legitimately writes that is not a quote — the
reference tags, figure paths, layout attributes, and (추론)/(해석)/(배경) claims — is
excluded by design.

Usage:
    uv run verify.py <work_dir | manifest.json> [--summary summary.md] [--strict]

Exits 0 unless --strict is given and something is ungrounded.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# 1,234.5 / 28.4 / 2017 — thousands separators optional.
NUMBER_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")

# Spans that contain digits but assert nothing about the paper.
NOISE_PATTERNS = [
    re.compile(r"!?\[[^\]]*\]\([^)]*\)"),        # markdown links and images
    re.compile(r"(?:arXiv:)?\d{4}\.\d{4,5}(?:v\d+)?"),          # arXiv id, new style
    re.compile(r"[a-z][a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?"),  # arXiv id, old style
    re.compile(r"\{[^}]*\}"),                    # pandoc attrs, e.g. { width=60% }
    re.compile(r"`[^`]*`"),                      # inline code
    re.compile(r"^\s{0,3}#{1,6}\s.*$", re.M),    # headings
    re.compile(r"^\s*\d+[.)]\s", re.M),          # ordered-list markers
    re.compile(r"^\s*\|?[\s|:-]+\|[\s|:-]*$", re.M),  # table rules
]

# Locators point AT the source rather than quoting it. arxiv-source ingests have
# no rendered numbers at all, so these would otherwise fire on every summary.
LOCATOR_RE = re.compile(
    r"§\s*[\d.]+"
    r"|\b(?:Table|Tab\.|Figure|Fig\.?|Eq\.?|Equation|Sec\.?|Section|Appendix|App\.?)"
    r"\s*[A-Z]?\d*(?:\.\d+)*"
    r"|(?:표|그림|식|절|장|부록)\s*\d+(?:\.\d+)*",
    re.IGNORECASE,
)

# A line carrying one of these declares its numbers as the model's own reading.
INFERENCE_RE = re.compile(r"\(\s*(?:추론|해석|배경|inferred|interpretation|background)\s*\)", re.IGNORECASE)


@dataclass(frozen=True)
class Number:
    text: str
    line: int
    context: str

    @property
    def normalized(self) -> str:
        return self.text.replace(",", "")


def _blank(match: re.Match) -> str:
    """Replace a span with same-length whitespace, preserving line offsets."""
    return re.sub(r"[^\n]", " ", match.group(0))


def scrub(line: str) -> str:
    """Remove spans whose digits are not claims about the paper."""
    for pat in NOISE_PATTERNS:
        line = pat.sub(_blank, line)
    return LOCATOR_RE.sub(_blank, line)


def extract_numbers(markdown: str) -> list[Number]:
    """Every number in the summary that asserts a value taken from the paper."""
    out: list[Number] = []
    for i, raw in enumerate(markdown.splitlines(), start=1):
        # The template's `> 저자 · 출처 · 연도 · 도메인` header states provenance,
        # not findings, and it is mandatory — checking it misfires every run.
        if raw.lstrip().startswith(">"):
            continue
        if INFERENCE_RE.search(raw):
            continue
        cleaned = scrub(raw)
        for m in NUMBER_RE.finditer(cleaned):
            out.append(Number(text=m.group(0), line=i, context=raw.strip()))
    return out


def find_ungrounded(numbers: list[Number], corpus: str) -> list[Number]:
    """Numbers that do not appear anywhere in the source text."""
    haystack = corpus.replace(",", "")
    return [n for n in numbers if n.normalized not in haystack]


def load_corpus(manifest: dict, work_dir: Path) -> str:
    """Every source file the summary could legitimately quote from."""
    rel_paths = list(manifest.get("all_tex") or [])
    rel_paths += list(manifest.get("bibliography") or [])
    if manifest.get("extracted_text"):
        rel_paths.append(manifest["extracted_text"])

    parts = [manifest.get("abstract") or "", manifest.get("title") or ""]
    for rel in rel_paths:
        try:
            parts.append((work_dir / rel).read_text(errors="ignore"))
        except OSError:
            continue
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="paper-summary grounding checker")
    ap.add_argument("target", help="work folder, or the path to its manifest.json")
    ap.add_argument("--summary", default="summary.md",
                    help="summary filename inside the work folder")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 when something is ungrounded")
    args = ap.parse_args()

    target = Path(args.target).expanduser().resolve()
    manifest_path = target if target.is_file() else target / "manifest.json"
    if not manifest_path.is_file():
        print(f"verify: no manifest at {manifest_path}", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text())
    work_dir = Path(manifest.get("work_dir") or manifest_path.parent)

    summary_path = work_dir / args.summary
    if not summary_path.is_file():
        print(f"verify: no summary at {summary_path}", file=sys.stderr)
        return 2

    numbers = extract_numbers(summary_path.read_text())
    corpus = load_corpus(manifest, work_dir)
    if not corpus.strip():
        print("verify: source corpus is empty — nothing to check against", file=sys.stderr)
        return 2

    ungrounded = find_ungrounded(numbers, corpus)
    print(f"verify: {len(numbers)} number(s) checked, {len(ungrounded)} not found in source")
    if not ungrounded:
        return 0

    print()
    for n in ungrounded:
        ctx = n.context if len(n.context) <= 90 else n.context[:87] + "..."
        print(f"  {summary_path.name}:{n.line}  {n.text:>12}  |  {ctx}")
    print()
    print("Each number above does not appear in the source. Correct it against the")
    print("paper, or mark the claim (추론)/(해석) if it is your own derivation.")
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
