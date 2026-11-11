"""The Caddyfile template, and the checked-in artifacts it is the source of truth for.

No database and no network: :mod:`app.edge.render` is pure by construction, which is the
point of keeping it apart from the admin-API client.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from app.config import Settings
from app.edge.render import (
    ADMIN_LISTEN,
    LOG_URI_REGEXP,
    LOG_URI_REPLACEMENT,
    UPSTREAM,
    acme_directory,
    checked_in_artifacts,
    config_hash,
    missing_requirements,
    normalize_domains,
    render_caddyfile,
    tls_challenge,
    tls_is_effective,
)
from app.log_redact import redact

#: The repository root: ``backend/tests/`` -> ``backend/`` -> the tree.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _settings(**values: object) -> Settings:
    """A :class:`Settings` with no ``.env`` read, so a developer's file cannot skew a test."""
    base = {
        "tls_enabled": True,
        "tls_domains": "keyup.example.com",
        "tls_cloudflare_api_token": "cf-token-value",
        "tls_acme_email": "nobody@example.com",
        "tls_acme_ca": "production",
        "tls_redirect_http": True,
    }
    return Settings(_env_file=None, **{**base, **values})


# ------------------------------------------------------------------- the four variants ---


def test_the_four_combinations_render_the_configuration_each_one_names() -> None:
    """tls x redirect.  With TLS off the redirect switch has nothing to act on."""
    for redirect in (True, False):
        plain = render_caddyfile(_settings(tls_enabled=False, tls_redirect_http=redirect))
        assert "auto_https off" in plain
        assert "acme_ca" not in plain
        assert ":80 {" in plain
        assert "redir https://" not in plain

    redirecting = render_caddyfile(_settings(tls_redirect_http=True))
    assert "redir https://{host}{uri} 308" in redirecting
    # Both modes switch Caddy's synthesised redirect off; the explicit :80 block decides.
    assert "auto_https disable_redirects" in redirecting
    # The two exemptions that make a cutover safe: the health probe and the recorder push.
    assert "handle /health {" in redirecting
    assert "handle /api/* {" in redirecting

    both = render_caddyfile(_settings(tls_redirect_http=False))
    assert "redir https://" not in both
    # Without this, Caddy synthesises its own :80 -> :443 redirect for the named host and
    # the catch-all below never sees the request — the whole mode would be a no-op.
    assert "auto_https disable_redirects" in both
    # https is still served: this is "and also http", not "instead of".
    assert "keyup.example.com {" in both
    assert "dns cloudflare" in both


def test_every_variant_exposes_the_admin_api_on_the_compose_network() -> None:
    """The admin API is what makes the Admin page able to configure TLS at all."""
    for enabled in (True, False):
        for redirect in (True, False):
            text = render_caddyfile(_settings(tls_enabled=enabled, tls_redirect_http=redirect))
            assert f"admin {ADMIN_LISTEN}" in text
            assert "admin off" not in text


def test_a_half_filled_configuration_renders_plain_http_rather_than_a_broken_site() -> None:
    """The same fallback ``entrypoint.sh`` makes: a missing piece must not take :80 down.

    The token is a requirement of DNS-01 only, so its case pins ``tls_challenge=dns``: under
    ``auto`` a blank token is not "missing", it selects HTTP-01.
    """
    for blank in ("tls_domains", "tls_acme_email", "tls_cloudflare_api_token"):
        for challenge in ("auto", "dns", "http"):
            if blank == "tls_cloudflare_api_token" and challenge != "dns":
                continue
            settings = _settings(**{blank: "", "tls_challenge": challenge})
            assert not tls_is_effective(settings), (blank, challenge)
            assert missing_requirements(settings) == [blank], (blank, challenge)
            assert "acme_ca" not in render_caddyfile(settings), (blank, challenge)


# ------------------------------------------------------------------ the ACME challenge ---


@pytest.mark.parametrize(
    ("challenge", "token", "expected"),
    [
        ("auto", "cf-token-value", "dns"),
        ("auto", "", "http"),
        ("", "cf-token-value", "dns"),
        ("bogus", "", "http"),
        ("dns", "", "dns"),
        ("DNS", "cf-token-value", "dns"),
        ("http", "cf-token-value", "http"),
        (" Http ", "", "http"),
    ],
)
def test_the_challenge_resolves_auto_by_whether_a_token_is_set(
    challenge: str, token: str, expected: str
) -> None:
    assert tls_challenge(_settings(tls_challenge=challenge, tls_cloudflare_api_token=token)) == expected


