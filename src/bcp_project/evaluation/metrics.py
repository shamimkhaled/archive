"""Scoring functions shared by ``scripts/bench_accuracy.py``.

Text is compared after the same normalisation for reference and hypothesis: Unicode NFC, zero-width
joiners removed, whitespace collapsed, and case folded. Bangla CER is reported over code points (the
figure most OCR papers quote) and over grapheme clusters (what a person correcting the text sees:
a conjunct such as ক্ষ counts as one character).
"""

from __future__ import annotations

import math
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from rapidfuzz.distance import Levenshtein as _RF
except ImportError:  # pragma: no cover - exercised only without rapidfuzz
    _RF = None

try:
    import regex as _regex
except ImportError:  # pragma: no cover
    _regex = None

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))
_SPACE = re.compile(r"\s+")
_PUNCT = _regex.compile(r"[\p{P}\p{S}]") if _regex else None
_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
_BN_RANGE = re.compile(r"[\u0980-\u09FF]")


def _strip_punct(text: str) -> str:
    # \w-based patterns would drop Bangla vowel signs (category Mc/Mn), so strip by category.
    if _PUNCT is not None:
        return _PUNCT.sub(" ", text)
    return "".join(" " if unicodedata.category(c)[0] in "PS" else c for c in text)


def normalize(text: Optional[str], *, strip_punct: bool = False, ascii_digits: bool = False) -> str:
    out = unicodedata.normalize("NFC", text or "").translate(_ZERO_WIDTH)
    if ascii_digits:
        out = out.translate(_BN_DIGITS)
    if strip_punct:
        out = _strip_punct(out)
    return _SPACE.sub(" ", out).strip().casefold()


