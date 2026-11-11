"""The ``worker`` process: ingest, transcription, alerts and maintenance in one place.

``python -m app.worker`` (the compose ``worker`` service) starts six things and shuts
them all down cleanly on SIGTERM:

1. **ingest** — :class:`app.ingest.watcher.IngestWatcher`: inotify plus the periodic full
   rescan that catches whatever landed while the process was down (§5.1);
2. **transcription** — :func:`app.stt.worker.run_stt_worker`, ``STT_WORKERS`` threads
   claiming ``transcription_jobs`` with ``FOR UPDATE SKIP LOCKED`` (§5.2);
3. **alerts** — a ``LISTEN keyup_events`` loop that hands each ``call.new`` /
   ``call.transcribed`` to :func:`app.alerts.evaluate`.  Listening rather than polling is
   what makes a keyword alert land seconds after the transcript;
4. **audio processing** — :func:`app.audioproc.jobs.run_audio_worker`, one thread
   claiming ``audio_jobs`` and writing each call's processed copy — trimmed, denoised,
   loudness-normalized, tones turned down — beside the original (§5.5).  Started
   unconditionally and gated inside the loop on ``audio_process_enabled``;
5. **exporters** — :func:`app.exporters.worker.run_export_worker`, one thread polling the
   ``call_exports`` ledger, plus — when ``export_broadcastify_feed_enabled`` is on — the
   Broadcastify **Feed**'s own Icecast source thread beside it, which that loop starts and
   stops.  All of it is started unconditionally and does nothing at all unless an operator
   has turned ``export_broadcastify_enabled`` / ``export_openmhz_enabled`` /
   ``export_broadcastify_feed_enabled`` on; all three ship off (docs/EXPORTERS.md);
6. **maintenance** — APScheduler: retention nightly (``CRON_RETENTION``), the export-bundle
   vacuum nightly (``CRON_EXPORT_VACUUM``), recorder-gap detection hourly over the last
   week and nightly over the whole table (``CRON_GAPS``), the site channel
   audit every ``CHANNEL_AUDIT_MINUTES`` (which also runs the RadioReference sync when it is
   due), the standby arbiter every 30 s (dormant unless ``arbiter_enabled`` is on), the
   outbound webhook queue every ``WEBHOOK_POLL_SECONDS`` (dormant unless ``webhook_url``
   is set), the Broadcastify feed poller and its archive sync (both dormant unless their
   own switches are on; docs/BCFY_STATS.md), a stale-job reaper every five minutes
   (§5.3), the unpark pass that recovers the jobs an STT outage parked (every two minutes;
   §5.2) and, beside them, the audio discard pass that deletes the recordings a talkgroup's
   **Record** toggle said not to keep (every 60 s; §5.6), and the disk-free check that
   raises ``system.disk_low`` before the disk fills (every five minutes; app/disk_watch.py),
   the health check that raises and clears ``system.health`` — a hot recorder silent, no
   new calls, transcription stalled, the database backup overdue (every minute;
   app/health.py) — and the hourly sweep of abandoned upload temp bodies in
   ``uploads/.incoming``, and the fuzzy-search vocabulary every five minutes, rebuilt whole once a day.

``--stt-only`` runs nothing but the transcription threads, which is how a backlog is
burned down against a GPU box without touching the archive.
"""

from __future__ import annotations

import argparse
import logging
import queue
import signal
import threading
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import FrameType

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import runtime_settings, wake
from app.config import Settings, get_settings
from app.db import EVENT_CHANNEL, SessionLocal

log = logging.getLogger("app.worker")

__all__ = ["Worker", "main"]

#: how long the LISTEN loop blocks before re-checking the stop flag
LISTEN_TIMEOUT_S = 5.0

#: events the alert evaluator understands; anything else on the bus is for the API
ALERT_EVENTS = ("call.new", "call.transcribed")

#: How long a new call's transcription waits for its audio pass when audio processing is
#: on (:data:`app.wake.AUDIO_FIRST`).  The audio worker rings the transcription bell the
#: moment the processed copy is written, which is the usual way in; this is the ceiling, so
#: a busy audio thread costs a live transcript a few seconds and never the fallback poll.
#: About what the old race cost: the two queues polled every 2 s and 3 s, and whichever
#: looked first decided whether Whisper got the processed copy.
AUDIO_GRACE_S = 3.0

#: How often the standby arbiter looks at the recorders.  Half a minute is short against
#: the shortest sensible staleness threshold (60 s) so a failover costs at most one tick on
#: top of it, and long enough that a pass — one small query and, almost always, no writes —
#: is free to leave running.  The interval is fixed rather than settable: the thresholds
#: that decide *behavior* are the settings, and a second knob that only changes the
#: sampling rate would be one more thing to get wrong.
ARBITER_INTERVAL_S = 30

#: How often free space is sampled (:mod:`app.disk_watch`).  Fixed: the thresholds are the
#: settings, the sampling rate is not a decision anybody needs to make.
DISK_WATCH_INTERVAL_MINUTES = 5