def test_the_challenge_defaults_to_auto_and_reads_its_environment_name() -> None:
    assert Settings(_env_file=None).tls_challenge == "auto"
    assert Settings(_env_file=None, KEYUP_TLS_CHALLENGE="http").tls_challenge == "http"


def test_a_deployment_with_a_token_renders_exactly_what_it_did_before_http01() -> None:
    """[PROD]: production has a token and pins ``dns``; ``auto`` must agree with the pin.

    The pushed configuration's hash is what the worker's reconcile compares, so any byte of
    difference here would re-push (and re-render) production's edge on the next pass.
    """
    for redirect in (True, False):
        auto = render_caddyfile(_settings(tls_redirect_http=redirect))
        pinned = render_caddyfile(_settings(tls_redirect_http=redirect, tls_challenge="dns"))
        assert auto == pinned
        assert "dns cloudflare cf-token-value" in auto
        assert "resolvers 1.1.1.1 1.0.0.1" in auto


def test_http01_needs_no_token_and_never_names_a_dns_provider() -> None:
    for redirect in (True, False):
        for token in ("", "cf-token-value"):
            settings = _settings(
                tls_challenge="http" if token else "auto",
                tls_cloudflare_api_token=token,
                tls_redirect_http=redirect,
            )
            assert tls_is_effective(settings)
            assert missing_requirements(settings) == []
            text = render_caddyfile(settings)
            assert "tls nobody@example.com\n" in text
            assert "dns cloudflare" not in text
            assert "resolvers" not in text
            assert "cf-token-value" not in text
            assert "keyup.example.com {" in text
            # The :80 block keeps its exemptions; Caddy answers the challenge ahead of it.
            assert "auto_https disable_redirects" in text
            assert ("redir https://{host}{uri} 308" in text) is redirect
            assert "handle /api/* {" in text if redirect else "respond /health" in text


def test_the_http01_artifacts_are_the_dns01_ones_without_the_dns_provider() -> None:
    artifacts = checked_in_artifacts()
    for suffix in ("", "-noredirect"):
        dns = artifacts[f"Caddyfile.tls{suffix}"]
        http = artifacts[f"Caddyfile.tls-http01{suffix}"]
        assert "{$CLOUDFLARE_API_TOKEN}" in dns
        assert "{$CLOUDFLARE_API_TOKEN}" not in http
        assert "\ttls {$ACME_EMAIL}\n" in http
        for placeholder in ("{$KEYUP_TLS_DOMAINS}", "{$ACME_CA}", "{$EDGE_TRUSTED_PROXIES}"):
            assert placeholder in http
        # Everything from the site block's body down is the same text.
        assert dns[dns.index('respond /health "ok" 200') :] == http[http.index('respond /health "ok" 200') :]


# ---- entrypoint.sh: the container's own choice, from the environment ----------------------


