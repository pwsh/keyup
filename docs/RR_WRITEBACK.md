# RadioReference write-back — feasibility

*Investigated 2026-09-23. Nothing here is built.*

**Question.** Keyup reads RadioReference (RR) — `getTrsDetails`, `getTrsSites`, `getTrsTalkgroups`, `getTag`
through the SOAP service (`app/rr/client.py`, [CHANNEL_AUDIT.md §3](CHANNEL_AUDIT.md)). Can it send corrections
back: talkgroups RR does not list, a wrong alpha tag, a site's undocumented channels?

**Answer: not programmatically.** RR has no write interface; its only correction channel is a signed-in,
human-reviewed web form. What Keyup can do is draft the submission for an operator to check and paste.

## What RR offers

| Channel | Writes? | Source |
| --- | --- | --- |
| SOAP Web Service (`api.radioreference.com/soap2`) | **No.** Every documented method is a `get…`/`search…` read (`getTrsDetails`, `getTrsSites`, `getTrsTalkgroups`, `getTag`, `getCountyInfo`, `searchCountyFreq`, …); there is no add, update or submit method. Each user authenticates with their own Premium account and each application needs an app key issued by RR. | [RadioReference.com Web Service 3.1](https://wiki.radioreference.com/index.php/RadioReference.com_Web_Service3.1), [API](https://wiki.radioreference.com/index.php/API) |
| Database submissions (`/db/submit/`, the *Submit Info* button on a system or county page) | **Yes, by a person.** A signed-in form. Multi-row data such as a talkgroup list is pasted as plain text, columns separated by a tab or two or more spaces, following RR's per-type templates, with talkgroups in decimal. There is no file or CSV upload. A volunteer database administrator reviews every submission, may ask questions, and accepts or rejects it. | [RR Database Submission Guidelines](https://wiki.radioreference.com/index.php/RR_Database_Submission_Guidelines) |
| Broadcastify Calls ingest | **No.** Upload-only (audio plus talkgroup, frequency, source id, time). Unknown talkgroups are simply sent; they get identified by listeners and end up in the same submission form. | [Broadcastify Calls ingest API](https://www.broadcastify.com/calls/dev/), [forum thread on unknown talkgroups](https://forums.radioreference.com/threads/broadcastify-calls-and-unknown-talkgroups.481306/) |

**Terms.** RR's [terms of use](https://www.radioreference.com/terms/) prohibit commercial use and redistribution of
its data without a licence. They do not mention automated submissions. The database admins do expect every item to
be something the submitter has verified: heard, in use, and not copied from FCC licence data
([forum reminder from a database admin](https://forums.radioreference.com/threads/a-friendly-reminder-regarding-database-submissions.387945/)).
A script that posts the form would be scraping a login-gated page to do something RR expects a person to vouch
for, so it is not an option even though no clause names it.

Not verified: the raw WSDL could not be fetched and parsed during the investigation, so an undocumented write
method cannot be ruled out entirely. Nothing in RR's documentation or forums refers to one.

## What Keyup could send, strongest evidence first

1. **Site channels.** Every frequency the control channel announces that RR does not list, and every adjacent
   site the system broadcasts that RR does not know about. Both are already computed as the channel audit's
   *undocumented* and *unknown neighbor* findings, with first-seen and last-seen times
   ([CHANNEL_AUDIT.md](CHANNEL_AUDIT.md)). They are objective: the system says so on the air.
2. **Talkgroups RR does not list.** Talkgroups with calls in Keyup and no entry in `getTrsTalkgroups`, with call
   count, first and last heard, busiest hours, the radios heard on them and links to sample calls. The operator
   listens and proposes the alpha tag and description.
3. **Alpha tag or description differences.** A local alias that differs from RR's. This is the weakest
   evidence: a rename in Keyup is often local preference (`_name_talkgroups` in `app/rr/sync.py` never overwrites
   an operator's alias for exactly this reason), so these should be listed for review and never submitted
   wholesale.
4. RR-listed talkgroups never heard are **not** worth submitting. Not hearing a talkgroup is evidence about this
   receiver, not about the system.

## The realistic route: a submission report (not built)

- **Where.** A *RadioReference submission* card on the Sites page (per system), and
  `keyup rr submission-report --system <id>` for the same text on the command line. Admin only, because it
  calls the RR API with the deployment's Premium credentials.
- **What it does.** One `getTrsTalkgroups` and one `getTrsSites` for the system (the sync already makes these
  calls, but it keeps no copy of RR's talkgroup list), diffed against the directory and the stored channel
  audit. Output: plain text in RR's submission template, decimal talkgroups, columns separated by two spaces,
  one block per section above. Each row carries its evidence (counts, first and last heard, a link to a sample
  call) as a comment for the operator; the operator removes those before pasting. A copy button, no submit
  button.
- **Bookkeeping.** A `submitted_at` note per row, so the next report does not suggest the same items again. That
  can live in `settings` or a small table: one migration, only if this is built.
- **Cost.** Small to medium: the channel-audit query exists; the new parts are the RR talkgroup diff, the
  template formatter and a card. No new RR endpoints and no new permissions.

Transcripts are a separate question. The Broadcastify
Calls ingest fields documented above include no transcript. OpenMHz was not part of this investigation.