#: How often the health conditions are checked (:mod:`app.health`).  A minute: every check
#: is one indexed read, and the thresholds (minutes) are the settings.
HEALTH_INTERVAL_S = 60

#: How often ``uploads/.incoming`` is swept for abandoned temp bodies (:func:`app.audio.sweep_incoming`).
INCOMING_SWEEP_INTERVAL_MINUTES = 60

#: How often the Broadcastify feed poller's tick runs.  The *setting* is the poll interval;
#: this is only the sampling rate at which "is a poll due?" is asked, so an operator who
#: sets 60 s gets 60 s and one who sets 300 s is not polled early.  Fifteen seconds is
#: fine-grained enough that the shortest sensible interval is honoured and cheap enough to
#: leave running: with the feature off, a tick is one cached settings read.
BCFY_TICK_S = 15

#: …and the archive sync's tick.  Its interval is in *hours*, so asking every quarter of an
#: hour whether the watermark has aged out is more than enough.
BCFY_ARCHIVE_TICK_MINUTES = 15

#: How often the HTTPS edge is reconciled (docs/HTTPS.md).  The edge restarts on its own —
#: `restart: unless-stopped`, an image pull, an OOM — and comes back on whatever its
#: environment says, which is not necessarily what the settings table says; nothing tells
#: the API when that happens.  Five minutes bounds how long a restarted edge can serve the
#: wrong configuration, and a pass is one GET against a container on the same network.
EDGE_RECONCILE_MINUTES = 5

#: How often the audio discard pass runs (:mod:`app.audio_discard`, §5.6).  A minute: the
#: files it deletes are occupying disk the operator has said they do not want occupied, and
#: a pass that finds nothing — every pass, until Record is turned off somewhere — is one
#: probe of an empty partial index.
AUDIO_DISCARD_INTERVAL_S = 60

#: How many un-evaluated events may queue up between the LISTEN thread and the evaluator.
#: Evaluating a rule set costs a query per call and a delivery per matching rule, and a
#: burst of ingest must never block the connection that is reading NOTIFY: when the queue
#: is full its oldest event is dropped, which loses an alert rather than the whole feed.
ALERT_QUEUE_MAXSIZE = 1000


def _dsn(settings: Settings) -> str:
    """psycopg DSN from the SQLAlchemy URL (LISTEN needs a raw, autocommit connection)."""
    return settings.database_url.replace("postgresql+psycopg://", "postgresql://")


