"""Site channel audit: the air, RadioReference and the recorder's configuration.

The recorder is the only thing that sees the air.  A P25 control channel announces its
own frequency, its backup control channels, its neighbors, the band plan and every
traffic channel it grants; RadioReference documents what *should* be there; the recorder's
config lists what it is told to hunt.  Keyup holds all three and reports the differences —
undocumented channels, missing channels, control-channel drift, recorder-config gaps and
unknown adjacent sites (docs/CHANNEL_AUDIT.md).

* :mod:`app.rr.observations` — ``POST /api/recorder/observations`` into the three tables
* :mod:`app.rr.client` — the RadioReference SOAP client behind a two-method interface
* :mod:`app.rr.sync` — RR → ``site_frequencies`` with ``source='radioreference'``
* :mod:`app.rr.audit` — the report, the alerts and the scheduled job
"""

from __future__ import annotations

__all__ = ["audit", "client", "observations", "sync"]
