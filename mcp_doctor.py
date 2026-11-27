#!/usr/bin/env python3
"""
mcp-doctor - diagnose MCP (Model Context Protocol) server configuration.

Two jobs, both offline and zero-dependency:

  lint   static analysis of a client config file (claude_desktop_config.json,
         mcp.json, .cursor/mcp.json, .vscode/mcp.json, ...). Catches the
         failures that actually bite people in the wild: broken JSON, command
         not on PATH, missing script paths, unset ${ENV} placeholders, `npx`
         invocations that block forever on an install prompt, .cmd/.bat
         launchers that some clients cannot spawn on Windows.

  probe  real stdio handshake. Spawns the server, sends `initialize`,
         `notifications/initialized` and `tools/list`, then reports the tool
         inventory, per-step latency and anything the server wrote to stderr.
         Remote `url` servers get a Streamable-HTTP initialize instead.

Exit code is 0 when clean, 1 when an error-severity finding exists, so the
tool drops straight into CI.

Pure standard library, single file, Python 3.8+.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "mcp-doctor", "version": VERSION}

CONFIG_WRAPPERS = ("mcpServers", "servers")

SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}

ANSI = {
    "error": "\033[31m",
    "warn": "\033[33m",
    "info": "\033[36m",
    "ok": "\033[32m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    server: Optional[str] = None
    hint: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "server": self.server,
            "message": self.message,
            "hint": self.hint,
        }


@dataclass
class ServerSpec:
    name: str
    raw: Dict[str, Any]
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    url: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)
    transport: str = "stdio"

    @property
    def signature(self) -> str:
        return " ".join([self.command or ""] + self.args).strip()


@dataclass
class ProbeStep:
    name: str
    ok: bool
    elapsed_ms: float
    detail: str = ""


@dataclass
class ProbeResult:
    server: str
    transport: str
    ok: bool
    steps: List[ProbeStep] = field(default_factory=list)
    tools: List[Dict[str, Any]] = field(default_factory=list)
    server_info: Dict[str, Any] = field(default_factory=dict)
    protocol_version: Optional[str] = None
    stderr: str = ""
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "server": self.server,
            "transport": self.transport,
            "ok": self.ok,
            "protocol_version": self.protocol_version,
            "server_info": self.server_info,
            "tools": self.tools,
            "steps": [
                {
                    "name": s.name,
                    "ok": s.ok,
                    "elapsed_ms": round(s.elapsed_ms, 1),
                    "detail": s.detail,
                }
                for s in self.steps
            ],
            "stderr": self.stderr,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# config loading
# --------------------------------------------------------------------------- #


def find_config_wrapper(data: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return (servers mapping, wrapper key) for the config dialects in use."""
    if not isinstance(data, dict):
        return None, None
    for key in CONFIG_WRAPPERS:
        value = data.get(key)
        if isinstance(value, dict):
            return value, key
    # VS Code / some forks nest under {"mcp": {"servers": ...}}
    nested = data.get("mcp")
    if isinstance(nested, dict):
        for key in CONFIG_WRAPPERS:
            value = nested.get(key)
            if isinstance(value, dict):
                return value, "mcp." + key
    return None, None


def load_config(path: str) -> Tuple[Optional[dict], List[Finding]]:
    findings: List[Finding] = []
    if not os.path.exists(path):
        findings.append(
            Finding(
                "MD001",
                "error",
                f"config file not found: {path}",
                hint="pass the real path, e.g. ~/.config/Qoder/mcp.json",
            )
        )
        return None, findings
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        findings.append(
            Finding(
                "MD002",
                "error",
                f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}",
                hint="trailing commas and // comments are not valid JSON",
            )
        )
        return None, findings
    except OSError as exc:
        findings.append(Finding("MD001", "error", f"cannot read config: {exc}"))
        return None, findings

    servers, wrapper = find_config_wrapper(data)
    if servers is None:
        findings.append(
            Finding(
                "MD003",
                "error",
                "no server block found; expected a top-level \"mcpServers\" or \"servers\" object",
                hint="Claude Desktop / Cursor / Qoder use mcpServers, VS Code uses servers",
            )
        )
        return data, findings
    if not servers:
        findings.append(Finding("MD003", "warn", f"\"{wrapper}\" is empty; no servers configured"))
    return data, findings