def _entrypoint(tmp_path: Path, **env: str) -> tuple[str, str]:
    """Run ``deploy/edge/entrypoint.sh`` with a stub ``caddy``; return (config, stderr)."""
    stub = tmp_path / "caddy"
    stub.write_text('#!/bin/sh\n[ "$1" = run ] && echo "$3"\nexit 0\n')
    stub.chmod(0o755)
    result = subprocess.run(  # noqa: S603 - a fixed script from this repository
        ["/bin/sh", str(REPO_ROOT / "deploy" / "edge" / "entrypoint.sh")],  # noqa: S607
        env={"PATH": f"{tmp_path}:{os.environ.get('PATH', '/usr/bin:/bin')}", **env},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip(), result.stderr


_TLS_ENV = {"KEYUP_TLS_DOMAINS": "keyup.example.com", "ACME_EMAIL": "nobody@example.com"}


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "Caddyfile.plain"),
        ({**_TLS_ENV, "CLOUDFLARE_API_TOKEN": "tok"}, "Caddyfile.tls"),
        ({**_TLS_ENV, "CLOUDFLARE_API_TOKEN": "tok", "KEYUP_TLS_CHALLENGE": "dns"}, "Caddyfile.tls"),
        (
            {**_TLS_ENV, "CLOUDFLARE_API_TOKEN": "tok", "KEYUP_TLS_REDIRECT_HTTP": "false"},
            "Caddyfile.tls-noredirect",
        ),
        (_TLS_ENV, "Caddyfile.tls-http01"),
        ({**_TLS_ENV, "KEYUP_TLS_REDIRECT_HTTP": "off"}, "Caddyfile.tls-http01-noredirect"),
        ({**_TLS_ENV, "CLOUDFLARE_API_TOKEN": "tok", "KEYUP_TLS_CHALLENGE": "HTTP"}, "Caddyfile.tls-http01"),
        # DNS-01 asked for without a token, and any TLS without an email: plain, not broken.
        ({**_TLS_ENV, "KEYUP_TLS_CHALLENGE": "dns"}, "Caddyfile.plain"),
        ({"KEYUP_TLS_DOMAINS": "keyup.example.com", "KEYUP_TLS_CHALLENGE": "http"}, "Caddyfile.plain"),
        # Production's shape: TLS lives in the settings table, the environment's names are blank.
        (
            {"KEYUP_TLS_DOMAINS": "", "CLOUDFLARE_API_TOKEN": "", "KEYUP_TLS_CHALLENGE": "dns"},
            "Caddyfile.plain",
        ),
    ],
)
def test_the_entrypoint_picks_the_file_the_api_would_render(
    tmp_path: Path, env: dict[str, str], expected: str
) -> None:
    config, _ = _entrypoint(tmp_path, **env)
    assert config == f"/etc/caddy/{expected}"
    assert expected in checked_in_artifacts()
    # And it is the same configuration the API's own resolution arrives at.
    settings = _settings(
        tls_enabled=bool(env.get("KEYUP_TLS_DOMAINS")),
        tls_domains=env.get("KEYUP_TLS_DOMAINS", ""),
        tls_acme_email=env.get("ACME_EMAIL", ""),
        tls_cloudflare_api_token=env.get("CLOUDFLARE_API_TOKEN", ""),
        tls_challenge=env.get("KEYUP_TLS_CHALLENGE", "auto"),
        tls_redirect_http=env.get("KEYUP_TLS_REDIRECT_HTTP", "true").lower() not in {"false", "off"},
    )
    if not tls_is_effective(settings):
        assert expected == "Caddyfile.plain"
    else:
        dns = tls_challenge(settings) == "dns"
        assert ("http01" not in expected) is dns
        assert ("noredirect" in expected) is (not settings.tls_redirect_http)


def test_the_dockerfile_ships_every_artifact() -> None:
    dockerfile = (REPO_ROOT / "deploy" / "edge" / "Dockerfile").read_text()
    for name in checked_in_artifacts():
        assert re.search(rf"^COPY {re.escape(name)}\s+/etc/caddy/{re.escape(name)}$", dockerfile, re.M), name


def test_the_token_never_reaches_a_placeholder_render() -> None:
    """The checked-in artifacts are in a git repository; the token is not."""
    for text in checked_in_artifacts().values():
        assert "cf-token-value" not in text
    assert "cf-token-value" in render_caddyfile(_settings())


def test_the_domains_are_normalized_into_one_site_block() -> None:
    text = render_caddyfile(_settings(tls_domains="  a.example.com\tb.example.com\n"))
    assert "a.example.com b.example.com {" in text
    assert normalize_domains("a.example.com, b.example.com") == ["a.example.com", "b.example.com"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("production", "https://acme-v02.api.letsencrypt.org/directory"),
        ("staging", "https://acme-staging-v02.api.letsencrypt.org/directory"),
        ("", "https://acme-v02.api.letsencrypt.org/directory"),
        ("https://acme.example.test/dir", "https://acme.example.test/dir"),
    ],
)
def test_the_ca_enum_maps_onto_an_acme_directory(value: str, expected: str) -> None:
    assert acme_directory(value) == expected
    assert f"acme_ca {expected}" in render_caddyfile(_settings(tls_acme_ca=value))


def test_the_environments_acme_ca_url_still_selects_the_right_directory() -> None:
    """``ACME_CA`` was a URL before it was an enum, and deployments still set it that way."""
    staging = "https://acme-staging-v02.api.letsencrypt.org/directory"
    assert Settings(_env_file=None, ACME_CA=staging).tls_acme_ca == "staging"
    assert Settings(_env_file=None, ACME_CA="").tls_acme_ca == "production"


def test_the_environments_names_still_reach_their_fields() -> None:
    """The bootstrap contract: the edge's own variables configure the API's view too."""
    settings = Settings(
        _env_file=None,
        KEYUP_TLS_DOMAINS="a.example.com",
        CLOUDFLARE_API_TOKEN="tok",
        ACME_EMAIL="a@example.com",
        KEYUP_TLS_REDIRECT_HTTP="false",
    )
    assert settings.tls_domains == "a.example.com"
    assert settings.tls_cloudflare_api_token == "tok"
    assert settings.tls_acme_email == "a@example.com"
    assert settings.tls_redirect_http is False


