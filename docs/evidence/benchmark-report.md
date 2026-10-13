# AI accuracy and benchmark report

Section 9 item covered here: AI Accuracy / Benchmark Report (with section 4.2 and 7.2 of the
post-demonstration report). Raw tables: [`benchmark-results.md`](benchmark-results.md). JSON
reports: `eval/reports/`. Manifest formats and the bank sample plan: `eval/benchmarks/README.md`.

**Status, 13 October 2026.** OCR and retrieval have been measured on synthetic and CI data, and
speech to text with speaker attribution on a small synthetic two-speaker set (English only is
meaningful; see 2a). Bangla speech, diarization, metadata, summarisation and Ask need bank samples
and are ready to run; nothing below claims accuracy on real Sonali Bank documents yet.

## 1. Method

The benchmark harness (`scripts/bench_accuracy.py`) calls the same functions the product uses
(`ocr.ocr_image_detail`, `ai.transcribe`, metadata suggestion, briefing generation, the HTTP Ask
endpoint), so results describe the shipped code rather than a separate prototype.

| Benchmark | Metrics | Human effort measure |
|---|---|---|
| OCR | CER (code points and grapheme clusters), WER, mean confidence, p50/p95 latency, high-error pages caught by review routing | Estimated correction minutes per page from edit distance (`--edits-per-minute`, default 30) |
| STT | WER, CER, real-time factor, speaker count error, speaker-attributed WER (cpWER) with the diarizer and with perfect speaker turns | Minutes of transcript editing |
| Metadata | Document-type accuracy and macro-F1 with confusion matrix; per-field exact / normalised match | Fields changed at confirmation (`metadata_confirm changed=`) |
| Summary | Heading detection, ROUGE-L against reference points, citation grounding (cited page contains the point) | Sections edited before approval |
| Ask | Answer-or-refuse accuracy, citation precision and recall, required facts present, restricted-document leaks, latency | — |
| Effort (observed) | From the audit log: time between `verify_open` and `verify_approve` per reviewer and version, minutes edits, briefing edits | Measured, not estimated |

Text is normalised before scoring (Unicode NFC, zero-width characters removed, whitespace folded,
Bangla digits folded to ASCII). Bangla WER tokenisation keeps vowel signs and hasanta inside words.

## 2. OCR results (synthetic set)

120 typeset pages (60 Bangla, 60 English) from board-paper style text, each rendered at three
qualities: `clean` (300 dpi), `scan` (200 dpi, 1.2° skew, blur, noise) and `poor` (150 dpi, 2.5°
skew, heavy noise, JPEG quality 30). Generator: `scripts/make_synthetic_benchmarks.py`.

| Language | Pages | CER | CER (graphemes) | WER | Mean confidence |
|---|---|---|---|---|---|
| Bangla, before line recovery | 60 | 3.2% | 3.9% | 9.0% | 90.2 |
| Bangla, with line recovery | 60 | **1.8%** | **2.5%** | **7.9%** | 89.9 |
| English | 60 | 0.1% | 0.1% | 0.2% | 91.2 |

### Finding 1: Tesseract silently drops whole Bangla lines

The two worst Bangla pages before the fix had 51% and 41% CER. Tesseract's layout analysis merges
a Bangla line (joined by the headstroke) into a block it then skips, and the page still reports high
confidence because confidence is computed only over the words it did read. Fix shipped in
`ocr.py`: find ink bands not covered by any recognised line, re-read each as a single line, and cap
the page confidence when ink coverage stays under `OCR_MIN_INK_COVERAGE` (0.9) so the page goes to
review. Those two pages dropped to 0.6% and 0.7% CER.

Controlled latency comparison on a 24-page subset (same machine, no other load):

| Metric | Recovery off | Recovery on |
|---|---|---|
| CER (all) | 2.2% | 0.56% |
| Bangla CER | 5.5% | 1.4% |
| Bangla WER | 10.8% | 7.2% |
| Mean latency per page | 1,812 ms | 1,801 ms |
| p95 latency per page | 2,276 ms | 2,013 ms |
| High-error pages | 1 | 0 |

Recovery adds no measurable latency when it has nothing to do and pays for itself in the rare pages
where it runs. (The full 120-page run with recovery shows a higher p95 because it ran while other
jobs shared the CPU; the controlled subset is the reliable figure.)

### Finding 2: Bangla confidence is poorly calibrated

Bangla pages scoring 85–92 have the same error rate as each other, so a single confidence threshold
does not separate good pages from bad ones. On the synthetic set, routing at the default thresholds
caught 0 of 3 Bangla pages above 5% CER. The product now has a separate Bangla page bar,
`OCR_BANGLA_PAGE_REVIEW_THRESHOLD`. **Recommendation for the POC: 92**, then tune it on bank
samples so that at least 90% of pages above 5% CER reach a reviewer while the review queue stays
manageable.

A second signal works better than any confidence bar: on the bad pages, Tesseract replaces whole
Bangla words with Latin or ASCII-digit junk ("অ্যান্ড কোং" read as "WIS (@12") at low word
confidence. A page with at least one such word (`OCR_BANGLA_SUSPECT_WORDS=1`, word confidence
below `OCR_BANGLA_SUSPECT_WORD_CONF=50`) goes to review. On the synthetic set this catches **3 of
3** Bangla pages above 5% CER while flagging 8 of 60 Bangla pages (13%) and 0 of 60 English pages
(`eval/reports/ocr-synthetic.json`, `review_routing`). It is on by default. A second Tesseract pass
with a different page-segmentation mode was also tried as a cross-check; it produced identical text
on these pages, so it is not used.

