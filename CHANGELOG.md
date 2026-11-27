# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-27

### Added

- `lint` — 18 static checks (MD001–MD018) over MCP client config files, with
  `text`, `json` and `markdown` output.
- `probe` — real stdio handshake (`initialize` → `notifications/initialized` →
  `tools/list`) with per-step timing, tool inventory and captured stderr.
- Streamable-HTTP `initialize` probe for remote `url` servers, including
  `text/event-stream` response parsing.
- Windows batch-shim handling: `.cmd`/`.bat` commands are launched through
  `cmd /c` so they can start from a non-shell parent.
- Reader-thread I/O with hard per-step timeouts, so a silent server cannot hang
  the run.
- `--strict` mode that promotes warnings to a failing exit code for CI.
- `demo_server.py` fixture and 19 unittest cases covering lint rules, handshake
  success, timeout and missing-binary paths.