def language_of(text: str) -> str:
    """'bn' when most letters are Bengali script, else 'en'."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "en"
    bengali = sum(1 for c in letters if _BN_RANGE.match(c))
    return "bn" if bengali / len(letters) >= 0.5 else "en"


def graphemes(text: str) -> List[str]:
    if _regex:
        return _regex.findall(r"\X", text)
    clusters: List[str] = []
    for ch in text:
        joins = clusters and (unicodedata.category(ch) in ("Mn", "Mc") or clusters[-1].endswith("\u09cd"))
        if joins:
            clusters[-1] += ch
        else:
            clusters.append(ch)
    return clusters


@dataclass
class EditStats:
    distance: int
    reference_length: int
    substitutions: int
    deletions: int
    insertions: int

    @property
    def rate(self) -> Optional[float]:
        if self.reference_length == 0:
            return 0.0 if self.distance == 0 else None
        return self.distance / self.reference_length

    def as_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "rate": None if self.rate is None else round(self.rate, 4)}


def _opcounts(ref: Sequence[Any], hyp: Sequence[Any]) -> Tuple[int, int, int]:
    if _RF is not None:
        subs = dels = ins = 0
        for op in _RF.editops(ref, hyp):
            if op.tag == "replace":
                subs += 1
            elif op.tag == "delete":
                dels += 1
            else:
                ins += 1
        return subs, dels, ins
    # Wagner-Fischer keeping op counts on the cheapest path.
    prev = [(j, 0, 0, j) for j in range(len(hyp) + 1)]  # (cost, subs, dels, ins)
    for i in range(1, len(ref) + 1):
        cur = [(i, 0, i, 0)]
        for j in range(1, len(hyp) + 1):
            if ref[i - 1] == hyp[j - 1]:
                best = prev[j - 1]
            else:
                s, d, n = prev[j - 1], prev[j], cur[j - 1]
                best = min(
                    (s[0] + 1, s[1] + 1, s[2], s[3]),
                    (d[0] + 1, d[1], d[2] + 1, d[3]),
                    (n[0] + 1, n[1], n[2], n[3] + 1),
                )
            cur.append(best)
        prev = cur
    _, subs, dels, ins = prev[-1]
    return subs, dels, ins


def edit_stats(reference: Sequence[Any], hypothesis: Sequence[Any]) -> EditStats:
    subs, dels, ins = _opcounts(list(reference), list(hypothesis))
    return EditStats(subs + dels + ins, len(reference), subs, dels, ins)


def cer(reference: str, hypothesis: str, *, clusters: bool = False) -> EditStats:
    ref, hyp = normalize(reference), normalize(hypothesis)
    if clusters:
        return edit_stats(graphemes(ref), graphemes(hyp))
    return edit_stats(ref, hyp)


def wer(reference: str, hypothesis: str) -> EditStats:
    ref = normalize(reference, strip_punct=True).split()
    hyp = normalize(hypothesis, strip_punct=True).split()
    return edit_stats(ref, hyp)


def cpwer(reference: Dict[str, str], hypothesis: Dict[str, str]) -> Tuple[EditStats, Dict[str, Optional[str]]]:
    """Concatenated minimum-permutation WER (CHiME-6): speaker-attributed word error.

    ``reference`` and ``hypothesis`` map a speaker to everything that speaker said. Each reference
    speaker is scored against one hypothesis speaker, under the mapping with the fewest errors, so a
    word transcribed correctly but given to the wrong speaker still counts as an error. Hypothesis
    speakers left unmatched count as insertions. Returns the pooled stats and the mapping used.
    """
    from itertools import permutations

    ref_ids, hyp_ids = list(reference), list(hypothesis)
    slots: List[Optional[str]] = list(hyp_ids) + [None] * max(0, len(ref_ids) - len(hyp_ids))
    if len(slots) > 8:
        raise ValueError("cpWER supports at most 8 speakers")
    hyp_words = {h: normalize(hypothesis[h], strip_punct=True).split() for h in hyp_ids}
    best: Optional[Tuple[int, List[EditStats], int, Tuple[Optional[str], ...]]] = None
    for perm in permutations(slots, len(ref_ids)):
        stats = [wer(reference[r], hypothesis[h] if h else "") for r, h in zip(ref_ids, perm)]
        extra = sum(len(hyp_words[h]) for h in hyp_ids if h not in perm)
        total = sum(s.distance for s in stats) + extra
        if best is None or total < best[0]:
            best = (total, stats, extra, perm)
    if best is None:
        return EditStats(0, 0, 0, 0, 0), {}
    total, stats, extra, perm = best
    pooled = EditStats(
        total,
        sum(s.reference_length for s in stats),
        sum(s.substitutions for s in stats),
        sum(s.deletions for s in stats),
        sum(s.insertions for s in stats) + extra,
    )
    return pooled, dict(zip(ref_ids, perm))


def pooled_rate(stats: Iterable[EditStats]) -> Optional[float]:
    """Corpus-level rate: total edits over total reference length (not a mean of page rates)."""
    items = list(stats)
    total = sum(s.reference_length for s in items)
    return round(sum(s.distance for s in items) / total, 4) if total else None


def correction_minutes(edits: int, *, edits_per_minute: float = 30.0, seconds_per_page: float = 20.0, pages: int = 1) -> float:
    """Estimated human time to fix the text: proofreading per page plus keystroke-level edits.

    The defaults (30 character edits a minute, 20 seconds to proofread a page) are assumptions;
    replace them with timings observed in the verification queue (``bench_accuracy.py effort``).
    """
    return round(pages * seconds_per_page / 60 + edits / max(edits_per_minute, 1e-6), 2)


# --- Classification and fields -----------------------------------------------------------------


def classification_report(truth: Sequence[str], predicted: Sequence[str]) -> Dict[str, Any]:
    labels = sorted(set(truth) | set(predicted))
    confusion: Dict[str, Counter] = defaultdict(Counter)
    for t, p in zip(truth, predicted):
        confusion[t][p] += 1
    per_class = {}
    f1s = []
    for label in labels:
        tp = confusion[label][label]
        fp = sum(confusion[t][label] for t in labels if t != label)
        fn = sum(confusion[label][p] for p in labels if p != label)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        support = sum(confusion[label].values())
        per_class[label] = {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4), "support": support}
        if support:
            f1s.append(f1)
    correct = sum(1 for t, p in zip(truth, predicted) if t == p)
    return {
        "n": len(truth),
        "accuracy": round(correct / len(truth), 4) if truth else None,
        "macro_f1": round(statistics.mean(f1s), 4) if f1s else None,
        "per_class": per_class,
        "confusion": {t: dict(row) for t, row in confusion.items()},
    }


_DATE_PATTERNS = (
    re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})"),
    re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$"),
)


def normalize_date(value: str) -> str:
    text = normalize(value, ascii_digits=True)
    for index, pattern in enumerate(_DATE_PATTERNS):
        match = pattern.match(text)
        if match:
            parts = match.groups()
            year, month, day = (parts[0], parts[1], parts[2]) if index == 0 else (parts[2], parts[1], parts[0])
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return text


def field_score(expected: Any, predicted: Any, *, field: str = "", fuzzy_threshold: float = 0.9) -> Dict[str, Any]:
    """Exact match after normalisation, plus a fuzzy match (1 - CER >= threshold) for free-text fields."""
    if isinstance(expected, list):
        want = {normalize(str(v)) for v in expected if str(v).strip()}
        got = {normalize(str(v)) for v in (predicted or []) if str(v).strip()}
        overlap = len(want & got)
        recall = overlap / len(want) if want else (1.0 if not got else 0.0)
        return {"exact": want == got, "fuzzy": recall >= 0.5, "similarity": round(recall, 4)}
    if field == "doc_date":
        a, b = normalize_date(str(expected or "")), normalize_date(str(predicted or ""))
    elif field == "reference_number":
        a = normalize(str(expected or ""), ascii_digits=True).replace(" ", "")
        b = normalize(str(predicted or ""), ascii_digits=True).replace(" ", "")
    else:
        a, b = normalize(str(expected or ""), ascii_digits=True), normalize(str(predicted or ""), ascii_digits=True)
    if not a:
        return {"exact": not b, "fuzzy": not b, "similarity": 1.0 if not b else 0.0}
    stats = edit_stats(a, b)
    similarity = max(0.0, 1 - stats.distance / max(len(a), len(b), 1))
    return {"exact": a == b, "fuzzy": similarity >= fuzzy_threshold, "similarity": round(similarity, 4)}


# --- Summaries --------------------------------------------------------------------------------


def _lcs(a: Sequence[str], b: Sequence[str]) -> int:
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b, 1):
            cur.append(prev[j - 1] + 1 if x == y else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1]


def rouge_l(candidate: str, reference: str) -> Dict[str, float]:
    cand = normalize(candidate, strip_punct=True).split()
    ref = normalize(reference, strip_punct=True).split()
    lcs = _lcs(cand, ref)
    precision = lcs / len(cand) if cand else 0.0
    recall = lcs / len(ref) if ref else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}


def contains_fact(text: str, fact: str) -> bool:
    return normalize(fact, strip_punct=True, ascii_digits=True) in normalize(text, strip_punct=True, ascii_digits=True)


# --- Retrieval and answers --------------------------------------------------------------------


def set_precision_recall(predicted: Iterable[str], relevant: Iterable[str]) -> Dict[str, Optional[float]]:
    got, want = set(predicted), set(relevant)
    precision = len(got & want) / len(got) if got else None
    recall = len(got & want) / len(want) if want else None
    return {"precision": None if precision is None else round(precision, 4), "recall": None if recall is None else round(recall, 4)}


def ndcg_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int = 10) -> float:
    want = set(relevant)
    dcg = sum(1 / math.log2(i + 2) for i, doc in enumerate(ranked[:k]) if doc in want)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(len(want), k)))
    return round(dcg / ideal, 4) if ideal else 0.0


def latency_stats(samples_ms: Iterable[float]) -> Dict[str, Optional[float]]:
    values = sorted(float(v) for v in samples_ms)
    if not values:
        return {"n": 0, "p50": None, "p95": None, "mean": None, "max": None}

    def pct(p: float) -> float:
        index = min(len(values) - 1, max(0, math.ceil(p / 100 * len(values)) - 1))
        return round(values[index], 1)

    return {"n": len(values), "p50": pct(50), "p95": pct(95), "mean": round(statistics.mean(values), 1), "max": round(values[-1], 1)}


def mean(values: Iterable[Optional[float]]) -> Optional[float]:
    items = [v for v in values if v is not None]
    return round(statistics.mean(items), 4) if items else None