### Finding 3: legacy Bijoy-font PDFs

Many Bangladeshi documents are typed in Bijoy fonts (SutonnyMJ and similar) whose PDF text layer
is ANSI gibberish ("‡mvbvjx e¨vsK" for "সোনালী ব্যাংক"). The text layer passes a length check, so
without detection it would be indexed as unsearchable text. The pipeline now detects this encoding
and OCRs those pages (`text_layer: legacy_bangla` in page metadata). This must be measured on bank
samples (`quality: legacy_font`), since the synthetic set uses Unicode fonts only. Documents ingested
before this change should be re-ingested if they came from Bijoy sources.

### Remaining limits

Handwriting is not recognised (route to review). Very poor faxes and stamps over text produce the
remaining errors; see the "failure cases" lists in `benchmark-results.md`.

## 2a. Speech to text and speaker attribution (synthetic two-speaker set)

`make_synthetic_benchmarks.py --sets stt` builds eight two-speaker meetings with espeak-ng, with
exact speaker turns; `bench_accuracy.py stt` scores them with Whisper `small` (CPU, int8).
Speaker-attributed WER is cpWER (CHiME-6): each reference speaker's words against one hypothesis
speaker's, under the best mapping, so a word given to the wrong speaker is an error.

| Slice | Files | WER | cpWER, perfect turns | Real-time factor |
|---|---|---|---|---|
| English | 2 | 14.8% | 14.8% | 0.14 |
| Bangla (espeak-ng voice) | 2 | 119% | 120% | 2.85 |
| Bangla + English speakers | 4 | 80% | 79% | 0.88 |

Findings:

1. **Attribution plumbing works.** With perfect turns, cpWER equals WER on English: Whisper's
   segment boundaries do not straddle speaker changes separated by a short pause.
2. **The synthetic Bangla voice is unusable.** Whisper hears espeak-ng Bangla as noise (it emits
   emoji and loops, hence the real-time factor near 3). These rows say nothing about Bangla
   recognition; real recordings are required. Generating better Bangla speech with Meta's MMS-TTS
   was rejected because its weights are licensed for non-commercial use only.
3. **Language detection is per file.** Left to detect the language, Whisper picks one for the
   whole recording; a meeting that starts in Bangla loses its English turns and vice versa.
   faster-whisper's per-segment `multilingual` mode did not change the result on this set. Decide
   between `WHISPER_LANGUAGE=bn` and a larger model on real code-switched recordings.
4. **Diarization accuracy is not measured yet.** The pyannote model is gated on Hugging Face; with
   the bank's token (or the bank-hosted `DIARIZATION_URL`), the same command reports cpWER with
   the real diarizer next to the perfect-turns ceiling.
5. Running the benchmark found that a worker image built today would install PyAV 16+, which
   faster-whisper 1.2 cannot open audio with. `requirements-worker.txt` now pins `av<16`.

## 3. Retrieval (CI evaluation)

`eval/retrieval_bilingual.jsonl`: 100 queries (50 Bangla, 50 English) over a bilingual test corpus,
including restricted documents and users without grants. Run on every commit with deterministic stub
embeddings, so it tests ranking, fusion, filters and access control, not embedding quality.

| Slice | Queries | Recall@10 | MRR | Grounded | Cited page correct |
|---|---|---|---|---|---|
| All | 100 | 100% | 0.99 | 100% | 100% |
| Bangla | 50 | 100% | 0.98 | 100% | 100% |
| English | 50 | 100% | 0.99 | 100% | 100% |

Restricted-document leaks: **0**. Filter violations: 0. `scripts/eval_retrieval.py --live` runs the
same evaluation with the configured embedding and rerank providers (cloud or on-prem), and the
bank's own question set is run through the `ask` benchmark below.

## 4. Benchmarks ready to run on bank data

| Benchmark | Needs | Command |
|---|---|---|
| STT | 10 board-room recordings with typed transcripts and speaker turns (30–60 min per language at minimum) | `bench_accuracy.py stt --manifest …/stt.jsonl --json eval/reports/stt-cloud.json` (set `DIARIZATION_PROVIDER` for diarized cpWER) |
| Metadata | 150 documents with confirmed metadata | `bench_accuracy.py metadata --manifest …/metadata.jsonl --json …` |
| Summary | 20 board papers with management's reference briefing | `bench_accuracy.py summary --manifest …/summary.jsonl --json …` |
| Ask | Running UAT instance, test users per role (`BENCH_TOKENS`), 100 questions | `BENCH_BASE_URL=… bench_accuracy.py ask --manifest …/ask.jsonl --json …` |
| Effort | UAT audit log after reviewers have worked the queue | `bench_accuracy.py effort --since 2026-11-01 --json …` |

Each is run twice, cloud and on-prem, and compared with
`bench_accuracy.py compare eval/reports/X-cloud.json eval/reports/X-local.json` (see
`local-ai-cutover.md`).

## 5. Proposed POC acceptance targets

To be agreed with the bank before the bank-sample run.

| Measure | Target |
|---|---|
| Bangla OCR CER on `clean` + `scan` pages | ≤ 3% |
| English OCR CER | ≤ 1% |
| Pages above 5% CER routed to human review | ≥ 90% |
| Bangla STT WER on board-room audio | ≤ 25% (minutes are always human-reviewed) |
| Document-type accuracy | ≥ 90% |
| Briefing points grounded in the cited page | ≥ 95% |
| Ask: correct answer-or-refuse | ≥ 90%; restricted leaks = 0 |
| Search p95 / Ask p95 at 100 concurrent users | ≤ 1.5 s / ≤ 8 s (`docs/CAPACITY.md`) |
