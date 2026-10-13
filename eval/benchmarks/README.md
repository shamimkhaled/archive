# Benchmark manifests

`scripts/bench_accuracy.py` reads one JSONL manifest per benchmark. Paths inside a manifest are
relative to the manifest file, so a sample folder can be moved or mounted anywhere. Bank samples
contain real documents and recordings: keep them on bank storage, never in git. Everything in
`eval/benchmarks/` except this README is git-ignored.

## Formats

| Benchmark | One line per | Fields |
|---|---|---|
| `ocr` | page | `id`, `file` (PDF or image), `page` (1-based), `lang` (`bn`, `en`, `mixed`), `reference` or `reference_file` (UTF-8 ground-truth text), `quality` (`clean`, `scan`, `poor`, `handwritten`, `legacy_font`, …) |
| `stt` | recording | `id`, `audio`, `lang` (`bn`, `en`, `mixed`; `mixed` lets Whisper detect), `reference` or `reference_file`, `speakers` (count, optional), `turns` (optional: `[{"speaker", "start", "end", "text"}]` in seconds; enables speaker-attributed WER) |
| `metadata` | document | `id`, `file` or `text` or `text_file`, `lang`, `expected`: `doc_type`, `title`, `reference_number`, `department`, `subject`, `category`, `doc_date` |
| `summary` | board paper | `id`, `file` or `pages`, `title`, `sensitivity`, `expected`: heading → list of reference points, or `"not_found"` |
| `ask` | question | `id`, `question`, `lang`, `user` (key into `BENCH_TOKENS`), `expect_docs`, `expect_answer` (facts), `should_refuse`, `forbidden_docs` |

Example lines:

```json
{"id": "BM-2019-07-p3", "file": "pages/board-memo-2019-07.pdf", "page": 3, "lang": "bn", "reference_file": "truth/board-memo-2019-07-p3.txt", "quality": "scan"}
{"id": "AGM-2025-a", "audio": "audio/agm-2025-part1.m4a", "lang": "bn", "reference_file": "truth/agm-2025-part1.txt", "speakers": 6}
{"id": "q-017", "question": "২০২৪ সালের বাজেট কে অনুমোদন করেছে?", "lang": "bn", "user": "member", "expect_docs": ["SB-2024-0113"], "expect_answer": ["পরিচালনা পর্ষদ"], "should_refuse": false, "forbidden_docs": ["SB-2024-0200"]}
```

## Building the bank sample set

Recommended minimum for the POC (section 4.2 of the post-demonstration report):

| Set | Size | Mix |
|---|---|---|
| OCR | 200 pages | 100 Bangla, 60 English, 40 mixed; at least 30 legacy Bijoy-font PDFs, 30 poor scans, 10 handwritten annotations |
| STT | 10 recordings, ≥ 5 hours | Board-room audio, Bangla with English code-switching, 3–10 speakers |
| Metadata | 150 documents | All document types in `document_types.py`, both languages |
| Summary | 20 board papers | 10–80 pages each, with management's reference briefing |
| Ask | 100 questions | 50 Bangla, 50 English; 20 should refuse; 15 probe restricted documents with a user who has no grant |

Ground truth is typed by the bank's records staff (two people per sample; disagreements resolved by
a third). Time spent per page is recorded, which calibrates `--edits-per-minute`.

## Synthetic set

`PYTHONPATH=src python scripts/make_synthetic_benchmarks.py` typesets 120 OCR pages (Bangla and
English, three scan qualities) plus metadata and Ask manifests into `synthetic/`. It exercises the
pipeline and the metrics without bank data; it does not replace bank samples.

`--sets stt` builds eight two-speaker "meetings" (about 45 seconds each) with espeak-ng: a Chair
voice and a CFO voice alternate turns read from the corpus, in Bangla, English, or one language
each. The manifest carries the exact speaker turns, so `bench_accuracy.py stt` reports
speaker-attributed WER (cpWER) twice: with the configured diarizer, and with the reference turns
as a perfect diarizer (the ceiling set by Whisper's segment boundaries). espeak-ng's Bangla voice
is not intelligible to Whisper, so Bangla rows of this set measure nothing about Bangla accuracy;
credible STT numbers need 30–60 minutes of real, consented board-room recordings per language in
the same manifest format, with typed speaker turns.

## Speaker turns for real recordings

For each recording, the transcriber types who spoke and when (start and end in seconds, from any
audio editor), one entry per turn, in the `turns` field. `speakers` is then the number of distinct
speakers. Run with the diarizer enabled on the worker image (`DIARIZATION_PROVIDER=pyannote` or
`http`):

    DIARIZATION_PROVIDER=pyannote HF_TOKEN=… PYTHONPATH=src python scripts/bench_accuracy.py stt \
        --manifest /bank/samples/stt.jsonl --json eval/reports/stt-bank.json
