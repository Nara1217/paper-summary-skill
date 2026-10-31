"""Tests for the grounding checker.

The checker is advisory: it exists to catch invented or mistyped numbers before
the summary is handed over, so false positives are far more costly than misses.
Everything the skill legitimately writes that is NOT a quote from the paper —
reference tags, figure paths, layout attributes, inference-marked claims — must
stay out of the report.

Run with:  ./tests/run.sh
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import verify  # noqa: E402


def nums(md):
    return [n.text for n in verify.extract_numbers(md)]


def test_reports_a_number_absent_from_the_source():
    found = verify.find_ungrounded(
        verify.extract_numbers("BLEU 는 41.8 을 기록했다."),
        "we report a BLEU score of 28.4",
    )
    assert [n.text for n in found] == ["41.8"]


def test_accepts_a_number_present_in_the_source():
    found = verify.find_ungrounded(
        verify.extract_numbers("BLEU 는 28.4 를 기록했다."),
        "we report a BLEU score of 28.4",
    )
    assert found == []


def test_thousands_separators_in_the_summary_still_match():
    """The paper writes 213000; a Korean summary may write 213,000."""
    found = verify.find_ungrounded(
        verify.extract_numbers("총 213,000 스텝을 학습했다."),
        "trained for 213000 steps",
    )
    assert found == []


def test_thousands_separators_in_the_source_still_match():
    """The paper writes 213,000; the summary may drop the separator."""
    found = verify.find_ungrounded(
        verify.extract_numbers("총 213000 스텝을 학습했다."),
        "trained for 213,000 steps",
    )
    assert found == []


def test_figure_width_attributes_are_not_claims():
    assert nums("![Fig](figures/x.png){ width=60% }") == []


def test_image_and_link_paths_are_not_claims():
    """figures/modalnet-21.png must not be read as the number 21."""
    assert nums("![arch](figures/modalnet-21.png)") == []
    assert nums("[논문](https://arxiv.org/abs/1706.03762)") == []


def test_reference_tags_are_not_claims():
    """Locators point AT the source; they are not quoted values.

    For arxiv-source ingests the .tex has no rendered numbers at all, so these
    would otherwise be flagged on every single summary.
    """
    assert nums("정확도가 향상되었다 (§4.1, Table 3, Fig. 5)") == []
    assert nums("성능이 올랐다 (§2, 표 1, 그림 2, 식 3)") == []


def test_metadata_header_line_is_not_checked():
    """The template's `> 저자 · 출처 · 연도 · 도메인` line is provenance.

    It is mandatory in every summary, so treating it as claims made the checker
    fire a false alarm on literally every run.
    """
    assert nums("> Vaswani et al. · arXiv:1706.03762 · 2017 · ai") == []


def test_arxiv_ids_are_not_claims():
    """Identifiers carry digits but assert nothing about the paper."""
    assert nums("원 논문 hep-ex/0012045 의 후속 연구다.") == []
    assert nums("arXiv:2005.14165 에서 제안되었다.") == []
    assert nums("2301.12345v3 을 참조했다.") == []


def test_ordered_list_markers_are_not_claims():
    assert nums("1. 첫 번째 기여\n2. 두 번째 기여") == []


def test_inference_marked_lines_are_skipped():
    """(추론)/(해석) declare the number as the model's own, not the paper's."""
    assert nums("약 3배 빠르다 (추론)") == []
    assert nums("roughly 3x faster (inferred)") == []
    assert nums("이 값은 2배 수준이다 (해석)") == []


def test_background_marked_lines_are_skipped():
    """(배경) marks general background knowledge, not a claim from the paper."""
    assert nums("Th17 세포는 IL-17을 분비한다. (배경)") == []
    assert nums("Th17 cells secrete IL-17. (background)") == []


def test_a_real_claim_on_an_inference_free_line_is_still_extracted():
    """Guard against the skip rules swallowing everything."""
    assert nums("정확도는 88.7% 였다.") == ["88.7"]


def test_report_lists_line_numbers(tmp_path):
    md = "# 제목\n\n첫 줄\n\n값은 99.9 이다.\n"
    found = verify.find_ungrounded(verify.extract_numbers(md), "unrelated source")
    assert len(found) == 1
    assert found[0].line == 5