class Worker:
    """Owns every background component and the one stop event they share."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self.scheduler: BackgroundScheduler | None = None
        self.watcher = None
        self.alerts: queue.Queue[tuple[str, int]] = queue.Queue(maxsize=ALERT_QUEUE_MAXSIZE)
        self.alerts_dropped = 0
        #: whether the LISTEN thread queues events for the alert evaluator (start_listener)
        self._alerts_enabled = True
        #: when the feed poller last actually polled, so the tick can honour the interval
        self._bcfy_last_poll: datetime | None = None
        #: when the sample retention last ran, so it runs daily off a quarter-hourly tick
        self._bcfy_last_prune: datetime | None = None

    # ------------------------------------------------------------------ components

    def start_ingest(self) -> None:
        from app.ingest.watcher import IngestWatcher  # noqa: PLC0415 - watchdog is heavy

        self.watcher = IngestWatcher(self.settings)
        # The watcher schedules its own full rescan every INGEST_SCAN_MINUTES; the initial
        # catch-up scan runs on this thread so a huge archive does not block the rest of
        # the worker from coming up.
        thread = threading.Thread(target=self.watcher.start, name="ingest-start", daemon=True)
        thread.start()
        self.threads.append(thread)

    def start_stt(self, workers: int | None = None) -> None:
        from app.stt.worker import run_stt_worker  # noqa: PLC0415 - imports httpx backends

        def run() -> None:
            try:
                run_stt_worker(self.stop, workers=workers, settings=self.settings)
            except Exception:  # noqa: BLE001 - a misconfigured backend must be visible, not fatal
                log.exception("stt worker stopped")

        thread = threading.Thread(target=run, name="stt", daemon=True)
        thread.start()
        self.threads.append(thread)

    def start_audio(self) -> None:
        """The audio-processing queue (§5.5) — one thread, gated on its runtime switch.

        One thread rather than a pool: a pass is a couple of short ffmpeg invocations over a
        two-second file, so the queue drains far faster than ingest fills it, and a second
        process spawning ffmpeg alongside the transcription workers would compete with them
        for the same cores for no gain.  Like the exporter thread it is started
        unconditionally and re-reads ``audio_process_enabled`` every cycle, so switching the
        stage off in the Admin page takes effect on the next poll rather than the next
        deploy.
        """
        from app.audioproc.jobs import run_audio_worker  # noqa: PLC0415 - imports numpy

        def run() -> None:
            try:
                run_audio_worker(self.stop, settings=self.settings)
            except Exception:  # noqa: BLE001 - audio processing must never take the worker down
                log.exception("audio worker stopped")

        thread = threading.Thread(target=run, name="audio", daemon=True)
        thread.start()
        self.threads.append(thread)

    def start_exports(self) -> None:
        """The outbound exporters (Broadcastify Calls, OpenMHz, and the Broadcastify Feed).

        Always started, never presumed on: the thread wakes every ``EXPORT_POLL_SECONDS``,
        re-reads the runtime switches and goes back to sleep unless one of them is on.  That
        is what makes flipping a switch in the Admin page take effect on the next cycle
        rather than the next deploy, and it costs one query a cycle to leave running.
        """
        from app.exporters.worker import run_export_worker  # noqa: PLC0415 - imports httpx

        def run() -> None:
            try:
                run_export_worker(self.stop, settings=self.settings)
            except Exception:  # noqa: BLE001 - an exporter must never take the worker down
                log.exception("export worker stopped")

        thread = threading.Thread(target=run, name="exports", daemon=True)
        thread.start()
        self.threads.append(thread)

    def start_listener(self, *, alerts: bool = True) -> None:
        """The LISTEN connection and the evaluator that drains it — two threads, one queue.

        They are separate on purpose (M2): a rule set can take an unbounded amount of time
        to evaluate — an SMTP relay that hangs, a webhook that is slow — and doing that
        work on the connection that is reading ``NOTIFY`` would let the backlog build up
        inside PostgreSQL until it kills the connection.

        The same connection rings the queue workers' doorbells (:mod:`app.wake`), which is
        why an ``--stt-only`` process starts it too, with ``alerts=False``: the alert rules
        belong to the process that runs ingest, and must not be evaluated twice.
        """
        self._alerts_enabled = alerts
        targets = [(self._listen, "events")]
        if alerts:
            targets.append((self._evaluate_queue, "alerts"))
        for target, name in targets:
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self.threads.append(thread)

    @staticmethod
    def _ring(event: object, payload: dict) -> None:
        """Wake the queue workers a bus event has work for (:mod:`app.wake`).

        ``call.new`` queued an audio pass and a transcription for a primary copy (a
        duplicate queues neither).  A *backlog* or *bulk* upload does not ring: those jobs
        sit behind live traffic by design, the fallback poll picks them up, and a bulk push
        announcing dozens of calls a second would otherwise wake the idle threads dozens of
        times a second to find nothing they are allowed to take.  ``call.updated`` is what
        a forced re-transcribe (``POST /calls/{id}/transcribe``) announces.

        Ringing only ends an idle wait early.  Which job a woken thread takes is still the
        claim statement's decision — priority order and the backlog share — so no ring can
        reorder the queue or starve the backlog band.
        """
        from app.ingest.flow import priority_class  # noqa: PLC0415 - imports the models

        if event == "call.new":
            if payload.get("duplicate"):
                return
            if priority_class(payload.get("ingest_priority")) in ("backlog", "bulk"):
                return
            wake.AUDIO.ring()
            wake.STT.ring(AUDIO_GRACE_S if wake.AUDIO_FIRST.is_set() else 0.0)
        elif event == "call.updated":
            wake.STT.ring()

    def _offer(self, event: str, call_id: int) -> None:
        """Queue one event for the evaluator, dropping the oldest when it is full."""
        try:
            self.alerts.put_nowait((event, call_id))
            return
        except queue.Full:
            pass
        self.alerts_dropped += 1
        with suppress(queue.Empty):
            self.alerts.get_nowait()
        with suppress(queue.Full):
            self.alerts.put_nowait((event, call_id))
        if self.alerts_dropped % 100 == 1:
            log.warning(
                "alerts: evaluation is behind; %d event(s) dropped so far", self.alerts_dropped
            )

    def _listen(self) -> None:
        """Follow ``keyup_events`` and queue what the alert rules care about."""
        import json  # noqa: PLC0415

        import psycopg  # noqa: PLC0415

        while not self.stop.is_set():
            try:
                with psycopg.connect(_dsn(self.settings), autocommit=True) as conn:
                    conn.execute(f"LISTEN {EVENT_CHANNEL}")
                    log.info("listening on %s", EVENT_CHANNEL)
                    # Whatever was announced while this connection was down is lost; one
                    # ring lets the idle workers look now rather than at the fallback poll.
                    wake.AUDIO.ring()
                    wake.STT.ring()
                    while not self.stop.is_set():
                        for note in conn.notifies(timeout=LISTEN_TIMEOUT_S, stop_after=1):
                            try:
                                payload = json.loads(note.payload)
                            except ValueError:
                                continue
                            if not isinstance(payload, dict):
                                continue
                            event, call_id = payload.get("event"), payload.get("id")
                            try:
                                self._ring(event, payload)
                            except Exception:  # noqa: BLE001 - a ring is an optimisation
                                log.exception("event listener: could not ring for %s", event)
                            if not self._alerts_enabled:
                                continue
                            if event not in ALERT_EVENTS or call_id is None:
                                continue
                            self._offer(str(event), int(call_id))
            except Exception:  # noqa: BLE001 - the database went away; back off and retry
                if self.stop.is_set():
                    return
                log.exception("event listener lost its connection; retrying in 5 s")
                self.stop.wait(5.0)

    def _evaluate_queue(self) -> None:
        """Run the alert rules for every queued event until the worker stops."""
        from app.alerts import evaluate  # noqa: PLC0415

        while not self.stop.is_set():
            try:
                event, call_id = self.alerts.get(timeout=LISTEN_TIMEOUT_S)
            except queue.Empty:
                continue
            session = SessionLocal()
            try:
                evaluate(session, call_id, event=event, settings=self.settings)
            except Exception:  # noqa: BLE001 - alerts never break the loop
                log.exception("alert evaluation failed for call %s", call_id)
            finally:
                session.close()

    # ------------------------------------------------------------------ scheduled jobs

    def run_retention(self) -> None:
        from app.retention import apply_retention  # noqa: PLC0415

        session = SessionLocal()
        try:
            report = apply_retention(session, settings=self.settings)
            log.info("retention pass: %s", report.as_dict())
        except Exception:  # noqa: BLE001 - a scheduler job must never die
            log.exception("retention pass failed")
            session.rollback()
        finally:
            session.close()
        # The activity log used to be pruned here, by a single unbatched DELETE.  It is now
        # one of the clocks ``apply_retention`` runs itself (``purge_activity_events``),
        # batched, and its count is in the report logged above — so there is nothing left to
        # call between the pass and the webhook receipts.
        self._prune_webhooks()

    def _prune_webhooks(self) -> None:
        """Drop delivered webhook receipts past their keep window — the same nightly pass.

        Only ``delivered`` rows go: a parked ``failed`` one is exactly what an operator
        comes back to retry, so age is not a reason to lose it.
        """
        from app.webhooks import prune  # noqa: PLC0415 - imports httpx

        session = SessionLocal()
        try:
            removed = prune(session)
            if removed:
                log.info("webhook prune: removed %d delivered row(s)", removed)
        except Exception:  # noqa: BLE001 - a scheduler job must never die
            log.exception("webhook prune failed")
            session.rollback()
        finally:
            session.close()

    def run_export_vacuum(self) -> None:
        """Delete export bundles past ``EXPORT_KEEP_DAYS``, and old Broadcastify claims.

        Two unrelated things on one schedule because they are the same job: the largest
        thing this deployment writes, and the smallest.  A ``bcfy_claims`` row is read for
        a few seconds either side of the call it names and is dead weight after that; two
        days of them is what an operator needs to see yesterday's uploads and no more
        (docs/EXPORTERS.md, "Recorder-direct upload").
        """
        from app.exporters.claims import prune as prune_claims  # noqa: PLC0415
        from app.exports import vacuum_bundles  # noqa: PLC0415

        session = SessionLocal()
        try:
            vacuum_bundles(session, self.settings)
            gone = prune_claims(session)
            session.commit()
            if gone:
                log.info("export vacuum: pruned %d broadcastify claims", gone)
        except Exception:  # noqa: BLE001 - a scheduler job must never die
            log.exception("export vacuum failed")
            session.rollback()
        finally:
            session.close()

    def run_gaps(self, *, full: bool = False) -> None:
        """Recorder-gap detection: hourly over the recent window, nightly over everything.

        The hourly pass is what notices an outage, and a gap only exists once traffic has
        resumed, so it is always recent (:data:`app.ingest.gaps.RECENT_WINDOW`).  The
        nightly ``full`` pass on ``CRON_GAPS`` reconciles the whole table — the holes a
        backlog upload filled long after the fact.
        """
        from app.ingest.gaps import detect_gaps, recent_window_start  # noqa: PLC0415

        session = SessionLocal()
        try:
            since = None if full else recent_window_start()
            detect_gaps(session, settings=self.settings, since=since)
            session.commit()
        except Exception:  # noqa: BLE001
            log.exception("gap detection failed")
            session.rollback()
        finally:
            session.close()

    def run_gaps_full(self) -> None:
        self.run_gaps(full=True)

    def run_edge_reconcile(self) -> None:
        """Re-push the HTTPS configuration if the edge is not running it (docs/HTTPS.md).

        Cheap and silent in the ordinary case: one ``GET /config/`` against the edge, a hash
        comparison against the last configuration that was successfully loaded, and no
        write.  It re-applies only when the edge is answering for the wrong set of names, or
        when we have never managed to configure it at all.
        """
        from app.edge import service as edge_service  # noqa: PLC0415

        session = SessionLocal()
        try:
            edge_service.reconcile(session)
        except Exception:  # noqa: BLE001 - a scheduler job must never die
            log.exception("edge reconcile failed")
            session.rollback()
        finally:
            session.close()

    def run_channel_audit(self) -> None:
        """Recompute every site's channel audit, log a line each, raise the new alerts.

        Also where the RadioReference sync is due-checked from, so a deployment that has
        RR credentials gets its documentation refreshed without a second scheduler entry
        (docs/CHANNEL_AUDIT.md).  The endpoint computes the report live; this job exists
        for the log line and for the alerts.
        """
        from app.rr.audit import run_channel_audit  # noqa: PLC0415

        session = SessionLocal()
        try:
            run_channel_audit(session, settings=self.settings)
            session.commit()
        except Exception:  # noqa: BLE001 - a scheduler job must never die
            log.exception("channel audit failed")
            session.rollback()
        finally:
            session.close()

    def run_reconcile(self) -> None:
        """The batch reconciliation pass (§5.1 step 4, docs/RECONCILIATION.md).

        Inline reconciliation catches the copies that arrive close together; this catches
        the ones that do not — an sdrtrunk backlog bulk-uploaded days after trunkd recorded
        the same traffic live.  It walks everything ingested since the previous pass, which
        is the watermark it keeps in the ``settings`` table, and logs one summary line.
        """
        from app.ingest.reconcile import last_pass_at, reconcile_batch  # noqa: PLC0415

        session = SessionLocal()
        try:
            since = last_pass_at(session)
            if since is not None:
                # Overlap the previous window by the scan interval: a call ingested while
                # the last pass was running would otherwise fall between two passes.
                since -= timedelta(minutes=self.settings.reconcile_minutes)
            reconcile_batch(
                session, self.settings, since=since, advance_watermark=True
            )
            session.commit()
        except Exception:  # noqa: BLE001 - a scheduler job must never die
            log.exception("reconciliation pass failed")
            session.rollback()
        finally:
            session.close()

    def run_arbiter(self) -> None:
        """One standby-arbitration pass (:mod:`app.arbiter`, docs/RECORDERS.md).

        Registered unconditionally and gated *inside* the pass on the ``arbiter_enabled``
        runtime setting, exactly as the exporter thread is: that is what makes turning
        arbitration on in the Admin page take effect on the next tick rather than on the
        next deploy, and it costs one settings read every 30 seconds to leave in place.  The
        module-level :data:`app.arbiter.arbiter` is deliberately shared across ticks — its
        recovery clocks are the hysteresis, and a new instance each pass would mean a spare
        that never stands down.
        """
        from app.arbiter import run_arbiter  # noqa: PLC0415

        session = SessionLocal()
        try:
            report = run_arbiter(session, self.settings)
            if report.changed:
                log.info(
                    "arbiter: promoted %s, demoted %s",
                    report.promoted or "nothing",
                    report.demoted or "nothing",
                )
        except Exception:  # noqa: BLE001 - a scheduler job must never die
            log.exception("arbiter pass failed")
            session.rollback()
        finally:
            session.close()

    def run_webhooks(self) -> None:
        """One outbound-webhook delivery cycle (:mod:`app.webhooks`).

        Registered unconditionally and gated *inside* the tick on ``webhook_url`` and
        ``webhook_events``, exactly as the arbiter and the exporters are: that is what
        makes pointing a consumer's receiver somewhere else — or switching it off during
        their maintenance window — take effect on the next tick rather than the next
        deploy.  With the feature off, which is how it ships, a tick is one cached
        settings read and a return.

        A scheduler job rather than a thread of its own because the work is bounded and
        bursty: ``webhook_batch_size`` deliveries, sent one at a time, and then nothing
        until the next tick.
        """
        from app.webhooks import WebhookWorker  # noqa: PLC0415 - imports httpx

        try:
            counts = WebhookWorker(settings=self.settings).tick()
        except Exception:  # noqa: BLE001 - a scheduler job must never die
            log.exception("webhook delivery pass failed")
            return
        if any(counts.values()):
            log.info(
                "webhooks: delivered %d, failed %d", counts["delivered"], counts["failed"]
            )

    def run_requeue_stale(self) -> None:
        from app.audioproc.jobs import requeue_stale as requeue_stale_audio  # noqa: PLC0415
        from app.stt.worker import requeue_stale  # noqa: PLC0415

        session = SessionLocal()
        try:
            if requeue_stale(session, self.settings.stt_stale_minutes):
                wake.STT.ring()
            # The audio queue's crashed claims are reaped on the same tick but against its
            # own threshold (``app.audioproc.jobs.STALE_MINUTES``, 30): one audio pass can
            # legitimately run longer than the transcription queue's ten minutes.
            requeue_stale_audio(session)
        except Exception:  # noqa: BLE001
            log.exception("stale-job reaper failed")
            session.rollback()
        finally:
            session.close()

    def run_unpark(self) -> None:
        """Give the jobs an STT outage parked back to the queue (§5.2).

        The other half of the stale-job reaper beside it: that one recovers work a *worker*
        dropped, this one recovers work a *backend* dropped.  Both are cheap enough to run
        blind — this pass is one indexed count against a partial index, and it returns
        without touching the queue unless a backend is currently enabled, healthy and being
        reported on.  Every two minutes, because an outage that has just ended should not
        cost the archive another five.
        """
        from app.stt.worker import unpark_backend_outage  # noqa: PLC0415

        session = SessionLocal()
        try:
            if unpark_backend_outage(session):
                wake.STT.ring()
        except Exception:  # noqa: BLE001 - a scheduler job must never die
            log.exception("stt unpark pass failed")
            session.rollback()
        finally:
            session.close()

    def run_audio_discard(self) -> None:
        """Delete the audio the **Record** toggle said not to keep (§5.6).

        Beside :meth:`run_unpark` and shaped the same way: registered unconditionally,
        cheap to leave running, and a no-op on a deployment where every talkgroup records —
        which is every deployment until an operator turns Record off, because the pass is
        one probe of a partial index that has no entries.  Every 60 s rather than every few
        minutes because the file is occupying disk the operator has said they do not want
        occupied, and a minute is short enough that "Record off" feels like it took effect.
        """
        from app.audio_discard import run_discard  # noqa: PLC0415

        session = SessionLocal()
        try:
            report = run_discard(session, self.settings)
            if report.discarded or report.errors:
                session.commit()
            else:
                session.rollback()
        except Exception:  # noqa: BLE001 - a scheduler job must never die
            log.exception("audio discard pass failed")
            session.rollback()
        finally:
            session.close()

    def run_search_vocab(self) -> None:
        """Keep ``search_vocab`` current for fuzzy search (:mod:`app.services.fuzzy`).

        New transcripts' words every pass (~20 ms on production's rate), the whole table
        rebuilt when the last rebuild is a day old (~6 s).
        """
        from app.services.fuzzy import refresh_vocabulary  # noqa: PLC0415

        session = SessionLocal()
        try:
            summary = refresh_vocabulary(session)
            session.commit()
            if summary["mode"] == "full":
                log.info("search vocabulary rebuilt: %s", summary)
            else:
                log.debug("search vocabulary: %s", summary)
        except Exception:  # noqa: BLE001 - a scheduler job must never die
            log.exception("search vocabulary refresh failed")
            session.rollback()
        finally:
            session.close()

    def run_incoming_sweep(self) -> None:
        """Remove abandoned upload temp bodies (:func:`app.audio.sweep_incoming`)."""
        from app.audio import sweep_incoming  # noqa: PLC0415

        try:
            sweep_incoming(self.settings)
        except Exception:  # noqa: BLE001 - a scheduler job must never die
            log.exception("incoming sweep failed")

    def run_disk_watch(self) -> None:
        """Sample free space and raise ``system.disk_low`` on a crossing (:mod:`app.disk_watch`).

        Every five minutes: the host reached 98 % on 2026-09-22 with nothing saying so, and
        the upload path's 507 is the moment recording stops, not a warning.
        """
        from app.disk_watch import check  # noqa: PLC0415

        session = SessionLocal()
        try:
            check(session, self.settings)
        except Exception:  # noqa: BLE001 - a scheduler job must never die
            log.exception("disk check failed")
            session.rollback()
        finally:
            session.close()

    def run_health(self) -> None:
        """Raise or clear the health conditions (:mod:`app.health`)."""
        from app.health import check  # noqa: PLC0415

        session = SessionLocal()
        try:
            check(session, self.settings)
        except Exception:  # noqa: BLE001 - a scheduler job must never die
            log.exception("health check failed")
            session.rollback()
        finally:
            session.close()

    def run_bcfy_poll(self) -> None:
        """One Broadcastify feed poll (:mod:`app.bcfy_stats`, docs/BCFY_STATS.md).

        Registered unconditionally and gated *inside* the tick on ``bcfy_stats_enabled`` and
        the credentials, exactly as the arbiter and the webhook queue are: with the feature
        off — which is how it ships — a tick is one cached settings read and a return.  The
        module-level :data:`app.bcfy_stats.poller.poller` is shared across ticks because the
        failure backoff *is* its state; a new instance each tick would poll a dead API at
        full rate for ever.

        The tick runs at a fixed interval and the *setting* decides whether the poll is due,
        so changing ``bcfy_stats_poll_seconds`` takes effect without re-registering the job.
        """
        from app.bcfy_stats.poller import poller as feed_poller  # noqa: PLC0415 - imports httpx
        from app.bcfy_stats.poller import sync_public_page  # noqa: PLC0415

        session = SessionLocal()
        try:
            settings = runtime_settings.effective_settings(session, base=self.settings)
            # The public page rides on this tick rather than a job of its own: it is the
            # same feature, the same switch and the same feed id, and its interval is its
            # own watermark row's business.  It runs *before* the poll-due check because
            # this job returns early far more often than it polls, and the page's own
            # interval is hours rather than a minute.
            page = sync_public_page(session, settings)
            if "skipped" not in page:
                # Committed even when nothing was written, because the *watermark* was:
                # rolling it back with the poll below would fetch the page again on the
                # next fifteen-second tick, for ever.
                session.commit()
            due = feed_poller.next_attempt_at
            interval = max(5.0, float(getattr(settings, "bcfy_stats_poll_seconds", 60.0) or 60.0))
            last = getattr(self, "_bcfy_last_poll", None)
            now = datetime.now(UTC)
            if due is None and last is not None and (now - last).total_seconds() < interval:
                return
            result = feed_poller.tick(session, settings, now=now)
            if result.polled:
                self._bcfy_last_poll = now
            if result.stored:
                session.commit()
            else:
                session.rollback()
        except Exception:  # noqa: BLE001 - a scheduler job must never die
            log.exception("bcfy feed poll failed")
            session.rollback()
        finally:
            session.close()

    def run_bcfy_archives(self) -> None:
        """The archive-listing sync (:mod:`app.bcfy_stats.poller`).

        Same shape as the poll: a fixed tick, and the pass itself decides — from
        ``bcfy_archives_sync_enabled`` and its own watermark in the ``settings`` table —
        whether anything is due.  The watermark is a row rather than a field on this object
        so a restarted worker does not re-fetch a week of listings on every boot.
        """
        from app.bcfy_stats.poller import sync_archives  # noqa: PLC0415 - imports httpx
        from app.bcfy_stats.store import SAMPLE_KEEP_DAYS, prune_samples  # noqa: PLC0415

        session = SessionLocal()
        try:
            settings = runtime_settings.effective_settings(session, base=self.settings)
            sync_archives(session, settings)
            # The sample retention rides on this tick rather than on a scheduler entry of
            # its own, and at most once a day: the delete is a range scan over a table that
            # grows by one row a minute, which is nothing once a day and pointless every
            # fifteen minutes.  It runs whether or not the archive sync did anything, so a
            # deployment that never turns the archive sync on still ages its samples out.
            now = datetime.now(UTC)
            last_prune = getattr(self, "_bcfy_last_prune", None)
            if last_prune is None or (now - last_prune) >= timedelta(days=1):
                self._bcfy_last_prune = now
                removed = prune_samples(session, keep_days=SAMPLE_KEEP_DAYS)
                if removed:
                    log.info("bcfy-stats: pruned %d sample(s) past %d days", removed, SAMPLE_KEEP_DAYS)
            session.commit()
        except Exception:  # noqa: BLE001 - a scheduler job must never die
            log.exception("bcfy archive sync failed")
            session.rollback()
        finally:
            session.close()

    def start_scheduler(self, *, maintenance: bool = True) -> None:
        self.scheduler = BackgroundScheduler(timezone=self.settings.tz)
        self.scheduler.add_job(
            self.run_requeue_stale,
            "interval",
            minutes=5,
            id="requeue_stale",
            max_instances=1,
            coalesce=True,
        )
        # Beside the reaper and for the same reason: a queue that has stopped moving for a
        # reason nobody caused should start again by itself (§5.2).
        self.scheduler.add_job(
            self.run_unpark,
            "interval",
            minutes=2,
            id="unpark_backend_outage",
            max_instances=1,
            coalesce=True,
        )
        # The audio the Record toggle said not to keep, once transcription and the
        # exporters are finished with it (§5.6).  Unconditional and, on a deployment where
        # nothing has Record off, one probe of an empty partial index a minute.
        self.scheduler.add_job(
            self.run_audio_discard,
            "interval",
            seconds=AUDIO_DISCARD_INTERVAL_S,
            id="audio_discard",
            max_instances=1,
            coalesce=True,
        )
        if maintenance:
            self.scheduler.add_job(
                self.run_retention,
                CronTrigger.from_crontab(self.settings.cron_retention, timezone=self.settings.tz),
                id="retention",
                max_instances=1,
                coalesce=True,
            )
            self.scheduler.add_job(
                self.run_export_vacuum,
                CronTrigger.from_crontab(self.settings.cron_export_vacuum, timezone=self.settings.tz),
                id="export_vacuum",
                max_instances=1,
                coalesce=True,
            )
            self.scheduler.add_job(
                self.run_disk_watch,
                "interval",
                minutes=DISK_WATCH_INTERVAL_MINUTES,
                id="disk_watch",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(UTC),
            )
            self.scheduler.add_job(
                self.run_health,
                "interval",
                seconds=HEALTH_INTERVAL_S,
                id="health",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(UTC) + timedelta(seconds=HEALTH_INTERVAL_S),
            )
            # Every request removes its own temp body; this catches the ones a crashed or
            # killed process could not.
            self.scheduler.add_job(
                self.run_incoming_sweep,
                "interval",
                minutes=INCOMING_SWEEP_INTERVAL_MINUTES,
                id="incoming_sweep",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(UTC),
            )
            self.scheduler.add_job(
                self.run_gaps,
                "interval",
                hours=1,
                id="gaps",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(UTC),
            )
            self.scheduler.add_job(
                self.run_gaps_full,
                CronTrigger.from_crontab(self.settings.cron_gaps, timezone=self.settings.tz),
                id="gaps_full",
                max_instances=1,
                coalesce=True,
            )
            self.scheduler.add_job(
                self.run_search_vocab,
                "interval",
                minutes=5,
                id="search_vocab",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(UTC),
            )
            if self.settings.reconcile_enabled:
                self.scheduler.add_job(
                    self.run_reconcile,
                    "interval",
                    minutes=max(1, self.settings.reconcile_minutes),
                    id="reconcile",
                    max_instances=1,
                    coalesce=True,
                )
            self.scheduler.add_job(
                self.run_arbiter,
                "interval",
                seconds=ARBITER_INTERVAL_S,
                id="arbiter",
                max_instances=1,
                coalesce=True,
            )
            self.scheduler.add_job(
                self.run_webhooks,
                "interval",
                seconds=max(1, int(self.settings.webhook_poll_seconds or 5)),
                id="webhooks",
                max_instances=1,
                coalesce=True,
            )
            # Ticked every 15 s and gated inside; the setting decides when a poll is due,
            # so a changed interval needs no re-registration.  The archive sync is ticked
            # every 15 minutes against a watermark for the same reason.
            self.scheduler.add_job(
                self.run_bcfy_poll,
                "interval",
                seconds=BCFY_TICK_S,
                id="bcfy_poll",
                max_instances=1,
                coalesce=True,
            )
            self.scheduler.add_job(
                self.run_bcfy_archives,
                "interval",
                minutes=BCFY_ARCHIVE_TICK_MINUTES,
                id="bcfy_archives",
                max_instances=1,
                coalesce=True,
            )
            self.scheduler.add_job(
                self.run_edge_reconcile,
                "interval",
                minutes=EDGE_RECONCILE_MINUTES,
                id="edge_reconcile",
                max_instances=1,
                coalesce=True,
            )
            self.scheduler.add_job(
                self.run_channel_audit,
                "interval",
                minutes=max(1, self.settings.channel_audit_minutes),
                id="channel_audit",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(UTC),
            )
        self.scheduler.start()
        log.info(
            "scheduler: stale-job reaper every 5 min%s",
            (
                f", retention '{self.settings.cron_retention}', export vacuum "
                f"'{self.settings.cron_export_vacuum}', gaps hourly (recent) and "
                f"'{self.settings.cron_gaps}' (all), channel audit every "
                f"{self.settings.channel_audit_minutes} min, disk check every "
                f"{DISK_WATCH_INTERVAL_MINUTES} min, health check every "
                f"{HEALTH_INTERVAL_S} s, arbiter every "
                f"{ARBITER_INTERVAL_S} s, webhook delivery every "
                f"{max(1, int(self.settings.webhook_poll_seconds or 5))} s"
                + (
                    f", reconciliation every {self.settings.reconcile_minutes} min"
                    if self.settings.reconcile_enabled
                    else ""
                )
            )
            if maintenance
            else "",
        )

    # ------------------------------------------------------------------ lifecycle

    def run(self, *, stt_only: bool = False, ingest_only: bool = False, workers: int | None = None) -> None:
        if not ingest_only:
            self.start_stt(workers)
        if not stt_only:
            self.start_ingest()
            self.start_listener()
            self.start_audio()
            self.start_exports()
        else:
            # No alerts here, but the transcription threads still want their doorbell.
            self.start_listener(alerts=False)
        self.start_scheduler(maintenance=not stt_only)
        try:
            while not self.stop.wait(1.0):
                pass
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        log.info("worker: shutting down")
        self.stop.set()
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
        if self.watcher is not None:
            self.watcher.shutdown()
        for thread in self.threads:
            thread.join(timeout=30)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - process entry point
    parser = argparse.ArgumentParser(
        prog="python -m app.worker",
        description="Keyup worker: ingest, transcription, alerts and maintenance.",
    )
    parser.add_argument(
        "--stt-only", action="store_true", help="run only the transcription threads"
    )
    parser.add_argument(
        "--ingest-only", action="store_true", help="run ingest, alerts and maintenance, no STT"
    )
    parser.add_argument("--workers", type=int, default=None, help="STT threads (default STT_WORKERS)")
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args(argv)

    settings = get_settings()
    logging.basicConfig(
        level=(args.log_level or settings.log_level).upper(),
        format="%(asctime)s %(levelname)-5.5s [%(name)s] %(message)s",
    )

    worker = Worker(settings)

    def _signal(signum: int, frame: FrameType | None) -> None:  # noqa: ARG001
        log.info("signal %s — stopping", signum)
        worker.stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _signal)

    worker.run(stt_only=args.stt_only, ingest_only=args.ingest_only, workers=args.workers)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
