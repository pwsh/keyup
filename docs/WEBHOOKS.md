# Links and outbound webhooks

Two generic features for tying the archive to another system — a CAD or calls-for-service
application, an incident tracker, a case-management tool, or a script of your own:

- **Links** record that a call (or a whole conversation) belongs to a record over there.
- **Outbound webhooks** tell that system when something happens here, instead of it polling.

Both work with the ordinary API: reads are open by default, and every write needs an API token
(`docker compose exec api keyup token create <name> --scopes read,write`, sent as
`Authorization: Bearer kup_…`). The full schema is at `/api/docs` and `/api/openapi.json` on your
own deployment.

## Links — `/api/links`

```json
POST /api/links
{"call_id": 5894, "source": "cad", "external_id": "2026-000123",
 "link_confidence": "probable", "link_method": "time+agency", "link_note": "…"}
```

- `source` names the system that owns `external_id` (`cad`, `manual`, anything you choose). Re-posting
  the same `(source, external_id, call_id|conversation_id)` updates the confidence and note rather
  than creating a duplicate.
- `source`, `link_confidence` and `link_method` default to `manual`/`strong`/`manual` — a person's
  confident assertion — **only for `source=manual`**. Any other source must say how sure it is, or the
  request is a 422 naming the missing fields.
- `GET /api/links/{source}/{external_id}` returns every linked call (with audio URLs) for one outside
  record, in time order, so the other system can render confirmed audio without searching again.
- `GET /api/links?source=…&external_id=…&call=…&conversation=…` lists links (paged, `X-Total-Count`);
  `DELETE /api/links/{id}` removes one.
- The call page shows a call's links in its "External links" card.

`GET /api/timeline?from=…&to=…` is the companion read: compact per-minute activity for a window,
for a sparkline beside an incident.

## Outbound webhooks

Polling `/api/calls` on a timer is a query a minute for the times when nothing happened, and holding
`WS /api/live` open makes the consumer's own uptime decide whether it hears about a call at all. A
webhook is the third answer — keyup remembers what it owes and keeps trying.

**Off unless somebody turns it on.** In Admin → Settings → Webhooks (or `WEBHOOK_*` in the
environment) set `webhook_url`, tick the events, and set `webhook_secret`. A blank URL or an empty
event list means nothing is ever queued. The secret is write-only: it can be set, replaced and
cleared, never read back — reads of the archive are open by default, and a signing key that could be
read is one anybody who can see the site could forge with.

`webhook_call_scope` decides which calls are worth a delivery: `all`, `live` (only calls that arrived
as live traffic, so an archive migration is silent), `linked` (only calls that already carry a link),
or `live+linked`. Link events are never filtered by it.

Each delivery is a JSON `POST` with three headers:

| header | meaning |
|---|---|
| `X-Keyup-Event` | `call.stored`, `call.transcribed`, `link.created`, `link.deleted` |
| `X-Keyup-Delivery` | this delivery's id — also in the body, so either is enough to dedupe on |
| `X-Keyup-Signature` | `sha256=<hex>`, HMAC-SHA256 of the **exact request bytes** under `webhook_secret`. Absent when no secret is set |

Verify it before trusting the body — compare with `hmac.compare_digest`, never `==`:

```python
expected = "sha256=" + hmac.new(SECRET.encode(), request.body, hashlib.sha256).hexdigest()
if not hmac.compare_digest(expected, request.headers.get("X-Keyup-Signature", "")):
    return HttpResponse(status=401)
```

Answer **2xx** and keyup is done with it. Anything else is retried with a widening backoff (30 s,
60 s, 120 s, … capped at an hour) until `webhook_max_attempts`, after which the delivery is parked
as `failed` and left alone; a 4xx that is not 408 or 429 is parked at once, because "not this
schema, ever" is not something a sixth try fixes. Deliveries go out **one at a time**, in order, so
`call.stored` for a call always precedes its `call.transcribed`.

`call.stored` fires when the recording is in the archive and playable — *before* transcription, so a
consumer that wants the audio does not wait on an engine. `call.transcribed` fires when text exists.

```jsonc
// X-Keyup-Event: call.stored   (call.transcribed is the same shape, with the transcript filled in)
{
  "delivery_id": 4192,
  "event": "call.stored",
  "at": "2026-08-29T14:17:04.881233+00:00",
  "call": {
    "id": 5894,
    "system": "Example System", "site": "Central",
    "tgid": 1601, "talkgroup_id": 31, "talkgroup_alias": "EXAMPLE LAW 1",
    "talkgroup_ids": [1601],
    "category": "law", "service_tag": "Law Tac", "agency_code": "EXPD1",
    "radio_id": 812, "unit_id": "E1",
    "started_at": "2026-08-29T19:17:03+00:00", "started_local": "2026-08-29T14:17:03",
    "duration_s": 3.2, "call_type": "group",
    "emergency": false, "encrypted": false,
    "ingest_source": "trunkd", "ingest_priority": "live",
    "transcript_status": "pending", "transcript_text": null, "transcript_quality": null,
    "url": "https://keyup.example.com/calls/5894",
    "audio_url": "https://keyup.example.com/api/calls/5894/audio"
  }
}
```

```jsonc
// X-Keyup-Event: link.created   (link.deleted is the same shape, sent just before the row goes)
{
  "delivery_id": 4193,
  "event": "link.created",
  "at": "2026-08-29T14:22:10.104881+00:00",
  "link": {
    "id": 77, "source": "cad", "external_id": "2026-000123",
    "call_id": 5894, "conversation_id": null,
    "link_confidence": "probable", "link_method": "time+agency",
    "link_note": "…", "created_by": "token:cad",
    "url": "https://keyup.example.com/calls/5894"
  }
}
```

The `url` and `audio_url` links are built from `PUBLIC_BASE_URL`.

Two properties worth building against. **The body is frozen when the event happened** — a retry
three hours later re-sends what was true then, not a re-read of the row now, which is what makes
this an event log and what lets a delivery outlive the call being deleted by retention. And
**`ingest_priority` is on every call payload**: a `backlog` or `bulk` event is an archive migration
replaying old traffic, not news. `?priority=live|backlog|bulk` filters the same way anywhere the
call filters are accepted.

`GET /api/admin/webhooks` shows the switch, the queue tallies and the last deliveries with the error
each one hit (URLs scrubbed out — a delivery URL can carry a token in its path).
`POST /api/admin/webhooks/deliveries/{id}/retry` puts a parked one back at the front with a clean
attempt count. Nothing here can affect ingest, transcription or playback: the queue row is written
in the same transaction as the work that caused it and drained by a separate worker job, so a
receiver that is down costs keyup a retry and nothing else. Delivered rows are receipts and the
nightly pass drops them after a week; `failed` and `pending` ones are never pruned by age, because a
parked delivery is exactly the one somebody comes back to retry.

## Mapping agencies and units

An outside system usually names an agency and a unit, not a talkgroup. Talkgroups carry
`agency_code` and radios carry `unit_id` and `agency_code`; `GET /api/agencies` lists the agencies
with the talkgroups and radios mapped to each, and `?agency=` / `?unit=` work on every call filter.
See [TALKGROUPS.md](TALKGROUPS.md) for loading an agency list (`keyup seed-agencies`), classifying
talkgroups from a rules file (`keyup classify-talkgroups --rules`), and flagging the shared dispatch
channels no single agency owns (`keyup mark-shared`).
