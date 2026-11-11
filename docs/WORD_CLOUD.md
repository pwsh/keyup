# Word clouds per radio and per talkgroup — design note

*Written 2026-09-23. Not built.*

**Purpose.** The words a radio or a talkgroup uses most, and the ones it uses much more than the archive does.
The first list describes the channel. The second is what identifies an operator or a unit: a callsign, a
street, a facility. Both surface systematic mis-transcriptions (a place name that comes back spelt three ways)
worth adding to the STT domain prompt (`stt_prompt_extra`).

## Where it lives

- **UI.** A *Words* card on the Radio detail page and on the Talkgroup detail page, beside the busiest-hours
  chart. A window picker (7 / 30 / 90 days) and a toggle between *Most used* and *Distinctive*. Clicking a
  word opens `/calls?radio=…` or `/calls?talkgroup=…` with `q=<word>`, so every word leads to the calls behind
  it. A tag cloud reads poorly and is hard to use from a keyboard, so the card is a ranked bar list (ECharts,
  already a dependency), with the cloud as an optional view.
- **API.** `GET /radios/{id}/words` and `GET /talkgroups/{id}/words`, taking `days` (default 30, at most 365)
  and `limit` (default 60). Answer: `[{word, n, calls, score}]` plus `calls_scanned` and `truncated`. Same read
  scope as the detail pages. Cached in-process for 10 minutes per (scope, id, days), like the stats caches.
- **Code.** `app/services/words.py` (the query and the stop list) and the two routes in the existing
  directory routers. No migration and no worker job for version 1.

## The query

Surface words, not stems. `search_tsv` holds English stems (`copi`, `respond`), which are fine for matching and
wrong for display. Tokenise `calls.transcript_text` directly:

```sql
WITH scope AS (                       -- newest first, bounded (see "Cost")
  SELECT id, transcript_text
  FROM calls
  WHERE talkgroup_id = :id                       -- or radio_id = :id
    AND started_at >= now() - make_interval(days => :days)
    AND duplicate_of IS NULL
    AND transcript_quality IS DISTINCT FROM 'noise'
    AND coalesce(transcript_text, '') <> ''
  ORDER BY started_at DESC
  LIMIT 20000
), words AS (
  SELECT s.id, w
  FROM scope s, regexp_split_to_table(lower(s.transcript_text), '[^a-z0-9'']+') AS w
  WHERE length(w) >= 3
    AND ts_lexize('english_stem', w) <> '{}'     -- Postgres' own English stop words
    AND w <> ALL (:radio_stopwords)              -- copy, ahead, i'm, affirmative, …
)
SELECT w AS word, count(*) AS n, count(DISTINCT id) AS calls
FROM words GROUP BY w
HAVING count(DISTINCT id) >= 3                  -- one long call is not a pattern
ORDER BY n DESC LIMIT :limit * 3;
```

- The two scopes use the existing `ix_calls_talkgroup_started` / `ix_calls_radio_started` indexes. A
  radio-scoped cloud should also count `target_radio_id` for unit-to-unit calls.
- `ts_lexize('english_stem', w) = '{}'` is how Postgres says a word is on its English stop list. That removes
  "the", "you" and the like without shipping a list. Radio filler (*copy*, *ahead*, *affirmative*, *i'm*,
  *thank*) needs a short list of Keyup's own, reviewed against real output. A dev-archive run gave
  *i'm, copy, going, county, back, ahead* as the top six. *Thank* also stands in for the hallucination filter's
  "Thank you." Numbers split at the hyphen ("10-4" becomes "10" and "4") and fall to the length floor; unit
  numbers worth keeping ("E1", "medic 21") would need the tokeniser to keep letter-digit runs.
- **Distinctive** reuses the spelling-variant search's vocabulary (`search_vocab`) instead of a new table. `search_vocab.ndoc` is the number of
  archive calls containing each stem. Stem the surface word (`ts_lexize('english_stem', w)[1]`), join, and rank
  by `calls_in_scope / scope_calls ÷ ndoc / archive_calls`, with a floor on `calls` so a word heard twice
  cannot win. That is one join against a 181k-row primary-key table: nothing to maintain.

## Cost on 5.2 M calls

Measured on the dev archive (7,220 transcribed calls): the tokenise, stop-list and count query takes 80–150 ms
warm, about 15–20 µs of CPU per transcript. A large archive can hold a few million transcripts. The busiest talkgroup
(*EC LAW 1*) logs about 2,600 calls a day.

| Scope | Calls scanned | Estimate |
| --- | --- | --- |
| A typical radio, 30 days | hundreds to a few thousand | < 100 ms |
| A quiet talkgroup, 30 days | a few thousand | ~100 ms |
| *EC LAW 1*, 30 days | ~78,000 → capped at 20,000 | ~0.3–0.4 s CPU, plus up to ~20k heap-page reads cold (a few seconds on a cold cache); cached 10 min after |
| The whole archive | 2.6 M | ~45 s CPU plus a full read of `calls`: never on request |

The `LIMIT 20000` cap bounds the worst case. The answer says `truncated: true` and the card says "from the
latest 20,000 calls", which for a channel that busy is a week, not a month. That is honest, and a busy
channel's vocabulary does not change much between a week and a month.

**If whole windows on busy channels matter later**, add a daily rollup: `word_counts_daily (scope_kind, scope_id,
day, word, n, calls)`, filled by a nightly worker job for the previous day, like the `search_vocab` job. Rough
size: about 100 talkgroups and 1,000 radios active a day, each with a few hundred distinct words, is a few
hundred thousand rows a day, so it needs retention (keep 400 days) and a migration. Any window becomes a
`SUM … GROUP BY word` over at most 365 rows per word. That is version 2, only if version 1 proves useful.

## Not in scope

Per-person or per-unit clouds (the roster stays empty until talker aliases feed it), bigrams ("engine 3"; a second `regexp_matches` pass later), and non-English transcripts.
