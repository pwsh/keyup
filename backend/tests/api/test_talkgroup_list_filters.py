"""``GET /talkgroups``: a filter and a sort per column of the directory table (§4.2).

The page caps a page at 500 rows, so anything the table offers to sort or narrow by has to
be answerable *here*.  A client that sorted or filtered the rows it happened to have been
handed would be answering "of the channels on this screen, which are not transcribed"
while the header says "not transcribed" — the exact bug these tests exist to keep out.

So: every sortable header has a key the server accepts, every filter control has a query
parameter, the three-valued ones distinguish ``false`` from absent, and an unknown key is
a 422 rather than a silent fall-back to the default order.
"""

from __future__ import annotations

import pytest

from tests.api.conftest import bearer
from tests.conftest import requires_db

pytestmark = requires_db

#: Every key the table's headers ask for, including the one that is not a column.
SORT_KEYS = [
    "tgid",
    "alias",
    "description",
    "category",
    "service_tag",
    "agency_code",
    "priority",
    "calls_24h",
    "call_count",
    "first_seen_at",
    "last_seen_at",
    "record",
    "transcribe",
    "is_hidden",
    "shared",
    "export_broadcastify",
    "export_bcfy_calls",
    "export_openmhz",
]


@pytest.mark.parametrize("key", SORT_KEYS)
@pytest.mark.parametrize("direction", ["", "-"])
def test_every_column_the_table_sorts_by_is_a_key_the_server_takes(
    client, archive, key, direction
) -> None:
    """Both directions of every header, because ``nullslast()`` is applied to each
    separately and a typo in one of the two branches only shows up on one arrow."""
    response = client.get("/api/talkgroups", params={"sort": f"{direction}{key}"})
    assert response.status_code == 200, response.text
    assert response.json()["items"]


def test_an_unknown_sort_key_is_refused_rather_than_ignored(client, archive) -> None:
    # Falling back to the default order would answer a question nobody asked and look
    # like a UI bug at the far end.
    assert client.get("/api/talkgroups", params={"sort": "loudness"}).status_code == 422


def test_empty_values_sort_last_in_both_directions(client, archive, write_token) -> None:
    """A channel with no service tag is not "before A" or "after Z" — it is not in the
    ordering at all, and it belongs at the bottom whichever arrow is showing."""
    rows = client.get("/api/talkgroups", params={"page_size": 500}).json()["items"]
    tagged = next(row for row in rows if row["service_tag"])
    untagged = [row for row in rows if not row["service_tag"]]
    assert untagged, "the fixture archive should leave some channels untagged"

    for sort in ("service_tag", "-service_tag"):
        items = client.get("/api/talkgroups", params={"sort": sort, "page_size": 500}).json()["items"]
        codes = [row["service_tag"] for row in items]
        assert codes.index(tagged["service_tag"]) < codes.index(None)


def test_the_sort_is_stable_enough_to_page(client, archive) -> None:
    """Sorting by a boolean puts every row in one of two buckets; without a tie-break the
    same channel can arrive on two consecutive pages and another on neither."""
    first = client.get("/api/talkgroups", params={"sort": "-transcribe", "page_size": 2}).json()
    second = client.get(
        "/api/talkgroups", params={"sort": "-transcribe", "page_size": 2, "page": 2}
    ).json()
    ids = [row["id"] for row in first["items"] + second["items"]]
    assert len(ids) == len(set(ids))


def test_the_24_hour_count_orders_by_traffic_the_page_can_see(client, archive, write_token) -> None:
    """The "Last 24h" column is the one header with no column behind it. The fixture
    archive's calls are recent, so the channels that were heard outrank the one that has
    never been heard at all — and that one still appears, as a zero rather than a null."""
    system_id = client.get("/api/talkgroups").json()["items"][0]["system_id"]
    silent = client.post(
        "/api/talkgroups",
        json={"tgid": 99_999, "system_id": system_id, "alias": "NEVER HEARD"},
        headers=bearer(write_token),
    )
    assert silent.status_code == 201, silent.text

    items = client.get("/api/talkgroups", params={"sort": "-calls_24h", "page_size": 500}).json()[
        "items"
    ]
    heard = set(archive.by_tgid)
    positions = [index for index, row in enumerate(items) if row["tgid"] in heard]
    quiet = [index for index, row in enumerate(items) if row["tgid"] == 99_999]
    assert positions and quiet
    assert max(positions) < min(quiet)
    # It is still in the answer: an inner join would have dropped it entirely.
    assert len(items) == client.get("/api/talkgroups").json()["total"]


