"""HEAD on the audio route answers with headers only (players probe before downloading)."""

from __future__ import annotations


def test_head_audio_returns_headers_without_body(client, archive) -> None:
    response = client.head(f"/api/calls/{archive.call_ids[0]}/audio")
    assert response.status_code == 200
    assert response.headers["accept-ranges"] == "bytes"
    assert int(response.headers["content-length"]) > 0
    assert response.headers["etag"]
    assert response.content == b""
