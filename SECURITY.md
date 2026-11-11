# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub: open the repository's **Security** tab and
choose **Report a vulnerability**. Do not open a public issue or pull request for them.

Include what an attacker can do, the steps or a proof of concept, the Keyup version or commit, and
which settings are involved (for example, whether the archive is public, which token scopes are in
use, or whether HTTPS is terminated by Keyup's own edge or by a proxy in front of it).

The maintainer aims to acknowledge a report within a week. Once a fix is ready it is released and the advisory
published, crediting you unless you ask otherwise.

## Supported versions

Only the latest release receives security fixes. Upgrade before reporting if you can.

## Scope

In scope: the backend API and WebSocket, authentication and token handling, the upload endpoints
recorders use, the web frontend, the bundled Caddy edge configuration and the Docker Compose files
as shipped.

Out of scope: weaknesses that need an administrator account or shell access to the host, problems in
third-party services Keyup can push to (Broadcastify, OpenMHz, RadioReference), and deployments that
have changed the shipped defaults in a way the documentation warns against (for example, exposing
the database port).
