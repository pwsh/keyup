"""Optional integrations (``app/integrations/*/``) are discovered, never required.

The public tree ships without the private CAD package, so the application has to start —
and serve its whole API — when ``app/integrations/`` holds nothing but its ``__init__.py``.
This runs the real application from a copy of ``app/`` with every integration package
removed, in a fresh interpreter so nothing already imported here leaks into it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

PROBE = """
import json
from fastapi.testclient import TestClient
from app import integrations
from app.main import INTEGRATIONS, app

client = TestClient(app)  # no context manager: the lifespan (live bus, edge) is not started
schema = client.get("/api/openapi.json").json()
print(json.dumps({
    "discovered": integrations.discover(),
    "registered": INTEGRATIONS,
    "paths": sorted(schema["paths"]),
    "correlate_status": client.get("/api/correlate", params={"at": "2026-01-01T00:00:00Z"}).status_code,
    "description": schema["info"]["description"],
}))
"""


def _run_without_integrations(tmp_path: Path) -> dict:
    copy = tmp_path / "app"
    shutil.copytree(APP, copy, ignore=shutil.ignore_patterns("__pycache__"))
    for child in (copy / "integrations").iterdir():
        if child.is_dir():
            shutil.rmtree(child)
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    result = subprocess.run(  # noqa: S603 - our own interpreter and a fixed script
        [sys.executable, "-c", PROBE],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_the_app_starts_and_serves_no_correlation_routes_without_the_package(tmp_path) -> None:  # noqa: ANN001
    probe = _run_without_integrations(tmp_path)
    assert probe["discovered"] == []
    assert probe["registered"] == []
    assert not [p for p in probe["paths"] if p.startswith("/api/correlate")]
    assert probe["correlate_status"] == 404
    # The generic halves the integration used stay in core.
    assert "/api/links" in probe["paths"]
    assert "/api/timeline" in probe["paths"]
    assert "/api/calls" in probe["paths"]
    assert "/api/correlate" not in probe["description"]


def test_discovery_lists_only_packages(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    from app import integrations

    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "__init__.py").write_text("")
    (tmp_path / "loose.py").write_text("")
    monkeypatch.setattr(integrations, "__path__", [str(tmp_path)])
    assert integrations.discover() == ["alpha"]
