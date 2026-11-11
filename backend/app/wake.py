"""Doorbells: wake the idle queue workers when there is work, instead of making them poll.

The transcription and audio-processing threads used to sleep a fixed ``poll_s`` between
empty claims, so a fresh call waited on average half a poll before anybody looked at it.
The worker process already holds a ``LISTEN keyup_events`` connection for the alert rules
(:meth:`app.worker.Worker._listen`), and every new call is announced there as ``call.new``
in the transaction that queued its jobs — so that connection rings a bell, and the idle
threads waiting on it claim at once.

The poll stays, only slower: it is the fallback for everything the bus does not announce
(a job whose back-off has just expired, a notification lost while the LISTEN connection
was reconnecting, a job requeued by hand) and for a process that has no listener at all.

A :class:`Doorbell` counts rings rather than holding a flag.  A thread snapshots
:attr:`Doorbell.seq` *before* it claims and waits for that number to move, so a ring that
lands between an empty claim and the wait is not lost — the wait returns at once.  A ring
may also be *delayed* (:meth:`Doorbell.ring` with ``delay``): the transcription bell uses
that to give the audio pass a head start when audio processing is on.
"""

from __future__ import annotations

import threading
import time

__all__ = ["AUDIO", "AUDIO_FIRST", "STT", "Doorbell"]

#: How often a waiter looks at the stop flag.  Stopping sets an Event the bell cannot see,
#: so the wait is sliced; half a second costs nothing (no query) and keeps a SIGTERM well
#: inside compose's ten-second grace period.
STOP_CHECK_S = 0.5


class Doorbell:
    """A wake-all signal with a ring counter, for threads that otherwise poll."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._seq = 0
        #: monotonic deadline of the earliest pending delayed ring, if any
        self._due: float | None = None

    @property
    def seq(self) -> int:
        """The ring count — snapshot it before claiming, pass it to :meth:`wait`."""
        with self._cond:
            return self._seq

    def ring(self, delay: float = 0.0) -> None:
        """Wake every waiter now, or ``delay`` seconds from now.

        Delayed rings coalesce to the earliest pending one; an immediate ring clears it
        (whatever it was going to wake has just been woken).
        """
        with self._cond:
            if delay <= 0:
                self._seq += 1
                self._due = None
            else:
                due = time.monotonic() + delay
                if self._due is None or due < self._due:
                    self._due = due
            self._cond.notify_all()

    def wait(self, seen: int, timeout: float, stop: threading.Event | None = None) -> bool:
        """Block until the bell rings past ``seen``, ``timeout`` passes or ``stop`` is set.

        ``True`` when it was a ring; ``False`` for the fallback timeout or a stop.
        """
        end = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while True:
                now = time.monotonic()
                if self._due is not None and self._due <= now:
                    self._seq += 1
                    self._due = None
                    self._cond.notify_all()
                if self._seq != seen:
                    return True
                if stop is not None and stop.is_set():
                    return False
                if now >= end:
                    return False
                until = end if self._due is None else min(end, self._due)
                self._cond.wait(min(until - now, STOP_CHECK_S))


#: Rung for a new call's transcription job (and by the audio pass as it finishes a call).
STT = Doorbell()

#: Rung for a new call's audio-processing job.
AUDIO = Doorbell()

#: Set while this process' audio worker is running with ``audio_process_enabled`` on.  The
#: transcription job of a new call should then wait for the processed copy (§5.5), so the
#: listener delays its ring and the audio worker rings as soon as the pass is done.
AUDIO_FIRST = threading.Event()
