"""The generated OpenAPI document — this API is meant for third-party consumption."""

from __future__ import annotations

import pytest

from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def schema(client):  # noqa: ANN001, ANN201
    return client.get("/api/openapi.json").json()


def test_the_document_is_served_and_described(schema) -> None:
    assert schema["info"]["title"] == "Keyup"
    assert "X-Total-Count" in schema["info"]["description"]
    assert "patch" in schema["info"]["description"].lower()


def test_every_router_has_a_tag_with_a_description(schema) -> None:
    described = {tag["name"] for tag in schema["tags"]}
    used = {
        tag
        for path in schema["paths"].values()
        for operation in path.values()
        for tag in operation.get("tags", [])
    }
    assert used <= described, f"undocumented tags: {sorted(used - described)}"
    assert all(tag["description"] for tag in schema["tags"])


def test_every_operation_has_a_summary(schema) -> None:
    missing = [
        f"{method.upper()} {path}"
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
        if not operation.get("summary")
    ]
    assert missing == []


def test_the_headline_endpoints_are_documented(schema) -> None:
    for path in (
        "/api/calls",
        "/api/calls/{call_id}",
        "/api/calls/{call_id}/audio",
        "/api/calls/export",
        "/api/search",
        "/api/links",
        "/api/stats/overview",
        "/api/admin/settings",
        "/api/auth/login",
        "/api/metrics",
    ):
        assert path in schema["paths"], path
        assert schema["paths"][path]["get" if "get" in schema["paths"][path] else "post"]["description"]


def test_list_endpoints_document_the_total_count_header(schema) -> None:
    for path in ("/api/calls", "/api/talkgroups", "/api/radios", "/api/links"):
        assert "X-Total-Count" in schema["paths"][path]["get"]["responses"]["200"]["headers"]


def test_the_bearer_scheme_is_declared(schema) -> None:
    scheme = schema["components"]["securitySchemes"]["BearerAuth"]
    assert scheme["scheme"] == "bearer"
    assert scheme["bearerFormat"].startswith("kup_")
    assert schema["security"] == [{"BearerAuth": []}]


def test_the_shared_filters_are_documented_on_every_endpoint_that_takes_them(schema) -> None:
    for path in ("/api/calls", "/api/calls/facets", "/api/search", "/api/calls/export"):
        names = {p["name"] for p in schema["paths"][path]["get"]["parameters"]}
        assert {"q", "talkgroup", "agency", "unit", "date_from", "include_duplicates"} <= names


def test_call_out_carries_both_clocks(schema) -> None:
    call = schema["components"]["schemas"]["CallOut"]["properties"]
    assert call["started_at"]["format"] == "date-time"
    assert "local" in call["started_local"]["description"]
    assert call["talkgroup_ids"]["description"]
