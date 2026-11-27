# mcp-doctor

**Diagnose MCP server configs before your agent client does.** Static lint for the file, real stdio handshake for the process. Single file, zero dependencies, Python 3.8+.

Every MCP client — Claude Desktop, Cursor, VS Code, Qoder, WorkBuddy — fails the same three ways: a `command` that is not on `PATH`, a relative script path that resolves against the wrong working directory, and an `npx` invocation that sits forever on `Ok to proceed? (y)`. The client shows you nothing: no log, no error, just a server that never appears.

`mcp-doctor` finds those before you restart the app for the fifth time.

```console
$ python mcp_doctor.py probe demo_config.json
mcp-doctor 1.0.0
config  demo_config.json

servers (1)
  - demo  [stdio]  python -u demo_server.py

findings (0)  config looks healthy

probe (1)
  PASS demo  [stdio]
      [+] spawn             13.3 ms  process started
      [+] initialize       130.3 ms  protocol 2024-11-05
      [+] tools/list         0.1 ms  2 tool(s)
      server: mcp-doctor-demo 1.0.0
      tools (2):
        * echo(text)
          Return the text you pass in unchanged.
        * add(a, b)
          Add two numbers and return the sum.

summary  0 error(s), 0 warning(s), 0/1 probe(s) failed
```

## Install

Nothing to install. One file:

```bash
curl -O https://raw.githubusercontent.com/AndreWaRavage/mcp-doctor/main/mcp_doctor.py
python mcp_doctor.py --version
```

Or clone and run from the repo root:

```bash
git clone https://github.com/AndreWaRavage/mcp-doctor.git
cd mcp-doctor
python mcp_doctor.py lint demo_config.json
```

## Usage

```
mcp-doctor lint  <config> [--format text|json|markdown] [--server NAME] [--strict]
mcp-doctor probe <config> [--timeout 20] [--max-servers N] [--format ...]
```

`lint` reads the file and reports. `probe` runs `lint` first, then spawns each server and performs the real MCP handshake: `initialize` → `notifications/initialized` → `tools/list`. Every step is timed, stderr is captured, and the process is always reaped.

Point it at the file your client actually reads:

| Client | Path |
| --- | --- |
| Claude Desktop (Windows) | `%APPDATA%\Claude\claude_desktop_config.json` |
| Claude Desktop (macOS) | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Cursor | `~/.cursor/mcp.json` |
| VS Code | `.vscode/mcp.json` |
| Qoder / WorkBuddy | `~/.config/<product>/mcp.json` |

Supported dialects: `{"mcpServers": {...}}` (Claude Desktop, Cursor, Qoder), `{"servers": {...}}` (VS Code), and `{"mcp": {"servers": {...}}}`.

## Checks

| Code | Severity | What it catches |
| --- | --- | --- |
| MD001 | error | config file missing or unreadable |
| MD002 | error | invalid JSON, with line and column |
| MD003 | error/warn | no `mcpServers` / `servers` block, or an empty one |
| MD004 | error | server entry is not an object |
| MD005 | warn | both `command` and `url` set — clients disagree on which wins |
| MD006 | error | neither `command` nor `url` |
| MD007 | error | `url` is not `http(s)://` |
| MD008 | info | remote header contains a `${VAR}` placeholder some clients do not expand |
| MD009 | error | `command` not on `PATH`, with a `difflib` "did you mean" suggestion |
| MD010 | info | command resolves to a `.cmd`/`.bat` shim — the classic Windows launch failure |
| MD011 | error | `cwd` does not exist |
| MD012 | error | a path-shaped argument does not exist (relative paths resolve against the *client's* cwd, not yours) |
| MD013 | warn | `env` value references an unset `${VAR}` |
| MD014 | warn | `env` value is an empty string |
| MD015 | warn | `npx`/`bunx`/`uvx` without `-y` — hangs on the install prompt |
| MD016 | warn | two servers with an identical launch line |
| MD017 | info | unknown keys the client will silently ignore |
| MD018 | error | `--server NAME` does not exist in the file |

## Probe behaviour

- Spawns with the declared `env` merged over the current environment, in the declared `cwd`.
- Sends `protocolVersion: 2024-11-05`, reports whatever the server negotiates back.
- Default 20 s timeout per step (`--timeout`), enforced with a reader thread so a silent server cannot hang the run.
- On Windows, `.cmd`/`.bat` commands are launched through `cmd /c` automatically, because `CreateProcess` cannot execute a batch shim directly. This is the single most common cause of "MCP server works in terminal, dies in the app".
- Always terminates the child and closes every pipe, including on timeout.
- Remote `url` servers get a Streamable-HTTP `initialize` POST; `text/event-stream` responses are parsed for the first JSON-RPC message.

## In CI

Exit codes: `0` clean, `1` any error finding or failed probe, `2` usage error. `--strict` also fails on warnings.

```yaml
- run: python mcp_doctor.py lint mcp.json --format json > mcp-report.json
- run: python mcp_doctor.py probe mcp.json --timeout 30
```

`--format json` emits machine-readable findings; `--format markdown` emits a table you can paste into a PR or an issue.

## Demo

`demo_config.json` points at `demo_server.py`, a 100-line MCP server with two tools (`echo`, `add`) used as the fixture for the test suite:

```bash
python mcp_doctor.py probe demo_config.json     # should PASS
python -m unittest -v tests                     # 19 tests
```

## Limitations

- `lint` path detection is heuristic: an argument is treated as a path only if it looks like one (starts with `.`, `/`, `~`, a drive letter, or contains a separator). Package names and flags are skipped.
- The HTTP probe performs `initialize` only. It does not open a session, list tools, or handle OAuth discovery flows.
- A passing probe proves the server starts and speaks the protocol. It does not prove that individual tool calls succeed.

## 中文说明

`mcp-doctor` 用来排查 MCP 服务器配置。MCP 客户端（Claude Desktop、Cursor、VS Code、Qoder、WorkBuddy）出问题时的表现几乎都一样：服务器列表里干脆没有它，没有任何报错。原因通常只有三个 —— 命令不在 `PATH` 里、脚本用了相对路径而工作目录不是你以为的那个、`npx` 卡在 `Ok to proceed?` 的交互提示上。

`lint` 静态检查配置文件（18 条规则），`probe` 真正把进程拉起来走一遍 `initialize` → `tools/list` 握手，报告工具清单、每一步耗时和服务器写到 stderr 的内容。

单文件、零依赖、Python 3.8+。Windows 下会自动用 `cmd /c` 包装 `.cmd`/`.bat` 命令 —— 这是"MCP 在终端能跑、在应用里起不来"最常见的原因。

## License

MIT