def to_specs(servers: Dict[str, Any], findings: List[Finding]) -> List[ServerSpec]:
    specs: List[ServerSpec] = []
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            findings.append(
                Finding(
                    "MD004",
                    "error",
                    f"server entry must be an object, got {type(entry).__name__}",
                    server=name,
                )
            )
            continue
        raw_type = entry.get("type") or entry.get("transport")
        url = entry.get("url") or entry.get("endpoint") or entry.get("serverUrl")
        command = entry.get("command")
        args = entry.get("args") or []
        spec = ServerSpec(
            name=name,
            raw=entry,
            command=command if isinstance(command, str) else None,
            args=[str(a) for a in args] if isinstance(args, list) else [],
            env={str(k): str(v) for k, v in (entry.get("env") or {}).items()}
            if isinstance(entry.get("env"), dict)
            else {},
            cwd=entry.get("cwd") if isinstance(entry.get("cwd"), str) else None,
            url=url if isinstance(url, str) else None,
            headers={str(k): str(v) for k, v in (entry.get("headers") or {}).items()}
            if isinstance(entry.get("headers"), dict)
            else {},
        )
        if spec.url and not spec.command:
            spec.transport = "http" if str(raw_type or "").lower() not in ("sse",) else "sse"
        elif spec.url and spec.command:
            spec.transport = "stdio"
            findings.append(
                Finding(
                    "MD005",
                    "warn",
                    "both \"command\" and \"url\" are set; clients disagree on which wins",
                    server=name,
                    hint="drop one of them - use command for local, url for remote",
                )
            )
        specs.append(spec)
    return specs


# --------------------------------------------------------------------------- #
# lint
# --------------------------------------------------------------------------- #

PATHISH = re.compile(r"^(\.{1,2}[/\\]|[/\\]|~[/\\]|[A-Za-z]:[/\\]|.*[/\\].*)$")
ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
INTERACTIVE_FLAGS = ("-y", "--yes", "--assume-yes")


def _looks_like_path(token: str) -> bool:
    if not token or token.startswith("-"):
        return False
    return bool(PATHISH.match(token))


def _expand(token: str) -> str:
    return os.path.expanduser(os.path.expandvars(token))


def _resolve_command(command: str, cwd: Optional[str]) -> Optional[str]:
    if os.path.isabs(command) or os.sep in command or (os.altsep and os.altsep in command):
        candidate = _expand(command)
        if not os.path.isabs(candidate) and cwd:
            candidate = os.path.join(cwd, candidate)
        return candidate if os.path.exists(candidate) else None
    return shutil.which(command)