def test_the_policy_filters_tell_false_apart_from_unasked(client, archive, write_token) -> None:
    """The tri-state the filter row is built on. ``transcribe=false`` is a question with
    an answer; leaving it out is not the same as asking for ``true``."""
    everything = client.get("/api/talkgroups", params={"page_size": 500}).json()
    victim = everything["items"][0]
    client.patch(
        f"/api/talkgroups/{victim['id']}",
        json={"transcribe": False, "record": False},
        headers=bearer(write_token),
    )

    off = client.get("/api/talkgroups", params={"transcribe": "false", "page_size": 500}).json()
    assert [row["id"] for row in off["items"]] == [victim["id"]]
    on = client.get("/api/talkgroups", params={"transcribe": "true", "page_size": 500}).json()
    assert victim["id"] not in [row["id"] for row in on["items"]]
    assert on["total"] + off["total"] == everything["total"]

    # And the filters AND with each other rather than replacing one another.
    both = client.get(
        "/api/talkgroups", params={"transcribe": "false", "record": "true", "page_size": 500}
    ).json()
    assert both["total"] == 0


@pytest.mark.parametrize(
    "field", ["export_broadcastify", "export_bcfy_calls", "export_openmhz", "shared"]
)
def test_each_export_checkbox_and_shared_filter_on_its_own_column(
    client, archive, write_token, field
) -> None:
    """One filter per checkbox, not one for "any export": an operator auditing what goes
    to OpenMHz must not be shown the channels that only go to Broadcastify."""
    rows = client.get("/api/talkgroups", params={"page_size": 500}).json()["items"]
    victim = rows[0]
    wanted = not victim[field]
    client.patch(
        f"/api/talkgroups/{victim['id']}", json={field: wanted}, headers=bearer(write_token)
    )

    hits = client.get(
        "/api/talkgroups", params={field: str(wanted).lower(), "page_size": 500}
    ).json()
    assert victim["id"] in [row["id"] for row in hits["items"]]
    assert all(row[field] is wanted for row in hits["items"])


def test_the_service_tag_filter_is_checked_against_rrs_own_spelling(client, archive) -> None:
    """RR's tags are a closed list with fussy spelling ("Fire Dispatch", "Fire-Talk"). A
    near miss has to be a 422 and not a confident empty directory."""
    hit = client.get("/api/talkgroups", params={"service_tag": "Fire Dispatch"}).json()
    assert hit["total"] >= 1
    assert all(row["service_tag"] == "Fire Dispatch" for row in hit["items"])
    # Case is the client's business, not the operator's.
    assert client.get("/api/talkgroups", params={"service_tag": "fire dispatch"}).json()["total"] == (
        hit["total"]
    )

    bad = client.get("/api/talkgroups", params={"service_tag": "Fire Despatch"})
    assert bad.status_code == 422
    assert "vocabulary" in bad.json()["detail"]


def test_the_system_can_be_named_either_way(client, archive) -> None:
    """``system`` is what this endpoint has always called it and ``system_id`` is what the
    rest of the API calls a system reference; the filter row sends the latter."""
    row = client.get("/api/talkgroups").json()["items"][0]
    by_old = client.get("/api/talkgroups", params={"system": row["system_id"]}).json()
    by_new = client.get("/api/talkgroups", params={"system_id": row["system_id"]}).json()
    assert by_new["total"] == by_old["total"] >= 1
    assert client.get("/api/talkgroups", params={"system_id": 99_999}).json()["total"] == 0


def test_the_agency_filter_narrows_and_the_count_follows(client, archive, write_token) -> None:
    """The pager's total is the filtered total — the count under a filtered table saying
    "of 1,412" would be describing a directory the reader is not looking at."""
    rows = client.get("/api/talkgroups", params={"page_size": 500}).json()["items"]
    victim = rows[0]
    client.patch(
        f"/api/talkgroups/{victim['id']}",
        json={"agency_code": "EXAMPLEFD"},
        headers=bearer(write_token),
    )
    hits = client.get("/api/talkgroups", params={"agency": "examplefd", "page_size": 500}).json()
    assert victim["id"] in [row["id"] for row in hits["items"]]
    assert hits["total"] == len(hits["items"])
    assert all(row["agency_code"] == "EXAMPLEFD" for row in hits["items"])
