# Changelog

All notable changes to Keyup are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). While the version is 0.x, a minor release may change
defaults or the API; such changes are listed under **Changed**.

## [0.1.1] - 2026-11-11

### Changed
- Calls from trunkd's analog conventional channels keep their `mode`, `decoder`, `tone` and signal fields;
  a system first created by such a call is recorded as NBFM, and Broadcastify Calls never receives analog calls.
  The call page shows the channel's mode and tone.
- The backup health check and the backup script now default to the same folder (`/srv/keyup/backups/db`).
- `OV_MODEL` defaults to `OpenVINO/whisper-medium-fp16-ov`.
- Documentation: generic migration guide, corrected commands, fuller recorder-scope route list.

## [0.1.0] - 2026-11-11

### Added
- First public release: call ingest from sdrtrunk and Rdio-Scanner-compatible recorders,
  transcription (local CPU, GPU or an OpenAI-compatible server), a REST and WebSocket API, the web
  archive, alerts, reconciliation of duplicate recordings, and optional exports to Broadcastify and
  OpenMHz and directory sync from RadioReference (all off by default).
- Licensed under AGPL-3.0-or-later; third-party licences are listed in `THIRD-PARTY.md`.