def lint(specs: Iterable[ServerSpec], findings: List[Finding]) -> None:
    seen_signatures: Dict[str, str] = {}

    for spec in specs:
        name = spec.name

        if not spec.command and not spec.url:
            findings.append(
                Finding(
                    "MD006",
                    "error",
                    "server has neither \"command\" nor \"url\"",
                    server=name,
                    hint="a stdio server needs command (+args), a remote server needs url",
                )
            )
            continue

        if spec.transport in ("http", "sse") and spec.url:
            if not re.match(r"^https?://", spec.url):
                findings.append(
                    Finding(
                        "MD007",
                        "error",
                        f"url does not start with http(s): {spec.url}",
                        server=name,
                    )
                )
            for key in spec.headers:
                if ENV_PLACEHOLDER.search(spec.headers[key]):
                    findings.append(
                        Finding(
                            "MD008",
                            "info",
                            f"header \"{key}\" contains an environment placeholder; "
                            "clients that do not expand \\${} will send it literally",
                            server=name,
                        )
                    )

        if spec.command:
            resolved = _resolve_command(spec.command, spec.cwd)
            if resolved is None:
                suggestions = difflib.get_close_matches(
                    os.path.basename(spec.command), _path_commands(), n=1, cutoff=0.6
                )
                hint = "install it, or use an absolute path"
                if suggestions:
                    hint = f'did you mean "{suggestions[0]}"? ' + hint
                findings.append(
                    Finding(
                        "MD009",
                        "error",
                        f"command not found: {spec.command}",
                        server=name,
                        hint=hint,
                    )
                )
            elif os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
                findings.append(
                    Finding(
                        "MD010",
                        "info",
                        f"command resolves to a batch shim ({os.path.basename(resolved)}); "
                        "clients that spawn without cmd.exe cannot start it on Windows",
                        server=name,
                        hint="this is the classic Windows npx/uvx failure; prefer npx.cmd "
                        "or wrap with cmd /c",
                    )
                )
            elif spec.cwd and not os.path.isdir(_expand(spec.cwd)):
                findings.append(
                    Finding(
                        "MD011",
                        "error",
                        f"cwd does not exist: {spec.cwd}",
                        server=name,
                    )
                )

        if spec.args:
            for arg in spec.args:
                if not _looks_like_path(arg):
                    continue
                if "${" in arg or "$" in arg:
                    continue
                candidate = _expand(arg)
                if not os.path.isabs(candidate) and spec.cwd:
                    candidate = os.path.join(_expand(spec.cwd), candidate)
                if os.path.exists(candidate):
                    continue
                findings.append(
                    Finding(
                        "MD012",
                        "error",
                        f"path argument does not exist: {arg}",
                        server=name,
                        hint="relative paths resolve against the client's working directory, "
                        "not yours - use an absolute path",
                    )
                )

        for key, value in spec.env.items():
            for match in ENV_PLACEHOLDER.finditer(value):
                var = match.group(1) or match.group(2)
                if var not in os.environ:
                    findings.append(
                        Finding(
                            "MD013",
                            "warn",
                            f"env {key} references ${{{var}}} which is not set in this shell",
                            server=name,
                            hint="secrets are usually fine to leave unset here, but a "
                            "missing non-secret variable breaks the server at runtime",
                        )
                    )
            if value == "":
                findings.append(
                    Finding("MD014", "warn", f"env {key} is an empty string", server=name)
                )

        if _is_npx(spec.command) and not _has_interactive_flag(spec.args):
            findings.append(
                Finding(
                    "MD015",
                    "warn",
                    "npx without -y can block on the \"Ok to proceed?\" install prompt, "
                    "which looks like a hung server",
                    server=name,
                    hint="add -y to args, e.g. [\"-y\", \"@modelcontextprotocol/server-filesystem\"]",
                )
            )

        if spec.signature:
            previous = seen_signatures.get(spec.signature)
            if previous:
                findings.append(
                    Finding(
                        "MD016",
                        "warn",
                        f"identical launch line as server \"{previous}\"",
                        server=name,
                        hint="duplicate plugins are usually a copy-paste mistake",
                    )
                )
            else:
                seen_signatures[spec.signature] = name

        known = {
            "command",
            "args",
            "env",
            "cwd",
            "url",
            "headers",
            "type",
            "transport",
            "endpoint",
            "serverUrl",
            "disabled",
        }
        unknown = sorted(set(spec.raw) - known)
        if unknown:
            findings.append(
                Finding(
                    "MD017",
                    "info",
                    "unknown keys are ignored by most clients: " + ", ".join(unknown),
                    server=name,
                )
            )


def _path_commands() -> List[str]:
    names: List[str] = []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        try:
            for entry in os.scandir(directory):
                if entry.is_file():
                    names.append(entry.name)
        except (OSError, PermissionError):
            continue
    return names


def _is_npx(command: Optional[str]) -> bool:
    if not command:
        return False
    return os.path.basename(command).lower() in ("npx", "npx.cmd", "npx.exe", "bunx", "uvx")


def _has_interactive_flag(args: List[str]) -> bool:
    return any(a in INTERACTIVE_FLAGS for a in args)


