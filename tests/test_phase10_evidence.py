"""Phase 10: benchmark scoring, observed correction effort, report rendering."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from bcp_project.evaluation import metrics as M
from bcp_project.evaluation.effort import correction_effort

ROOT = Path(__file__).resolve().parents[1]


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --- text metrics ------------------------------------------------------------------------------


def test_cer_and_wer_english():
    stats = M.cer("The Board approved", "The Bord approved")
    assert stats.distance == 1 and stats.deletions == 1
    assert stats.rate == pytest.approx(1 / len("the board approved"))
    words = M.wer("The Board approved the tender.", "the board approved a tender")
    assert words.distance == 1 and words.substitutions == 1
    assert words.reference_length == 5


def test_normalisation_ignores_case_whitespace_and_zero_width():
    assert M.cer("Hello   World", "hello world").distance == 0
    assert M.cer("ক্\u200dষ", "ক্ষ").distance == 0


def test_bangla_wer_keeps_vowel_signs_when_stripping_punctuation():
    ref = "পরিচালনা পর্ষদ অনুমোদন দিয়েছে।"
    assert M.normalize(ref, strip_punct=True) == "পরিচালনা পর্ষদ অনুমোদন দিয়েছে"
    assert M.wer(ref, "পরিচালনা পর্ষদ অনুমোদন দিয়েছে").distance == 0
    assert M.wer(ref, "পরিচালনা পর্ষদ অনুমোদন দিযেছে").distance == 1


def test_bangla_grapheme_cer_counts_a_conjunct_once():
    ref, hyp = "ক্ষমতা", "কমতা"
    by_codepoint = M.cer(ref, hyp)
    by_cluster = M.cer(ref, hyp, clusters=True)
    assert by_codepoint.distance == 2
    assert by_cluster.distance == 1
    assert len(M.graphemes("ক্ষমতা")) < len("ক্ষমতা")


def test_pooled_rate_weights_by_reference_length():
    long_ok = M.edit_stats("a" * 99, "a" * 99)
    short_bad = M.edit_stats("b", "c")
    assert M.pooled_rate([long_ok, short_bad]) == pytest.approx(0.01)


def test_empty_reference_rate():
    assert M.cer("", "").rate == 0.0
    assert M.cer("", "x").rate is None


def test_cpwer_counts_words_given_to_the_wrong_speaker():
    reference = {"Chair": "the board approves the policy", "CFO": "finance will circulate the report"}
    perfect, mapping = M.cpwer(reference, {"S1": "finance will circulate the report", "S0": "the board approves the policy"})
    assert perfect.distance == 0 and mapping == {"Chair": "S0", "CFO": "S1"}

    # Same words, but "the policy" landed on the other speaker: plain WER would be 0.
    swapped, _ = M.cpwer(reference, {"S0": "the board approves", "S1": "the policy finance will circulate the report"})
    assert swapped.distance == 4 and swapped.reference_length == 10

    one_speaker, mapping = M.cpwer(reference, {"S0": "the board approves the policy finance will circulate the report"})
    # The CFO's five words are insertions on the Chair and deletions for the CFO.
    assert mapping["CFO"] is None and one_speaker.distance == 10

    extra, _ = M.cpwer({"Chair": "board approves"}, {"S0": "board approves", "S1": "noise words"})
    assert extra.distance == 2 and extra.insertions == 2


def test_diarization_status_reports_configuration_without_loading_models(monkeypatch):
    from bcp_project.diarization import diarization_status

    for name in ("DIARIZATION_PROVIDER", "DIARIZATION_MODEL", "DIARIZATION_URL", "HF_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert diarization_status() == {"provider": "none", "enabled": False, "detail": None, "ready": False, "issues": []}

    monkeypatch.setenv("DIARIZATION_PROVIDER", "pyannote")
    status = diarization_status()
    assert status["enabled"] and not status["ready"] and "HF_TOKEN" in status["issues"][0]
    monkeypatch.setenv("DIARIZATION_MODEL", "/srv/models/pyannote/config.yaml")
    assert diarization_status()["ready"] is True

    monkeypatch.setenv("DIARIZATION_PROVIDER", "http")
    assert diarization_status()["issues"] == ["DIARIZATION_URL is not set"]
    monkeypatch.setenv("DIARIZATION_URL", "http://diarize.internal/diarize")
    assert diarization_status()["ready"] is True


def test_language_of_and_digit_folding():
    assert M.language_of("বোর্ড সভা ২০২৬") == "bn"
    assert M.language_of("Board meeting 2026") == "en"
    assert M.normalize("২০২৬", ascii_digits=True) == "2026"


def test_correction_minutes_estimate():
    assert M.correction_minutes(0) == pytest.approx(0.33, abs=0.01)
    assert M.correction_minutes(60, edits_per_minute=30, seconds_per_page=0) == 2.0


# --- classification and fields -------------------------------------------------------------------


def test_classification_report():
    report = M.classification_report(["Minutes", "Minutes", "Circular", "Policy"], ["Minutes", "Circular", "Circular", "Policy"])
    assert report["accuracy"] == 0.75
    assert report["per_class"]["Minutes"]["recall"] == 0.5
    assert report["per_class"]["Circular"]["precision"] == 0.5
    assert report["confusion"]["Minutes"] == {"Minutes": 1, "Circular": 1}
    assert 0 < report["macro_f1"] < 1


def test_field_score_dates_references_and_lists():
    assert M.field_score("2026-01-14", "14/01/2026", field="doc_date")["exact"]
    assert M.field_score("2026-01-14", "১৪-০১-২০২৬", field="doc_date")["exact"]
    assert M.field_score("SBL/BA/2026/14", "sbl / ba / 2026 / 14", field="reference_number")["exact"]
    near = M.field_score("Board Meeting Minutes - ICT Procurement", "Board Meeting Minutes – ICT Procurement")
    assert not near["exact"] and near["fuzzy"]
    assert M.field_score(["board", "tender"], ["Board", "tender", "ict"])["similarity"] == 1.0
    assert M.field_score("", None) == {"exact": True, "fuzzy": True, "similarity": 1.0}


# --- summaries, retrieval, latency ----------------------------------------------------------------


def test_rouge_l_and_facts():
    score = M.rouge_l("the board approved the budget", "board approved the annual budget")
    assert score["recall"] == pytest.approx(4 / 5)
    assert M.contains_fact("অনুমোদিত বাজেট ১২ কোটি টাকা।", "12 কোটি")
    assert not M.contains_fact("approved", "rejected")


def test_retrieval_metrics():
    assert M.set_precision_recall(["a", "b"], ["a", "c"]) == {"precision": 0.5, "recall": 0.5}
    assert M.set_precision_recall([], ["a"])["precision"] is None
    assert M.ndcg_at_k(["a", "x"], ["a"]) == 1.0
    assert M.ndcg_at_k(["x", "a"], ["a"]) < 1.0


def test_latency_stats():
    stats = M.latency_stats([10, 20, 30, 40, 1000])
    assert stats["n"] == 5 and stats["p50"] == 30 and stats["p95"] == 1000 and stats["max"] == 1000
    assert M.latency_stats([])["p50"] is None


# --- observed correction effort ------------------------------------------------------------------


def _row(i, action, resource, detail, at, user="maker"):
    return {"id": i, "username": user, "action": action, "resource_id": resource, "detail": detail, "created_at": at}


def test_correction_effort_from_audit_rows():
    t0 = datetime(2026, 10, 1, 9, 0)
    rows = [
        _row(1, "verify_open", "D1", "v1", t0),
        _row(2, "verify_approve", "D1", "v1 pages_edited=2", t0 + timedelta(minutes=6)),
        _row(3, "metadata_confirm", "D1", "changed=title,doc_date", t0 + timedelta(minutes=6)),
        _row(4, "verify_open", "D2", "v3", t0 + timedelta(minutes=10), user="checker"),
        _row(5, "verify_reject", "D2", "v3 blurred scan", t0 + timedelta(minutes=12), user="checker"),
        _row(6, "verify_open", "D3", "v1", t0),
        _row(7, "verify_approve", "D3", "v1 pages_edited=0", t0 + timedelta(hours=5)),
        _row(8, "metadata_confirm", "D3", "changed=none", t0 + timedelta(hours=5)),
        _row(9, "minutes_save", "M1", "chars=10 changed=True", t0),
        _row(10, "minutes_save", "M1", "chars=12 changed=True", t0 + timedelta(hours=1)),
        _row(11, "minutes_approve", "M1", "", t0 + timedelta(hours=3)),
        _row(12, "briefing_generate", "D9", "version=1", t0),
        _row(13, "briefing_edit", "D9", "version=1 heading=Risks", t0 + timedelta(minutes=30)),
        _row(14, "briefing_approve", "D9", "version=1 sha256=x", t0 + timedelta(hours=2)),
    ]
    effort = correction_effort(rows)
    ocr = effort["ocr_verification"]
    assert ocr["approved"] == 2 and ocr["rejected"] == 1
    assert ocr["minutes_per_document"]["n"] == 2
    assert ocr["minutes_per_document"]["median"] == 4.0
    assert ocr["sessions_over_cap_or_unpaired"] == 1
    assert ocr["metadata_fields_corrected"] == {"title": 1, "doc_date": 1}
    assert ocr["pages_edited_per_document"]["mean"] == 1.0
    assert effort["minutes"]["approved"] == 1
    assert effort["minutes"]["saves_before_approval"]["median"] == 2
    assert effort["minutes"]["hours_to_approval"]["median"] == 3.0
    assert effort["briefings"]["section_edits_before_approval"]["median"] == 1
    assert effort["briefings"]["hours_to_approval"]["median"] == 2.0


def test_verify_pairs_need_the_same_reviewer_and_version():
    t0 = datetime(2026, 10, 1, 9, 0)
    effort = correction_effort(
        [
            _row(1, "verify_open", "D1", "v1", t0, user="a"),
            _row(2, "verify_approve", "D1", "v1 pages_edited=0", t0 + timedelta(minutes=3), user="b"),
            _row(3, "verify_open", "D1", "v2", t0, user="a"),
            _row(4, "verify_approve", "D1", "v1 pages_edited=0", t0 + timedelta(minutes=3), user="a"),
        ]
    )
    assert effort["ocr_verification"]["minutes_per_document"]["n"] == 1


# --- OCR: lines the layout analysis skipped ----------------------------------------------------------


def _page_with_two_lines():
    from PIL import Image, ImageDraw

    img = Image.new("L", (1200, 500), 255)
    draw = ImageDraw.Draw(img)
    # Text-like strokes (about a third ink), not solid blocks.
    for top, right in ((100, 900), (250, 1000)):
        for x in range(100, right, 6):
            draw.rectangle((x, top, x + 1, top + 40), fill=0)
    img.info["dpi"] = (300, 300)
    return img


def _tess(words):
    keys = ("text", "conf", "block_num", "par_num", "line_num", "left", "top", "width", "height")
    return {k: [w[i] for w in words] for i, k in enumerate(keys)}


def test_uncovered_bands_find_a_skipped_line():
    import numpy as np

    from bcp_project import ocr

    ink = np.zeros((500, 1200), dtype=bool)
    ink[100:141, 100:901] = True
    ink[250:291, 100:1001] = True
    words = [{"text": "first", "conf": 95.0, "line": (1, 1, 1), "box": (100, 100, 800, 41)}]
    bands, line_h = ocr._uncovered_bands(ink, words)
    assert line_h == 41
    assert len(bands) == 1
    top, bottom, left, right = bands[0]
    assert 245 <= top <= 250 and 290 <= bottom <= 296 and left == 100 and right == 1001


def test_ocr_recovers_a_skipped_line_in_reading_order(monkeypatch):
    import pytesseract

    from bcp_project import ocr

    calls = []

    def fake(image, lang=None, config="", output_type=None):
        calls.append(config)
        if not config:
            return _tess([("প্রথম", 96, 1, 1, 1, 100, 100, 800, 41)])
        return _tess([("দ্বিতীয়", 88, 1, 1, 1, 20, 20, 900, 41)])

    monkeypatch.setattr(pytesseract, "image_to_data", fake)
    monkeypatch.setenv("OCR_DESKEW", "0")
    result = ocr.ocr_image_detail(_page_with_two_lines(), "ben")
    assert result.text == "প্রথম\nদ্বিতীয়"
    assert result.recovered_lines == 1
    assert result.ink_coverage == 1.0
    assert calls[1] == "--psm 13"


def test_unrecoverable_text_caps_the_page_confidence(monkeypatch):
    import pytesseract

    from bcp_project import ocr

    def fake(image, lang=None, config="", output_type=None):
        if not config:
            return _tess([("first", 97, 1, 1, 1, 100, 100, 800, 41)])
        return _tess([])

    monkeypatch.setattr(pytesseract, "image_to_data", fake)
    monkeypatch.setenv("OCR_DESKEW", "0")
    result = ocr.ocr_image_detail(_page_with_two_lines(), "eng")
    assert result.recovered_lines == 0
    assert result.ink_coverage < 0.6
    assert result.confidence == round(result.ink_coverage * 100, 2)
    _, page_threshold = ocr.review_thresholds()
    assert result.confidence < page_threshold

    monkeypatch.setenv("OCR_RECOVER_LINES", "0")
    assert ocr.ocr_image_detail(_page_with_two_lines(), "eng").confidence == 97.0


def test_bangla_pages_can_have_their_own_review_bar(monkeypatch):
    from bcp_project import ocr

    monkeypatch.delenv("OCR_BANGLA_PAGE_REVIEW_THRESHOLD", raising=False)
    monkeypatch.setenv("OCR_REVIEW_THRESHOLD", "75")
    monkeypatch.setenv("OCR_PAGE_REVIEW_THRESHOLD", "60")
    pages = [ocr.PageText(1, "পরিচালনা পর্ষদ বাজেট অনুমোদন করেছে", 89.0, "ocr"), ocr.PageText(2, "Board approved the budget", 89.0, "ocr")]
    assert ocr.summarize_confidence(pages)["needs_review"] is False
    monkeypatch.setenv("OCR_BANGLA_PAGE_REVIEW_THRESHOLD", "92")
    assert ocr.summarize_confidence(pages)["needs_review"] is True
    assert ocr.summarize_confidence([pages[1]])["needs_review"] is False


BIJOY_PAGE = "‡mvbvjx e¨vsK wcGjwm cwiPvjbv cl©‡`i mfvq ev‡RU Aby‡gv`b Kiv nq| " * 3


def test_legacy_bangla_text_layer_is_detected():
    from bcp_project import ocr

    assert ocr.legacy_bangla_text(BIJOY_PAGE)
    assert not ocr.legacy_bangla_text("সোনালী ব্যাংক পিএলসি পরিচালনা পর্ষদের সভায় বাজেট অনুমোদন")
    assert not ocr.legacy_bangla_text("The Board approved the budget at 25°C ± 2, see § 12 and § 14(b)® " * 4)
    assert not ocr.legacy_bangla_text("Revenue grew 4%.† Footnote one.‡ " + "Plain English text continues here. " * 20)


def test_legacy_bangla_pages_are_ocred_instead_of_indexed_as_text(monkeypatch):
    from bcp_project import ocr

    monkeypatch.setattr(ocr, "_pdf_text_layer", lambda data: [BIJOY_PAGE, "Board approved the budget for the year."])
    monkeypatch.setattr(ocr, "_render_pdf_page", lambda data, number, dpi=300: object())
    monkeypatch.setattr(ocr, "ocr_image_detail", lambda image, lang: ocr.OcrResult("সোনালী ব্যাংক পিএলসি", 91.0))
    pages = ocr.extract_pdf(b"%PDF")
    assert [p.source for p in pages] == ["ocr", "text"]
    assert pages[0].text == "সোনালী ব্যাংক পিএলসি"
    assert pages[0].metadata["text_layer"] == "legacy_bangla"

    monkeypatch.setenv("OCR_DETECT_LEGACY_BANGLA", "0")
    assert [p.source for p in ocr.extract_pdf(b"%PDF")] == ["text", "text"]


# --- bench_accuracy report and compare -------------------------------------------------------------


@pytest.fixture(scope="module")
def bench():
    return _script("bench_accuracy")


def _ocr_report():
    group = {
        "pages": 2,
        "cer": 0.012,
        "cer_graphemes": 0.02,
        "wer": 0.05,
        "mean_confidence": 90.5,
        "latency_ms": {"n": 2, "p50": 1500.0, "p95": 1800.0, "mean": 1650.0, "max": 1800.0},
        "correction_minutes_est_per_page": 0.4,
        "review_routing": {"high_error_pages": 1, "high_error_caught": 1, "flagged_pages": 1},
    }
    return {
        "kind": "ocr",
        "generated_at": "2026-10-12T00:00:00Z",
        "environment": {"commit": "abc123", "host": "ci"},
        "summary": {"all": group, "by_lang": {"bn": group}},
        "by_quality": {"scan": group},
        "failures": [{"id": "page-7", "cer": 0.2}],
    }


def test_markdown_ocr_table(bench):
    text = bench.markdown(_ocr_report())
    assert "| bn | 2 | 1.2% | 2.0% | 5.0% | 90.50 | 1,800.00 | 0.40 | 1/1 |" in text
    assert "| scan | 2 | 1.2% | 5.0% |" in text
    assert "`page-7`" in text


def test_markdown_retrieval_from_eval_retrieval_output(bench):
    report = {
        "kind": "retrieval",
        "overall": {"n": 4, "recall_at_10": 1.0, "mrr": 0.875, "grounding": 1.0, "page_accuracy": 0.75},
        "en": {"n": 2, "recall_at_10": 1.0, "mrr": 1.0, "grounding": 1.0, "page_accuracy": 1.0},
        "bn": {"n": 0},
        "acl_leaks": [],
        "filter_violations": [],
        "latency_ms": {"p50": 3.1, "p95": 5.0},
    }
    text = bench.markdown(report)
    assert "| overall | 4 | 100.0% | 0.88 | 100.0% | 75.0% |" in text
    assert "| bn |" not in text
    assert "Restricted-document leaks: 0" in text


def test_compare_reports(bench, tmp_path, capsys):
    a, b = _ocr_report(), _ocr_report()
    b["summary"]["all"] = {**b["summary"]["all"], "cer": 0.008}
    pa, pb = tmp_path / "a.json", tmp_path / "b.json"
    pa.write_text(json.dumps(a))
    pb.write_text(json.dumps(b))
    bench.run_compare(SimpleNamespace(baseline=pa, candidate=pb, baseline_label="cloud", candidate_label="vllm", out=None))
    out = capsys.readouterr().out
    assert "| all.cer | 0.012 | 0.008 | -0.004 |" in out


def test_local_ai_check_against_mock_servers(monkeypatch):
    import httpx

    check = _script("local_ai_check")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen2.5-14b-instruct"}]})
        if path.endswith("/chat/completions"):
            content = json.dumps({"summary_bn": "পর্ষদ বাজেট অনুমোদন করেছে", "summary_en": "Budget approved"}, ensure_ascii=False)
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": {"completion_tokens": 40}})
        if path.endswith("/embeddings"):
            vectors = [[1.0, 0.1, 0.0, 0.0], [0.9, 0.2, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]]
            return httpx.Response(200, json={"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]})
        if path.endswith("/rerank"):
            return httpx.Response(200, json=[{"index": 1, "score": 0.97}, {"index": 0, "score": 0.02}, {"index": 2, "score": 0.01}])
        if path.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={"text": ""})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    chat = check.chat_checks(client, "http://ai/llm/v1", "qwen2.5-14b-instruct")
    assert chat["ok"] and chat["bangla_answer"] and chat["tokens_per_s"]
    assert not check.chat_checks(client, "http://ai/llm/v1", "other-model")["ok"]
    embed = check.embed_checks(client, "http://ai/embed/v1", "bge-m3", 4)
    assert embed["ok"] and embed["cosine_en_bn_same_meaning"] > embed["cosine_en_unrelated"]
    assert not check.embed_checks(client, "http://ai/embed/v1", "bge-m3", 1024)["ok"]
    assert check.rerank_checks(client, "http://ai/rerank")["ok"]
    assert check.stt_checks(client, "http://ai/stt/v1", "whisper", None)["ok"]

    monkeypatch.setenv("AI_EGRESS", "internal_only")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert check.policy_checks()["ok"]
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-real")
    assert not check.policy_checks()["ok"]


def test_manifest_paths_are_relative_to_the_manifest(bench, tmp_path):
    (tmp_path / "ref.txt").write_text("সভা", encoding="utf-8")
    manifest = tmp_path / "ocr.jsonl"
    manifest.write_text('# comment\n{"file": "p.pdf", "reference_file": "ref.txt"}\n', encoding="utf-8")
    [row] = bench.load_manifest(manifest)
    assert row["id"] == "ocr-2"
    assert bench.resolve(row, "file") == tmp_path / "p.pdf"
    assert bench.reference_text(row) == "সভা"
