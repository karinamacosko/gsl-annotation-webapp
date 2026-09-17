# GSL Annotation Tool

Web app for building a dataset of English sentences translated into Ghanaian Sign Language (GSL) gloss.

## Run

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 5000            # http://localhost:5000
```

## Data

- `data/sentences.csv` – the 1000 English sentences (input).
- `data/tokens.csv` – GSL dictionary tokens used for autocomplete.
- `data/users.json` – annotator accounts (created from the UI, no authentication).
- `data/associations.json` – extra associations added by annotators via "Other".
- `data/annotations.csv` – output; one row per submission with the columns
  `id, english_sentence, category, sentence_type, word_count, length_band, gsl_gloss, annotator_id, annotator_role, timestamp, confidence, non_manual_markers, flagged_missing_sign, notes`.
  Download it at `/api/annotations.csv`.

## Assignment rules

- Every sentence is annotated once.
- Every 4th sentence (250 sentences) is annotated by 3 different annotators. Change with `GSL_MULTI_EVERY` (e.g. `GSL_MULTI_EVERY=5` → 200 sentences).
- An annotator never sees the same sentence twice; sentences with no annotations are served before partially-annotated ones.
- Users see an alert when nothing is left for them / when the whole set is complete.

## Gloss format

- Dictionary signs: `HELLO`
- Fingerspelling: `fs-"Accra"`
- Missing sign: `SIGN_NOT_FOUND` or `SIGN_NOT_FOUND(SUGGESTED)`; sets `flagged_missing_sign=yes`.
