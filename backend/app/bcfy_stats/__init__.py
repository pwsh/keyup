"""Broadcastify **feed owner** statistics: listeners, status and archive listings.

A different product from the Broadcastify *Calls* exporter in :mod:`app.exporters`.  That
one publishes this archive's recordings; this one *reads* how the operator's own live audio
feed is doing — how many people are listening right now, whether it is up, and which
archived blocks exist — and keeps the answers so the history can be asked questions the
Broadcastify site does not answer: when is this feed hot, and does that follow the radio
traffic or the clock?

Four pieces:

* :mod:`~app.bcfy_stats.client` — the one HTTP endpoint, and the deliberately tolerant
  reading of a response whose field names are not published (docs/BCFY_STATS.md);
* :mod:`~app.bcfy_stats.public_page` — the *public*, login-free feed page, whose
  server-rendered sparkline is the only 24-hour listener history Broadcastify publishes and
  therefore the only backfill this feature has;
* :mod:`~app.bcfy_stats.store` — the sample, its hourly rollup, the archive upsert;
* :mod:`~app.bcfy_stats.poller` — the worker's two jobs and the backoff between failures;
* :mod:`~app.bcfy_stats.summary` — hot times: the 7x24 matrices, the peak, the correlation.

Everything ships **off**: with ``bcfy_stats_enabled`` false — the default — the worker's
tick is a settings read and a return.
"""

from __future__ import annotations

__all__ = ["client", "poller", "public_page", "store", "summary"]