def test_the_hash_changes_with_the_configuration_and_not_with_the_clock() -> None:
    first = config_hash(render_caddyfile(_settings()))
    assert first == config_hash(render_caddyfile(_settings()))
    assert first != config_hash(render_caddyfile(_settings(tls_redirect_http=False)))


# ------------------------------------------------------------- credentials in the log ---


def _every_render() -> dict[str, str]:
    """Every configuration the edge can run: the four literal pushes and the three files."""
    renders = {
        f"literal tls={enabled} redirect={redirect}": render_caddyfile(
            _settings(tls_enabled=enabled, tls_redirect_http=redirect)
        )
        for enabled in (True, False)
        for redirect in (True, False)
    }
    renders.update(
        {
            f"literal http01 redirect={redirect}": render_caddyfile(
                _settings(tls_challenge="http", tls_redirect_http=redirect)
            )
            for redirect in (True, False)
        }
    )
    return {**renders, **checked_in_artifacts()}


def _caddy_mask(uri: str) -> str:
    """What Caddy's ``regexp`` log filter does to ``request>uri``: Go's ``ReplaceAllString``.

    Go writes a group as ``${n}``; Python as ``\\g<n>``.  Both expand a group that did not
    take part in the match to the empty string, which is what lets one alternation carry
    both the feed-path branch and the query branch.
    """
    return re.sub(LOG_URI_REGEXP, re.sub(r"\$\{(\d+)\}", r"\\g<\1>", LOG_URI_REPLACEMENT), uri)


def test_every_configuration_masks_the_request_uri_in_its_logs() -> None:
    """The error log records ``request>uri`` whenever the upstream is down.

    The filter has to sit on the *default* logger in the global options block: a site-level
    ``log`` directive only filters that site's access log and leaves ``http.log.error`` (its
    ``.log0`` child) unfiltered - measured against Caddy 2.11.4.
    """
    line = f"request>uri regexp `{LOG_URI_REGEXP}` `{LOG_URI_REPLACEMENT}`"
    for name, text in _every_render().items():
        assert text.count(line) == 1, name
        assert text.count("log default {") == 1, name
        assert "wrap json" in text, name
        # Inside the global options block: before the first site block opens.
        sites = ("\n:80 {", "\n{$KEYUP_TLS_DOMAINS} {", "\nkeyup.example.com {")
        first_site = min(i for i in (text.find(site) for site in sites) if i >= 0)
        assert text.index("log default {") < first_site, name
        # No second, unfiltered logger and no access log to leak through.
        assert "\tlog {" not in text, name


@pytest.mark.parametrize(
    "uri",
    [
        "/api/upload/flow?key=kup_TEST&x=1",
        "/api/upload/flow?x=1&key=kup_TEST",
        "/api/recorder/stream?instance=a&key=s3cret",
        "/api/recorder/spectrum?key=kup_a&token=t0k&site=3",
        "/api/feeds/Zq3_x-9aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789.rss",
        "/api/feeds/Zq3_x-9aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789.atom?limit=5",
        "/api/calls/12/audio?raw=1&feed=Zq3_x-9aBcDe&x=2",
        "/api/calls/1/audio?feed=abc&key=kup_x",
        # left alone
        "/api/feeds/preset/emergency.rss",
        "/api/feeds/presets",
        "/api/feeds/12/rotate",
        "/api/calls?monkey=1&feedback=2&keyword=fire",
        "/health",
    ],
)
def test_the_edge_masks_exactly_what_the_api_and_nginx_mask(uri: str) -> None:
    """One mask for three logs: Caddy's pattern agrees with ``app.log_redact`` on every case."""
    assert _caddy_mask(uri) == redact(uri)
    for secret in ("kup_TEST", "s3cret", "kup_a", "t0k", "Zq3_x", "kup_x"):
        assert secret not in _caddy_mask(uri)


def test_the_mask_keeps_the_other_query_parameters_in_order() -> None:
    """Why ``regexp`` and not Caddy's ``query`` filter: that one re-encodes and sorts."""
    assert _caddy_mask("/api/upload/flow?x=1&key=kup_TEST&a=2") == "/api/upload/flow?x=1&key=REDACTED&a=2"


# -------------------------------------------------------------------- the upstream name ---


