from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional, Tuple


DEFAULT_AGY_DIR = Path.home() / ".gemini" / "antigravity"
DEFAULT_AGENTAPI_BAT = DEFAULT_AGY_DIR / "bin" / "agentapi.bat"
DEFAULT_BRAIN_DIR = DEFAULT_AGY_DIR / "brain"
DEFAULT_PROJECTS_DIR = Path.home() / ".gemini" / "config" / "projects"

UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


@dataclass
class AgentEvent:
    """Represents a live update from Antigravity while processing a turn."""

    kind: str  # "thinking", "tool_call", "response", "error"
    text: str
    tool_names: List[str] = field(default_factory=list)


class DesktopAgentBridge:
    """
    Bridges Discord directly to the running Antigravity 2.0 desktop application
    using `agentapi.bat` and live-tailing `transcript_full.jsonl`.
    """

    def __init__(
        self,
        agentapi_path: Optional[Path] = None,
        brain_dir: Optional[Path] = None,
        default_model: str = "pro",
    ) -> None:
        self.agentapi_path = agentapi_path or DEFAULT_AGENTAPI_BAT
        self.brain_dir = brain_dir or DEFAULT_BRAIN_DIR
        self.default_model = default_model
        self._ls_address: Optional[str] = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
        self._csrf_token: Optional[str] = os.environ.get("ANTIGRAVITY_CSRF_TOKEN")

        if not self.agentapi_path.exists():
            raise FileNotFoundError(
                f"Could not find agentapi.bat at {self.agentapi_path}. "
                "Make sure the Antigravity desktop app is installed."
            )

        self._agentapi_cmd: List[str] = self._resolve_agentapi_cmd(self.agentapi_path)

    @staticmethod
    def _resolve_agentapi_cmd(agentapi_path: Path) -> List[str]:
        """Resolve the underlying `language_server.exe` binary from `agentapi.bat`.

        Invoking the `.exe` directly avoids `cmd.exe`, which otherwise truncates
        command-line arguments at the first newline character (`\\n`).
        """
        if agentapi_path.suffix.lower() == ".bat" and agentapi_path.exists():
            try:
                content = agentapi_path.read_text(encoding="utf-8", errors="replace")
                m = re.search(r'"([^"]+language_server\.exe)"\s+agentapi', content, re.I)
                if m and Path(m.group(1)).exists():
                    return [m.group(1), "agentapi"]
            except Exception:
                pass
        return [str(agentapi_path)]

    def _discover_ls_connection(self) -> Tuple[str, str]:
        """
        Auto-discover ANTIGRAVITY_LS_ADDRESS and ANTIGRAVITY_CSRF_TOKEN from the
        running Antigravity `language_server.exe` process on Windows.
        """
        ps_script = (
            "$procs = Get-CimInstance Win32_Process -Filter \"Name = 'language_server.exe'\"; "
            "$result = foreach ($p in $procs) { "
            "  $ports = @(Get-NetTCPConnection -OwningProcess $p.ProcessId -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty LocalPort | Sort-Object -Descending); "
            "  [PSCustomObject]@{ pid = $p.ProcessId; cmd = $p.CommandLine; ports = $ports } "
            "}; "
            "$result | ConvertTo-Json -Compress"
        )
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps_script],
            text=True,
            timeout=10,
        ).strip()

        if not raw:
            raise RuntimeError(
                "Antigravity language_server.exe is not running. Please make sure the Antigravity desktop app is open."
            )

        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]

        # Prefer the hub process (--subclient_type hub) if multiple exist
        data.sort(key=lambda item: "--subclient_type hub" not in (item.get("cmd") or ""))

        for item in data:
            cmd = item.get("cmd") or ""
            ports = item.get("ports") or []
            m = re.search(r"--csrf_token\s+([^\s\"]+)", cmd)
            if not m or not ports:
                continue
            csrf_token = m.group(1)
            # Test candidate ports (highest port is typically the gRPC/HTTP server port)
            for port in ports:
                address = f"localhost:{port}"
                env = os.environ.copy()
                env["ANTIGRAVITY_LS_ADDRESS"] = address
                env["ANTIGRAVITY_CSRF_TOKEN"] = csrf_token
                probe = subprocess.run(
                    [*self._agentapi_cmd, "get-conversation-metadata", "00000000-0000-0000-0000-000000000000"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                combined = (probe.stdout or "") + (probe.stderr or "")
                # Even if conversation 0000... isn't found, a valid gRPC port returns JSON or "not found" instead of connection/HTTP errors
                if "Unauthenticated" not in combined and "connection refused" not in combined.lower() and "malformed HTTP" not in combined:
                    self._ls_address = address
                    self._csrf_token = csrf_token
                    return address, csrf_token

            # Fallback to highest port
            self._ls_address = f"localhost:{ports[0]}"
            self._csrf_token = csrf_token
            return self._ls_address, self._csrf_token

        raise RuntimeError(
            "Could not discover port and CSRF token from language_server.exe."
        )

    def _transcript_path(self, conversation_id: str) -> Path:
        full_path = (
            self.brain_dir
            / conversation_id
            / ".system_generated"
            / "logs"
            / "transcript_full.jsonl"
        )
        if full_path.exists():
            return full_path
        return (
            self.brain_dir
            / conversation_id
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )

    def read_steps(self, conversation_id: str) -> List[dict]:
        path = self._transcript_path(conversation_id)
        if not path.exists():
            return []
        steps: List[dict] = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    steps.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return steps

    def list_recent_conversations(self, limit: int = 10) -> List[Dict[str, str]]:
        """Return the most recently active conversations from the brain directory."""
        if not self.brain_dir.exists():
            return []

        items = []
        for conv_dir in self.brain_dir.iterdir():
            if not conv_dir.is_dir() or not UUID_RE.fullmatch(conv_dir.name):
                continue
            transcript = self._transcript_path(conv_dir.name)
            mtime = transcript.stat().st_mtime if transcript.exists() else conv_dir.stat().st_mtime
            preview = "(No messages yet)"
            if transcript.exists():
                steps = self.read_steps(conv_dir.name)
                for s in steps:
                    if s.get("type") == "USER_INPUT" and s.get("content"):
                        raw = s["content"]
                        m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", raw, re.S)
                        preview = (m.group(1) if m else raw).strip().replace("\n", " ")
                        if len(preview) > 80:
                            preview = preview[:77] + "..."
                        break
            items.append(
                {
                    "id": conv_dir.name,
                    "mtime": mtime,
                    "preview": preview,
                }
            )

        items.sort(key=lambda x: x["mtime"], reverse=True)
        return items[:limit]

    def list_projects(self) -> List[Dict[str, str]]:
        """Return all configured Antigravity projects from `~/.gemini/config/projects`."""
        if not DEFAULT_PROJECTS_DIR.exists():
            return []
        projects: List[Dict[str, str]] = []
        for path in sorted(DEFAULT_PROJECTS_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            proj_id = data.get("id")
            proj_name = data.get("name")
            if not proj_id or not proj_name:
                continue

            folders: List[str] = []
            for res in data.get("projectResources", {}).get("resources", []):
                uri = res.get("folderUri") or res.get("gitFolder", {}).get("folderUri") or ""
                if uri.startswith("file:///"):
                    decoded = urllib.parse.unquote(uri[len("file:///") :])
                    folders.append(decoded)

            projects.append(
                {
                    "id": str(proj_id),
                    "name": str(proj_name),
                    "folder": folders[0] if folders else "",
                }
            )
        projects.sort(key=lambda p: p["name"].lower())
        return projects

    @staticmethod
    def _slugify(value: str) -> str:
        """Normalize a project or Discord channel name into an alphanumeric/hyphen slug."""
        return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")

    def find_project_by_channel_name(self, channel_name: str) -> Optional[Dict[str, str]]:
        """Match a Discord text channel name to a configured Antigravity project.

        Only performs exact or normalized slug matches (not loose substring matches)
        so generic channels like `#general` or `#dev` are not accidentally attached.
        """
        raw = channel_name.strip().lower()
        if not raw:
            return None

        slug = self._slugify(raw)
        # Also allow optional channel prefixes such as `proj-` or `project-`
        stripped_slug = re.sub(r"^(?:proj|project)-", "", slug)
        candidates = {slug}
        if stripped_slug:
            candidates.add(stripped_slug)

        projects = self.list_projects()

        # 1. Exact case-insensitive match on project name or folder basename
        for p in projects:
            folder_name = Path(p["folder"]).name.lower() if p["folder"] else ""
            if p["name"].lower() == raw or (folder_name and folder_name == raw):
                return p

        # 2. Normalized slug match on project name or folder basename
        #    (e.g. Discord `#asherman-io` <-> project `asherman.io`)
        for p in projects:
            name_slug = self._slugify(p["name"])
            folder_slug = self._slugify(Path(p["folder"]).name) if p["folder"] else ""
            if name_slug in candidates or (folder_slug and folder_slug in candidates):
                return p

        return None

    def find_project(self, query: str) -> Optional[Dict[str, str]]:
        """Find a project by name, folder basename, or ID (case-insensitive)."""
        q = query.strip().lower()
        if not q:
            return None
        projects = self.list_projects()
        # 1. Exact name or ID match
        for p in projects:
            if p["name"].lower() == q or p["id"].lower() == q:
                return p
        # 2. Exact folder basename match
        for p in projects:
            if p["folder"] and Path(p["folder"]).name.lower() == q:
                return p
        # 3. Normalized slug match
        by_channel = self.find_project_by_channel_name(q)
        if by_channel:
            return by_channel
        # 4. Substring match on name or folder basename
        for p in projects:
            folder_name = Path(p["folder"]).name.lower() if p["folder"] else ""
            if q in p["name"].lower() or (folder_name and q in folder_name):
                return p
        return None

    async def _run_agentapi(self, *args: str, project_id: Optional[str] = None) -> str:
        if not self._ls_address or not self._csrf_token:
            await asyncio.to_thread(self._discover_ls_connection)

        for attempt in range(2):
            env = os.environ.copy()
            env["ANTIGRAVITY_LS_ADDRESS"] = self._ls_address or ""
            env["ANTIGRAVITY_CSRF_TOKEN"] = self._csrf_token or ""
            if project_id:
                env["ANTIGRAVITY_PROJECT_ID"] = project_id
            elif not env.get("ANTIGRAVITY_PROJECT_ID"):
                env["ANTIGRAVITY_PROJECT_ID"] = "outside-of-project"
            if not env.get("ANTIGRAVITY_APP_DATA_DIR"):
                env["ANTIGRAVITY_APP_DATA_DIR"] = str(DEFAULT_AGY_DIR)

            proc = await asyncio.create_subprocess_exec(
                *self._agentapi_cmd,
                *args,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            out_text = stdout.decode("utf-8", errors="replace").strip()
            err_text = stderr.decode("utf-8", errors="replace").strip()
            if proc.returncode == 0:
                return out_text

            # If Antigravity restarted on a new port/token, re-discover and retry once
            if attempt == 0:
                await asyncio.to_thread(self._discover_ls_connection)
            else:
                raise RuntimeError(
                    f"agentapi failed (exit {proc.returncode}): {err_text or out_text}"
                )
        return ""

    async def start_conversation(
        self,
        prompt: str,
        title: str = "Discord Session",
        model: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> str:
        """Create a new conversation in the Antigravity desktop app and return its ID."""
        chosen_model = (model if model is not None else self.default_model).strip().lower()
        args = ["new-conversation"]
        if chosen_model and chosen_model not in ("default", "inherit", "auto"):
            args.append(f"--model={chosen_model}")
        args.extend([f"--title={title}", prompt])
        output = await self._run_agentapi(*args, project_id=project_id)
        # Extract the conversation UUID from JSON or text output
        try:
            data = json.loads(output)
            conv_id = (
                data.get("conversationId")
                or data.get("response", {}).get("conversationId")
                or data.get("id")
            )
            if conv_id:
                return conv_id
        except json.JSONDecodeError:
            pass

        match = UUID_RE.search(output)
        if not match:
            raise RuntimeError(f"Could not parse conversation ID from output: {output}")
        return match.group(0)

    async def send_and_stream(
        self,
        conversation_id: str,
        prompt: str,
        is_new_conversation: bool = False,
        project_id: Optional[str] = None,
        poll_interval: float = 1.0,
        timeout: float = 600.0,
    ) -> AsyncIterator[AgentEvent]:
        """
        Send a message to an existing (or newly created) conversation and yield
        live `AgentEvent` updates as Antigravity thinks, runs tools, and responds.
        """
        existing_steps = self.read_steps(conversation_id)
        last_seen_index = (
            max((s.get("step_index", -1) for s in existing_steps), default=-1)
            if not is_new_conversation
            else -1
        )

        if not is_new_conversation:
            await self._run_agentapi(
                "send-message",
                "--title=Discord User",
                conversation_id,
                prompt,
                project_id=project_id,
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        while loop.time() < deadline:
            await asyncio.sleep(poll_interval)
            steps = self.read_steps(conversation_id)
            new_steps = [
                s for s in steps if s.get("step_index", -1) > last_seen_index
            ]
            if not new_steps:
                continue

            for step in new_steps:
                last_seen_index = max(last_seen_index, step.get("step_index", -1))
                source = step.get("source")
                step_type = step.get("type")

                if source == "MODEL" and step_type == "PLANNER_RESPONSE":
                    tool_calls = step.get("tool_calls") or []
                    content = (step.get("content") or "").strip()
                    thinking = (step.get("thinking") or "").strip()

                    if tool_calls:
                        names = [tc.get("name", "tool") for tc in tool_calls]
                        yield AgentEvent(
                            kind="tool_call",
                            text=thinking or f"Executing: {', '.join(names)}",
                            tool_names=names,
                        )
                        if content:
                            # Intermediate message before tool execution
                            yield AgentEvent(kind="thinking", text=content)
                    elif content:
                        yield AgentEvent(kind="response", text=content)
                        return

        yield AgentEvent(
            kind="error",
            text="Timed out waiting for Antigravity response (check the desktop app for pending tool approvals).",
        )


class SdkAgentBridge:
    """
    Optional standalone bridge using the official `google-antigravity` Python SDK.
    Requires `pip install google-antigravity`.
    """

    def __init__(self) -> None:
        self._agent = None

    async def ensure_started(self) -> None:
        if self._agent is not None:
            return
        from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig

        config = LocalAgentConfig(
            system_instructions="You are an Antigravity agent accessed via Discord.",
            capabilities=CapabilitiesConfig(),
        )
        self._agent = await Agent(config).__aenter__()

    async def send_and_stream(self, prompt: str) -> AsyncIterator[AgentEvent]:
        await self.ensure_started()
        response = await self._agent.chat(prompt)

        chunks: List[str] = []
        async for token in response:
            chunks.append(token)

        yield AgentEvent(kind="response", text="".join(chunks) or "(No response)")
