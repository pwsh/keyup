"""P25 protocol knowledge that is *not* about any one site: what the numbers mean.

The recorder sends Keyup numbers — ``mfid:opcode``, band ids, service bits — and the
archive stores them as it heard them.  This package is the other half: the tables that
turn one of those numbers back into the name the standard (or the vendor, or four days of
analysis) gives it.  Nothing here touches the database or the API.
"""