# --------------------------------------------------------------------------- #
# stdio client
# --------------------------------------------------------------------------- #


class StdioError(RuntimeError):
    pass


class StdioClient:
    """Minimal newline-delimited JSON-RPC client for MCP over stdio."""

    def __init__(self, spec: ServerSpec, timeout: float = 20.0) -> None:
        self.spec = spec
        self.timeout = timeout
        self.proc: Optional[subprocess.Popen] = None
        self._out: "queue.Queue[Optional[str]]" = queue.Queue()
        self._err: List[str] = []
        self._next_id = 0
        self._noise: List[str] = []

    # -- lifecycle ---------------------------------------------------------- #

    def _argv(self) -> List[str]:
        assert self.spec.command
        argv = [self.spec.command] + self.spec.args
        if os.name == "nt":
            resolved = _resolve_command(self.spec.command, self.spec.cwd)
            if resolved and resolved.lower().endswith((".cmd", ".bat")):
                comspec = os.environ.get("COMSPEC", "cmd.exe")
                return [comspec, "/c"] + argv
        return argv

    def start(self) -> None:
        env = os.environ.copy()
        for key, value in self.spec.env.items():
            env[key] = os.path.expandvars(value)
        cwd = _expand(self.spec.cwd) if self.spec.cwd else None
        kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.proc = subprocess.Popen(
                self._argv(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
                **kwargs,
            )
        except FileNotFoundError as exc:
            raise StdioError(f"cannot launch {self.spec.command}: {exc}") from exc
        except OSError as exc:
            raise StdioError(f"launch failed: {exc}") from exc

        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self._out.put(line.rstrip("\r\n"))
        self._out.put(None)

    def _pump_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            line = line.rstrip("\r\n")
            if line:
                self._err.append(line)
                if len(self._err) > 200:
                    self._err.pop(0)

    def stop(self) -> None:
        if not self.proc:
            return
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    try:
                        self.proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
        except OSError:
            pass
        finally:
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            self.proc = None

    # -- rpc ---------------------------------------------------------------- #

    def _send(self, payload: Dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin or self.proc.poll() is not None:
            raise StdioError("server process is not running")
        try:
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise StdioError(f"cannot write to server stdin: {exc}") from exc

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def request(
        self, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        deadline = time.time() + (timeout if timeout is not None else self.timeout)
        self._next_id += 1
        rid = self._next_id
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise StdioError(
                    f"timed out after {timeout or self.timeout:.0f}s waiting for \"{method}\""
                )
            try:
                line = self._out.get(timeout=remaining)
            except queue.Empty:
                raise StdioError(
                    f"timed out after {timeout or self.timeout:.0f}s waiting for \"{method}\""
                ) from None
            if line is None:
                raise StdioError(f"server closed stdout while waiting for \"{method}\"")
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._noise.append(line[:200])
                continue
            if not isinstance(message, dict):
                continue
            if "id" in message and message.get("id") != rid:
                continue
            if "error" in message:
                err = message["error"]
                raise StdioError(f"{method} returned error {err.get('code')}: {err.get('message')}")
            if "id" in message:
                return message

    @property
    def noise(self) -> List[str]:
        return self._noise

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._err[-40:])


def probe_stdio(spec: ServerSpec, timeout: float) -> ProbeResult:
    result = ProbeResult(server=spec.name, transport="stdio", ok=False)
    client = StdioClient(spec, timeout=timeout)

    started = time.time()
    try:
        client.start()
        result.steps.append(
            ProbeStep("spawn", True, (time.time() - started) * 1000, "process started")
        )
    except StdioError as exc:
        result.steps.append(ProbeStep("spawn", False, (time.time() - started) * 1000, str(exc)))
        result.error = str(exc)
        return result

    try:
        began = time.time()
        init = client.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"roots": {"listChanged": False}},
                "clientInfo": CLIENT_INFO,
            },
            timeout=timeout,
        )
        elapsed = (time.time() - began) * 1000
        payload = init.get("result", {})
        result.server_info = payload.get("serverInfo", {}) or {}
        result.protocol_version = payload.get("protocolVersion")
        result.steps.append(
            ProbeStep("initialize", True, elapsed, f"protocol {result.protocol_version}")
        )

        client.notify("notifications/initialized")

        began = time.time()
        listing = client.request("tools/list", {}, timeout=timeout)
        elapsed = (time.time() - began) * 1000
        tools = listing.get("result", {}).get("tools", []) or []
        result.tools = [
            {
                "name": t.get("name"),
                "description": (t.get("description") or "").strip().split("\n")[0][:160],
                "params": sorted((t.get("inputSchema") or {}).get("properties", {}) or {}),
            }
            for t in tools
            if isinstance(t, dict)
        ]
        result.steps.append(
            ProbeStep("tools/list", True, elapsed, f"{len(result.tools)} tool(s)")
        )
        result.ok = True
    except StdioError as exc:
        result.steps.append(ProbeStep("handshake", False, 0.0, str(exc)))
        result.error = str(exc)
    finally:
        result.stderr = client.stderr_tail
        client.stop()

    return result