def test_the_upstream_is_a_name_that_cannot_resolve_outside_docker() -> None:
    """``web`` is a delegated TLD (127.0.53.53, loopback in the edge) - never proxy to it.

    A hyphen keeps the label out of the public root (ASCII TLDs are letters only) and the
    trailing dot keeps the resolver from appending a ``search`` domain.
    """
    host, _, port = UPSTREAM.rpartition(":")
    assert port == "80"
    assert host.endswith("."), "an unrooted name gets the search domains appended"
    label = host.rstrip(".")
    assert "." not in label and "-" in label and not label.startswith("xn--")
    for name, text in _every_render().items():
        proxies = re.findall(r"reverse_proxy (\S+) \{", text)
        assert proxies, name
        assert set(proxies) == {UPSTREAM}, name
        assert "web:80" not in text.replace(UPSTREAM, ""), name


def test_compose_gives_web_the_alias_the_edge_proxies_to() -> None:
    """Without the alias every request is a 502: the name and the alias must move together."""
    compose = yaml.safe_load((REPO_ROOT / "compose.yaml").read_text())
    aliases = compose["services"]["web"]["networks"]["default"]["aliases"]
    assert UPSTREAM.rpartition(":")[0].rstrip(".") in aliases
    # The edge and web share that network (compose's implicit default).
    edge_networks = compose["services"]["edge"].get("networks") or {"default": None}
    assert "default" in edge_networks


# --------------------------------------------------------------- the checked-in copies ---


def test_checked_in_caddyfiles_match_renderer() -> None:
    """``deploy/edge/Caddyfile.*`` are artifacts of this module, not a second template.

    They have to exist on disk — the container starts from them, with no API to ask — and
    they have to be the same thing the API pushes over the admin API, or a deploy and an
    Apply would configure the proxy differently.  ``python -m app.edge`` regenerates them.
    """
    for name, expected in checked_in_artifacts().items():
        path = REPO_ROOT / "deploy" / "edge" / name
        assert path.is_file(), f"{path} is missing; run `python -m app.edge`"
        assert path.read_text() == expected, (
            f"{path} has drifted from app/edge/render.py; run `python -m app.edge`"
        )


def test_the_artifacts_use_env_placeholders_the_entrypoint_supplies() -> None:
    tls = checked_in_artifacts()["Caddyfile.tls"]
    for placeholder in ("{$KEYUP_TLS_DOMAINS}", "{$ACME_CA}", "{$ACME_EMAIL}", "{$CLOUDFLARE_API_TOKEN}"):
        assert placeholder in tls
    assert "{$" not in checked_in_artifacts()["Caddyfile.plain"]


# ---- the visitor's address through a Cloudflare Tunnel -----------------------------------


def _reverse_proxies(text: str) -> list[str]:
    return re.findall(r"reverse_proxy [^\n]+\{.*?\n\t+\}", text, flags=re.DOTALL)


def test_a_named_connector_is_trusted_for_the_cloudflare_header() -> None:
    text = render_caddyfile(_settings(edge_trusted_proxies="192.0.2.10"))
    assert "trusted_proxies static 192.0.2.10\n" in text
    assert "client_ip_headers Cf-Connecting-Ip" in text


def test_with_no_connector_named_only_loopback_is_trusted() -> None:
    for text in _every_render().values():
        if "{$EDGE_TRUSTED_PROXIES}" not in text:
            assert "trusted_proxies static 127.0.0.1/32\n" in text


def test_a_value_that_is_not_an_address_cannot_reach_the_caddyfile() -> None:
    text = render_caddyfile(_settings(edge_trusted_proxies="192.0.2.10 } evil 198.51.100.0/24"))
    assert "trusted_proxies static 192.0.2.10 198.51.100.0/24\n" in text
    assert "evil" not in text


def test_every_proxy_hands_nginx_one_address_so_the_hop_count_stays_two() -> None:
    for name, text in _every_render().items():
        proxies = _reverse_proxies(text)
        assert proxies, name
        for block in proxies:
            assert "header_up X-Forwarded-For {client_ip}" in block, name


def test_the_artifacts_take_the_connector_from_the_environment() -> None:
    artifacts = checked_in_artifacts()
    for name in (
        "Caddyfile.tls",
        "Caddyfile.tls-noredirect",
        "Caddyfile.tls-http01",
        "Caddyfile.tls-http01-noredirect",
    ):
        assert "trusted_proxies static {$EDGE_TRUSTED_PROXIES}" in artifacts[name], name
