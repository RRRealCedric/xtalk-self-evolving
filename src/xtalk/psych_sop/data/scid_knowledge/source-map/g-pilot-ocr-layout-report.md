# G Pilot OCR and Layout Report

## Scope

This report records local OCR and geometric alignment for the G Pilot source pages. It is source-layer evidence, not a clinical interpretation or a runtime bundle.

## Reproducibility

- Source SHA-256: `681d666c27eec13923e06b8f12317c2c27736c96ef0f0651d45bb65dc5cf799e`
- Renderer: `pypdfium2` at 300 DPI
- OCR engine: `/opt/homebrew/bin/tesseract` (tesseract 5.5.3)
- OCR language / PSM: `chi_sim+eng` / `6`

## Rendered pages

| PDF page | Pixels | OCR words | OCR lines |
| ---: | --- | ---: | ---: |
| 4 | 2482 x 3509 | 490 | 34 |
| 232 | 2482 x 3509 | 434 | 40 |
| 233 | 2482 x 3509 | 410 | 39 |

## OCR quality queue

- Low-confidence words (< 60): 158
- Critical-token review entries (< 85): 52
- Queue status: `queued_for_human_review`

## Layout-source alignment

| Layout asset | PDF page | Intersecting OCR lines |
| --- | ---: | ---: |
| `scan-s9` | 4 | 8 |
| `scan-s12` | 4 | 7 |
| `g2-g3` | 232 | 12 |
| `g2-g6-g7` | 232 | 17 |
| `g3-g11` | 233 | 23 |

## Interpretation boundary

Tesseract text, confidence, reading order, and geometry require human source review. The artifact does not assign constructs, scores, criteria, diagnoses, or runtime transitions.