# --------------------------------------------------------------------------- #
# http probe
# --------------------------------------------------------------------------- #


def probe_http(spec: ServerSpec, timeout: float) -> ProbeResult:
    result = ProbeResult(server=spec.name, transport=spec.transport, ok=False)
    if not spec.url:
        result.error = "missing url"
        return result
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    headers.update({k: os.path.expandvars(v) for k, v in spec.headers.items()})
    request = urllib.request.Request(spec.url, data=body, headers=headers, method="POST")

    began = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        result.steps.append(
            ProbeStep("POST initialize", False, (time.time() - began) * 1000, f"HTTP {exc.code}")
        )
        result.error = (
            f"HTTP {exc.code} - a bare initialize POST often needs a session; "
            "401/403 usually means a missing Authorization header"
        )
        return result
    except (urllib.error.URLError, OSError) as exc:
        result.steps.append(
            ProbeStep("POST initialize", False, (time.time() - began) * 1000, str(exc))
        )
        result.error = str(exc)
        return result

    result.steps.append(
        ProbeStep("POST initialize", True, (time.time() - began) * 1000, f"HTTP {status}")
    )
    message = _first_jsonrpc(raw)
    if message is None:
        result.error = "response was not JSON-RPC (is this a Streamable-HTTP MCP endpoint?)"
        return result
    if "error" in message:
        result.error = f"initialize error: {message['error']}"
        return result
    payload = message.get("result", {})
    result.server_info = payload.get("serverInfo", {}) or {}
    result.protocol_version = payload.get("protocolVersion")
    result.ok = True
    return result


