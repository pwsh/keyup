"""What a control-channel message kind is *called*.

``site_message_kinds`` remembers every ``mfid:opcode`` a site has ever transmitted, and
until something names them they are all equally anonymous — one busy site shipped with 0 of 267
named, which makes "unknown" say nothing at all.  Most of them are not unknown to anybody:
the standard opcodes have had names since TIA-102.AABC, and the recorder that sent them
prints those names in its own logs.  This module is that table, so a fresh site starts
named and the ``unknown`` count means *nobody anywhere has named this*.

Three sources, in order:

**The built-in standard table** (:data:`OSP_OPCODE_NAMES`) — the 64 outbound TSBK opcodes
for mfid 0, copied verbatim from trunkd's ``crates/trunkd-p25/src/message.rs`` (by way of
``harness/identifiers.py``) so that Keyup and the recorder call a message the same thing.
Where the spec and trunkd disagree on spelling, trunkd wins: its name is the one in the
logs the operator is reading beside this.

**The vendor entries trunkd names** (:data:`VENDOR_NAMES`) — the L3Harris TSBKs from the
``164:*`` rows of the trunkd identifier registry (docs/IDENTIFIERS.md §6).

**An optional curated TOML** — trunkd's ``docs/identifiers.toml`` ``[tsbk]`` table, passed
to ``keyup sites name-message-kinds --toml``.  It carries the findings of the unknown-data
analysis, including the *noise* entries: a ``mfid:opcode`` that occurred once with a CRC
residual its own parity forbids is not a message, it is a false pass.  Those are given
their label and deliberately left ``known = false``: they are explained, not known — a
thing that never happened cannot be a message kind the site transmits.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

__all__ = [
    "BUILTIN_NAMES",
    "OSP_OPCODE_NAMES",
    "VENDOR_NAMES",
    "builtin_name",
    "is_noise",
    "label_for",
    "load_curated",
]

#: The 64 standard outbound (OSP) TSBK opcode names for mfid 0, opcode = index.
#: Copied verbatim from trunkd's ``crates/trunkd-p25/src/message.rs``: an opcode trunkd
#: does not decode still has its spec name here, which is the whole point — a message
#: nobody parses is not a message nobody can name.
OSP_OPCODE_NAMES: tuple[str, ...] = (
    "GRP_VCH_GRANT", "RESERVED_01", "GRP_VCH_GRNT_UPD", "GRP_VCH_GRNT_UPX",
    "UU_VCH_GRANT", "UU_ANS_REQ", "UU_VCH_GRANT_UPD", "RESERVED_07",
    "TEL_INT_VCH_GRNT", "TEL_INT_VCH_GRNU", "TEL_INT_ANS_RQST", "RESERVED_0B",
    "RESERVED_0C", "RESERVED_0D", "RESERVED_0E", "RESERVED_0F",
    "IND_DCH_GRANT", "GRP_DCH_GRANT", "GRP_DCH_ANNOUNCE", "GRP_DCH_ANNC_EXP",
    "SNDCP_DCH_GRANT", "SNDCP_DCH_PAG_RQ", "SNDCP_DCH_ANN_EX", "RESERVED_17",
    "STATUS_UPDATE", "RESERVED_19", "OSP_STATUS_QUERY", "RESERVED_1B",
    "MESSAGE_UPDATE", "RADIO_MONITR_CMD",
    "RADIO UNIT MONITOR ENHANCED COMMAND", "CALL_ALERT",
    "ACK_RESPONSE_FNE", "QUEUED_RESPONSE", "RESERVED_22", "RESERVED_23",
    "EXTNDED_FUNC_CMD", "RESERVED_25", "RESERVED_26", "DENY_RESPONSE",
    "GRP_AFFIL_RESP", "SCCB_CCH_BCST_EX", "GRP_AFFIL_QUERY",
    "LOCN_REG_RESPONS", "UNIT_REG_RESPONS", "UNIT_REG_COMMAND",
    "AUTH_COMMAND", "DE_REGIST_ACK", "TDMA_SYNC_BCST", "AUTH_DEMAND",
    "AUTH_FNE_RESP", "IDEN_UPDATE_TDMA", "IDEN_UPDATE_VUHF",
    "TIME_DATE_ANNOUN", "ROAM_ADDR_CMD", "ROAM_ADDR_UPDATE", "SYS_SVC_BCAST",
    "SEC_CCH_BROADCST", "RFSS_STATUS_BCST", "NET_STATUS_BCAST",
    "ADJ SITE STATUS", "IDEN_UPDATE", "ADJ_STS_BCST_UNC", "OSP RESERVED 3F",
)

#: Vendor TSBKs the trunkd registry names (docs/IDENTIFIERS.md §6, ``164:*`` rows).
#: The two beacons are byte-identical on every one of ~686 000 occurrences and confirmed
#: transmitted by two recorders — named, because "L3Harris beacon" is what is known about
#: them, even though their content is not.
VENDOR_NAMES: dict[tuple[int, int], str] = {
    (164, 1): "L3Harris beacon (opcode 1)",
    (164, 15): "L3Harris beacon (opcode 15)",
    (164, 48): "HARRIS REGROUP",
}

#: ``(mfid, opcode) → name`` for everything Keyup knows without being told.
BUILTIN_NAMES: dict[tuple[int, int], str] = {
    **{(0, opcode): name for opcode, name in enumerate(OSP_OPCODE_NAMES)},
    # mfid 1 is the standard's "alternate" manufacturer id and carries the same opcodes
    # (the trunkd registry names 1:0, 1:2, 1:3, 1:22, 1:57, 1:59, 1:60 exactly so).
    **{(1, opcode): name for opcode, name in enumerate(OSP_OPCODE_NAMES)},
    **VENDOR_NAMES,
}


def builtin_name(mfid: int, opcode: int) -> str | None:
    """The built-in name for one kind, or ``None`` when nothing here names it."""
    return BUILTIN_NAMES.get((int(mfid), int(opcode)))


def is_noise(label: str | None) -> bool:
    """True for a label the registry marks as a decode artefact rather than a message.

    The convention is the trunkd registry's own: ``CRC false pass (noise)``, ``Mangled
    NET_STATUS_BCAST (noise)``.  Such a row is *explained* — which is worth storing — but
    it is not a message kind the site transmits, so it must not be counted as known.
    """
    if label is None:
        return False
    lowered = label.lower()
    # A *reserved* opcode has no defined message behind it: the standard leaves the slot
    # empty, so a decode that lands on one is a CRC false pass wearing a valid-looking
    # opcode, not a message kind the site transmits.  Named, so the table reads the same
    # as the trunkd registry, but never counted as known.
    return "noise" in lowered or "reserved" in lowered


def load_curated(path: str | Path) -> dict[tuple[int, int], str]:
    """The ``[tsbk]`` table of a trunkd ``identifiers.toml``, as ``(mfid, opcode) → name``.

    Keys are ``"mfid:opcode"`` and values are tables carrying at least ``name``; anything
    shaped differently is skipped rather than raising, because this file is maintained by
    hand in another repository and a typo in it should not stop the run naming the other
    two hundred rows.
    """
    with Path(path).open("rb") as handle:
        document = tomllib.load(handle)
    table = document.get("tsbk")
    if not isinstance(table, dict):
        return {}
    names: dict[tuple[int, int], str] = {}
    for key, entry in table.items():
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        mfid, _, opcode = str(key).partition(":")
        try:
            names[(int(mfid), int(opcode))] = name
        except ValueError:
            continue
    return names


def label_for(
    mfid: int, opcode: int, curated: dict[tuple[int, int], str] | None = None
) -> tuple[str | None, bool]:
    """``(label, known)`` for one kind: what to call it, and whether that counts as known.

    The curated file wins over the built-in table — it is the later, site-specific finding
    ("SNDCP_DCH_ANN_EX, byte-identical on 345 000 occurrences, so this site advertises no
    data channel") and it is the only source that can say a pair is noise.
    """
    key = (int(mfid), int(opcode))
    label = (curated or {}).get(key) or BUILTIN_NAMES.get(key)
    if label is None:
        return None, False
    return label, not is_noise(label)
