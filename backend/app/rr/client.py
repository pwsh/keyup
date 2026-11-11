"""The RadioReference SOAP API, behind an interface small enough to fake.

RadioReference's ``api.radioreference.com/soap2`` is an RPC-style SOAP service; every call
carries an ``authInfo`` struct with the operator's **own** app key and Premium-subscription
credentials.  Four methods are all this feature needs:

``getTrsDetails(sid, authInfo)``
    the system: its name, its WACN/system id, its NAC.
``getTrsSites(sid, authInfo)``
    every site: ``siteId``, ``siteNumber``, ``siteDescr``, ``rfss``, ``nac``, ``neighbors``
    and ``siteFreqs`` — each ``{freq (MHz, double), use ('d' primary control, 'a' alternate
    control, or empty for voice), lcn, colorCode, chanId}``.
``getTrsTalkgroups(sid, 0, 0, 0, authInfo)``
    the talkgroup list, used to fill in aliases the directory does not have and the
    service tag RR files each channel under (each entry carries a ``tags`` array of
    ``{tagId, tagDescr}``).
``getTag(0, authInfo)``
    every frequency and talkgroup tag definition — the ``tagId`` → ``tagDescr`` table.
    Asked for once per sync run and cached, because it is the same forty-odd rows for
    every system and a talkgroup list can carry thousands of references into it.

:class:`ZeepRadioReference` is the real one and imports ``zeep`` lazily, so a deployment
that never configures RR — the default — does not need the wheel and nothing in the test
suite reaches the network.  Tests pass their own object satisfying
:class:`RadioReferenceClient`.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from app.config import Settings, get_settings

log = logging.getLogger("app.rr.client")

__all__ = [
    "RR_WSDL",
    "RadioReferenceClient",
    "RadioReferenceError",
    "ZeepRadioReference",
    "credentials_present",
    "make_client",
]

#: RPC style, latest version — the combination the SOAP service documents
RR_WSDL = "http://api.radioreference.com/soap2/?wsdl&v=latest&s=rpc"


class RadioReferenceError(RuntimeError):
    """The RR service refused, or is not configured."""


@runtime_checkable
class RadioReferenceClient(Protocol):
    """What :mod:`app.rr.sync` needs.  Four reads, no writes.

    ``tags`` is optional on purpose: a test double (or an older one) that does not
    implement it still satisfies this protocol at the call sites, which fall back to the
    ``tagDescr`` the talkgroup record itself carries.
    """

    def trs_details(self, sid: int) -> dict[str, Any]: ...

    def trs_sites(self, sid: int) -> list[dict[str, Any]]: ...

    def trs_talkgroups(self, sid: int) -> list[dict[str, Any]]: ...


def credentials_present(settings: Settings | None = None) -> bool:
    """True when RR_APP_KEY, RR_USERNAME and RR_PASSWORD are all set."""
    settings = settings or get_settings()
    return bool(settings.rr_app_key and settings.rr_username and settings.rr_password)


def _plain(value: Any) -> Any:
    """zeep answers with its own object types; this is the recursive dict/list of them."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if hasattr(value, "__values__"):  # zeep CompoundValue
        return {str(k): _plain(v) for k, v in value.__values__.items()}
    return value


class ZeepRadioReference:
    """The real client.  One zeep service, reused for every call of one sync."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not credentials_present(self.settings):
            raise RadioReferenceError("RR_APP_KEY, RR_USERNAME and RR_PASSWORD are not all set")
        self._service: Any | None = None

    @property
    def auth(self) -> dict[str, str]:
        return {
            "appKey": self.settings.rr_app_key,
            "username": self.settings.rr_username,
            "password": self.settings.rr_password,
            "version": "latest",
            "style": "rpc",
        }

    def _call(self, method: str, *args: Any) -> Any:
        if self._service is None:
            try:
                from zeep import Client  # noqa: PLC0415 - optional dependency, imported on use
            except ImportError as exc:  # pragma: no cover - depends on the install extras
                raise RadioReferenceError(
                    "the RadioReference sync needs the 'zeep' package (pip install keyup[rr])"
                ) from exc
            self._service = Client(RR_WSDL).service
        try:
            return _plain(getattr(self._service, method)(*args, self.auth))
        except RadioReferenceError:
            raise
        except Exception as exc:  # noqa: BLE001 - a SOAP fault is data, not a crash
            raise RadioReferenceError(f"{method} failed: {type(exc).__name__}: {exc}") from exc

    def trs_details(self, sid: int) -> dict[str, Any]:
        return dict(self._call("getTrsDetails", int(sid)) or {})

    def trs_sites(self, sid: int) -> list[dict[str, Any]]:
        return list(self._call("getTrsSites", int(sid)) or [])

    def trs_talkgroups(self, sid: int) -> list[dict[str, Any]]:
        return list(self._call("getTrsTalkgroups", int(sid), 0, 0, 0) or [])

    def tags(self) -> list[dict[str, Any]]:
        """RR's whole tag table (``getTag(0)``): ``{tagId, tagDescr}`` for every tag.

        Not per-system and not per-talkgroup — one call answers for the whole run, which
        is why :mod:`app.rr.sync` asks for it once and passes the table down.
        """
        return list(self._call("getTag", 0) or [])


def make_client(settings: Settings | None = None) -> RadioReferenceClient | None:
    """A live client, or ``None`` when RR is not configured (the default)."""
    settings = settings or get_settings()
    if not credentials_present(settings):
        return None
    return ZeepRadioReference(settings)