def _first_jsonrpc(raw: str) -> Optional[Dict[str, Any]]:
    text = raw.strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            candidate = json.loads(text)
        except json.JSONDecodeError:
            return None
        return candidate if isinstance(candidate, dict) else None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            chunk = line[5:].strip()
            try:
                candidate = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and ("result" in candidate or "error" in candidate):
                return candidate
    return None


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def _color(text: str, key: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{ANSI.get(key, '')}{text}{ANSI['reset']}"


def render_text(
    config_path: str,
    specs: List[ServerSpec],
    findings: List[Finding],
    probes: Optional[List[ProbeResult]],
    color: bool,
) -> str:
    lines: List[str] = []
    lines.append(_color(f"mcp-doctor {VERSION}", "bold", color))
    lines.append(_color(f"config  {config_path}", "dim", color))
    lines.append("")

    if specs:
        lines.append(f"servers ({len(specs)})")
        for spec in specs:
            transport = spec.transport
            launch = spec.url if transport != "stdio" else (spec.signature or "<unset>")
            lines.append(f"  - {spec.name}  [{transport}]  {launch}")
        lines.append("")

    ordered = sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.code))
    if ordered:
        lines.append(f"findings ({len(ordered)})")
        for finding in ordered:
            tag = {"error": "ERROR", "warn": "WARN ", "info": "INFO "}[finding.severity]
            colored = _color(tag, finding.severity, color)
            head = f"  {colored} {finding.code}"
            if finding.server:
                head += _color(f" [{finding.server}]", "dim", color)
            lines.append(f"{head}  {finding.message}")
            if finding.hint:
                lines.append(_color(f"         hint: {finding.hint}", "dim", color))
        lines.append("")
    else:
        lines.append(_color("findings (0)  config looks healthy", "ok", color))
        lines.append("")

    if probes is not None:
        lines.append(f"probe ({len(probes)})")
        for probe in probes:
            mark = _color("PASS", "ok", color) if probe.ok else _color("FAIL", "error", color)
            lines.append(f"  {mark} {probe.server}  [{probe.transport}]")
            for step in probe.steps:
                step_mark = "+" if step.ok else "x"
                detail = f"  {step.detail}" if step.detail else ""
                lines.append(
                    f"      [{step_mark}] {step.name:<14} {step.elapsed_ms:7.1f} ms{detail}"
                )
            if probe.server_info:
                name = probe.server_info.get("name", "?")
                version = probe.server_info.get("version", "?")
                lines.append(f"      server: {name} {version}")
            if probe.tools:
                lines.append(f"      tools ({len(probe.tools)}):")
                for tool in probe.tools:
                    params = ", ".join(tool["params"]) if tool["params"] else "-"
                    lines.append(f"        * {tool['name']}({params})")
                    if tool["description"]:
                        lines.append(_color(f"          {tool['description']}", "dim", color))
            if probe.error:
                lines.append(_color(f"      error: {probe.error}", "error", color))
            if probe.stderr:
                lines.append(_color("      stderr:", "dim", color))
                for line in probe.stderr.splitlines()[-10:]:
                    lines.append(_color(f"        | {line}", "dim", color))
        lines.append("")

    errors = sum(1 for f in findings if f.severity == "error")
    warns = sum(1 for f in findings if f.severity == "warn")
    failed = sum(1 for p in probes or [] if not p.ok)
    summary = f"summary  {errors} error(s), {warns} warning(s)"
    if probes is not None:
        summary += f", {failed}/{len(probes)} probe(s) failed"
    key = "error" if errors or failed else ("warn" if warns else "ok")
    lines.append(_color(summary, key, color))
    return "\n".join(lines)


def render_markdown(
    config_path: str,
    specs: List[ServerSpec],
    findings: List[Finding],
    probes: Optional[List[ProbeResult]],
) -> str:
    out = [f"# mcp-doctor report", "", f"`{config_path}`", ""]
    out.append("| server | transport | launch |")
    out.append("| --- | --- | --- |")
    for spec in specs:
        launch = spec.url if spec.transport != "stdio" else spec.signature
        out.append(f"| `{spec.name}` | {spec.transport} | `{launch}` |")
    out.append("")
    if findings:
        out.append("| severity | code | server | message |")
        out.append("| --- | --- | --- | --- |")
        for f in sorted(findings, key=lambda x: SEVERITY_ORDER.get(x.severity, 9)):
            out.append(f"| {f.severity} | {f.code} | {f.server or '-'} | {f.message} |")
        out.append("")
    if probes:
        out.append("| server | ok | initialize | tools | error |")
        out.append("| --- | --- | --- | --- | --- |")
        for p in probes:
            init = next((s for s in p.steps if s.name == "initialize"), None)
            init_ms = f"{init.elapsed_ms:.0f} ms" if init else "-"
            out.append(f"| `{p.server}` | {p.ok} | {init_ms} | {len(p.tools)} | {p.error or ''} |")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-doctor",
        description="Diagnose MCP server configs: lint the file, then probe the real handshake.",
    )
    parser.add_argument("--version", action="version", version=f"mcp-doctor {VERSION}")
    sub = parser.add_subparsers(dest="command")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("config", help="path to the MCP config file")
    common.add_argument(
        "--format", choices=("text", "json", "markdown"), default="text", help="output format"
    )
    common.add_argument("--no-color", action="store_true", help="disable ANSI colours")
    common.add_argument("--server", help="only analyse this server name")
    common.add_argument(
        "--strict", action="store_true", help="treat warnings as errors for the exit code"
    )

    lint_parser = sub.add_parser("lint", parents=[common], help="static analysis only")
    lint_parser.set_defaults(handler=cmd_lint)

    probe_parser = sub.add_parser(
        "probe", parents=[common], help="lint, then run the real initialize/tools handshake"
    )
    probe_parser.add_argument(
        "--timeout", type=float, default=20.0, help="per-step timeout in seconds (default 20)"
    )
    probe_parser.add_argument(
        "--max-servers", type=int, default=0, help="probe at most N servers (0 = all)"
    )
    probe_parser.set_defaults(handler=cmd_probe)

    return parser


def _load(args: argparse.Namespace) -> Tuple[List[ServerSpec], List[Finding]]:
    findings: List[Finding] = []
    data, load_findings = load_config(args.config)
    findings.extend(load_findings)
    specs: List[ServerSpec] = []
    if data is not None:
        servers, _ = find_config_wrapper(data)
        if servers:
            specs = to_specs(servers, findings)
            lint(specs, findings)
    if args.server:
        matched = [s for s in specs if s.name == args.server]
        if not matched:
            findings.append(
                Finding(
                    "MD018",
                    "error",
                    f"no server named \"{args.server}\" in this config",
                    hint="names: " + ", ".join(s.name for s in specs) if specs else None,
                )
            )
        specs = matched
        findings = [f for f in findings if f.server in (None, args.server)]
    return specs, findings


def cmd_lint(args: argparse.Namespace) -> int:
    specs, findings = _load(args)
    color = not args.no_color and sys.stdout.isatty()
    if args.format == "json":
        print(
            json.dumps(
                {
                    "tool": "mcp-doctor",
                    "version": VERSION,
                    "config": args.config,
                    "servers": [{"name": s.name, "transport": s.transport} for s in specs],
                    "findings": [f.as_dict() for f in findings],
                    "summary": {
                        "errors": sum(1 for f in findings if f.severity == "error"),
                        "warnings": sum(1 for f in findings if f.severity == "warn"),
                        "infos": sum(1 for f in findings if f.severity == "info"),
                    },
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    elif args.format == "markdown":
        print(render_markdown(args.config, specs, findings, None))
    else:
        print(render_text(args.config, specs, findings, None, color))
    return exit_code(findings, [], args.strict)


def cmd_probe(args: argparse.Namespace) -> int:
    specs, findings = _load(args)
    probes: List[ProbeResult] = []
    targets = specs[: args.max_servers] if args.max_servers else specs
    for spec in targets:
        if spec.transport == "stdio" and not spec.command:
            probes.append(
                ProbeResult(spec.name, spec.transport, False, error="no command to launch")
            )
            continue
        if spec.transport in ("http", "sse"):
            probes.append(probe_http(spec, args.timeout))
        else:
            probes.append(probe_stdio(spec, args.timeout))

    color = not args.no_color and sys.stdout.isatty()
    if args.format == "json":
        print(
            json.dumps(
                {
                    "tool": "mcp-doctor",
                    "version": VERSION,
                    "config": args.config,
                    "findings": [f.as_dict() for f in findings],
                    "probes": [p.as_dict() for p in probes],
                    "summary": {
                        "errors": sum(1 for f in findings if f.severity == "error"),
                        "probe_failures": sum(1 for p in probes if not p.ok),
                    },
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    elif args.format == "markdown":
        print(render_markdown(args.config, specs, findings, probes))
    else:
        print(render_text(args.config, specs, findings, probes, color))
    return exit_code(findings, probes, args.strict)


def exit_code(findings: List[Finding], probes: List[ProbeResult], strict: bool) -> int:
    errors = sum(1 for f in findings if f.severity == "error")
    warns = sum(1 for f in findings if f.severity == "warn")
    failures = sum(1 for p in probes if not p.ok)
    if errors or failures:
        return 1
    if strict and warns:
        return 1
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return 2
    try:
        return args.handler(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
