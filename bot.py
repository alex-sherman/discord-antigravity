from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Set

import discord
from dotenv import load_dotenv

from agy_bridge import DesktopAgentBridge, SdkAgentBridge

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
AGY_MODE = os.getenv("AGY_MODE", "desktop").strip().lower()
AGY_MODEL = os.getenv("AGY_MODEL", "default").strip()
AGY_CHAT_MODEL = os.getenv("AGY_CHAT_MODEL", AGY_MODEL if AGY_MODEL != "default" else "flash").strip()
AGY_THREAD_MODEL = os.getenv("AGY_THREAD_MODEL", "pro").strip()

raw_allowed = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS: Set[int] = {
    int(uid.strip()) for uid in raw_allowed.split(",") if uid.strip().isdigit()
}

CHANNEL_MAP_FILE = Path(__file__).parent / "channel_map.json"

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

desktop_bridge: DesktopAgentBridge | None = None
sdk_bridge: SdkAgentBridge | None = None


def load_channel_map() -> Dict[int, str]:
    if not CHANNEL_MAP_FILE.exists():
        return {}
    try:
        raw = json.loads(CHANNEL_MAP_FILE.read_text(encoding="utf-8"))
        return {int(k): str(v) for k, v in raw.items()}
    except Exception:
        return {}


def save_channel_map(mapping: Dict[int, str]) -> None:
    try:
        serializable = {str(k): v for k, v in mapping.items()}
        CHANNEL_MAP_FILE.write_text(
            json.dumps(serializable, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        print(f"[Warning] Failed to save channel_map.json: {exc}")


# Maps Discord channel/thread/DM ID -> Antigravity conversation_id
channel_conversations: Dict[int, str] = load_channel_map()
# Maps Discord channel/thread/DM ID -> Antigravity project dict {"id": ..., "name": ..., "folder": ...}
channel_projects: Dict[int, Dict[str, str]] = {}

DISCORD_PREAMBLE = (
    "[Note: Connected via Discord — use inline code (`file.py`) instead of `file://` links.]\n\n"
)


def clean_discord_markdown(text: str) -> str:
    """Format text cleanly for Discord.

    Discord markdown does not support file:// URI hyperlinks (e.g.
    [label](file:///...)), which render as raw broken text. Convert them to
    clean inline code badges (`label`).
    """
    if not text:
        return text
    # Convert [`symbol`](file:///...) or [label](file:///...) -> `symbol` or `label`
    text = re.sub(r'\[`?([^\]`\r\n]+)`?\]\(file://[^\)]*\)', r'`\1`', text)
    # Convert <file:///...> -> `...`
    text = re.sub(r'<file:///[^>]+>', lambda m: f"`{m.group(0)[1:-1]}`", text)
    return text


def split_discord_message(text: str, limit: int = 1950) -> list[str]:
    """Split long markdown responses cleanly without breaking Discord's 2000-char limit."""
    text = clean_discord_markdown(text)
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1 or split_at < limit // 2:
            split_at = limit
        parts.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    return parts


def is_authorized(user: discord.abc.User) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user.id in ALLOWED_USER_IDS


def make_thread_name(prompt: str, project_name: str | None = None) -> str:
    """Build a clean Discord thread name (max 100 chars) from a prompt or timestamp."""
    cleaned = " ".join(prompt.split()).strip()
    prefix = f"[{project_name}] " if project_name else ""
    if cleaned:
        full = f"{prefix}{cleaned}"
        return full[:95] + ("..." if len(full) > 95 else "")
    base = project_name or "Antigravity"
    return f"{base} — {datetime.now().strftime('%b %d %H:%M')}"


def describe_channel(channel: discord.abc.Messageable, author: discord.abc.User) -> str:
    if isinstance(channel, discord.Thread):
        return f"Discord Thread: {channel.name}"
    if isinstance(channel, discord.TextChannel):
        return f"Discord #{channel.name}"
    return f"Discord DM ({author.display_name})"


def get_model_for_channel(channel: discord.abc.Messageable) -> str:
    """Use AGY_THREAD_MODEL inside threads and AGY_CHAT_MODEL in DMs / server channels."""
    if isinstance(channel, discord.Thread):
        return AGY_THREAD_MODEL
    return AGY_CHAT_MODEL


def resolve_channel_project(
    channel: discord.abc.Messageable,
) -> Dict[str, str] | None:
    """Return the Antigravity project attached to `channel`, auto-matching by channel name if applicable."""
    channel_id = getattr(channel, "id", 0)
    if not channel_id:
        return None

    if channel_id in channel_projects:
        return channel_projects[channel_id]

    if desktop_bridge is None:
        return None

    # Text channels auto-attach to a project with a matching name (if one exists)
    if isinstance(channel, discord.TextChannel):
        matched = desktop_bridge.find_project_by_channel_name(channel.name)
        if matched:
            channel_projects[channel_id] = matched
            return matched

    # Threads inherit their parent text channel's project unless overridden
    if isinstance(channel, discord.Thread) and isinstance(
        channel.parent, discord.TextChannel
    ):
        parent_proj = resolve_channel_project(channel.parent)
        if parent_proj:
            channel_projects[channel_id] = parent_proj
            return parent_proj

    return None


async def run_prompt_in_channel(
    target_channel: discord.abc.Messageable,
    author: discord.abc.User,
    prompt: str,
    project: Dict[str, str] | None = None,
) -> None:
    """Send `prompt` to the conversation dedicated to `target_channel` and stream results."""
    channel_id = getattr(target_channel, "id", 0)
    if project is not None:
        channel_projects[channel_id] = project
    active_project = resolve_channel_project(target_channel)
    project_id = active_project["id"] if active_project else None

    status_msg = await target_channel.send("⏳ *Sending to Antigravity...*")

    try:
        if AGY_MODE == "sdk":
            assert sdk_bridge is not None
            async with target_channel.typing():
                async for event in sdk_bridge.send_and_stream(prompt):
                    if event.kind == "response":
                        chunks = split_discord_message(event.text)
                        await status_msg.edit(content=chunks[0])
                        for extra in chunks[1:]:
                            await target_channel.send(extra)
            return

        # Desktop Bridge Mode: each channel/thread/DM has its own 1-to-1 conversation
        assert desktop_bridge is not None
        conv_id = channel_conversations.get(channel_id)
        created_new = False

        if not conv_id:
            chosen_model = get_model_for_channel(target_channel)
            proj_label = f" in project `{active_project['name']}`" if active_project else ""
            await status_msg.edit(
                content=f"🚀 *Starting new Antigravity conversation{proj_label} (model: `{chosen_model}`)...*"
            )
            initial_prompt = f"{DISCORD_PREAMBLE}{prompt}"
            conv_id = await desktop_bridge.start_conversation(
                prompt=initial_prompt,
                title=describe_channel(target_channel, author),
                model=chosen_model,
                project_id=project_id,
            )
            channel_conversations[channel_id] = conv_id
            save_channel_map(channel_conversations)
            created_new = True

        tool_log: list[str] = []
        async with target_channel.typing():
            async for event in desktop_bridge.send_and_stream(
                conversation_id=conv_id,
                prompt=prompt,
                is_new_conversation=created_new,
                project_id=project_id,
            ):
                if event.kind == "tool_call":
                    tools_str = ", ".join(f"`{t}`" for t in event.tool_names)
                    tool_log.append(f"🔧 Running {tools_str}")
                    recent_log = "\n".join(tool_log[-5:])
                    try:
                        await status_msg.edit(
                            content=f"🧠 *Antigravity is working (`{conv_id[:8]}...`)*\n{recent_log}"
                        )
                    except Exception:
                        pass
                elif event.kind == "thinking" and event.text:
                    chunks = split_discord_message(event.text)
                    for chunk in chunks:
                        await target_channel.send(chunk)
                elif event.kind == "interim" and event.text:
                    # Interim progress message while subagent / background task is active
                    chunks = split_discord_message(event.text)
                    try:
                        await status_msg.edit(content=chunks[0])
                        for extra in chunks[1:]:
                            await target_channel.send(extra)
                    except Exception:
                        for chunk in chunks:
                            await target_channel.send(chunk)
                    tool_log.clear()
                    status_msg = await target_channel.send(
                        f"⏳ *Antigravity: Subagent working in background (`{conv_id[:8]}...`)*"
                    )
                elif event.kind == "response":
                    chunks = split_discord_message(event.text)
                    try:
                        await status_msg.edit(content=chunks[0])
                        for extra in chunks[1:]:
                            await target_channel.send(extra)
                    except Exception:
                        for chunk in chunks:
                            await target_channel.send(chunk)
                    return
                elif event.kind == "error":
                    try:
                        await status_msg.edit(content=f"⚠️ {event.text}")
                    except Exception:
                        await target_channel.send(f"⚠️ {event.text}")
                    return

    except Exception as exc:
        await status_msg.edit(content=f"❌ **Error communicating with Antigravity:** `{exc}`")


async def start_new_session(
    message: discord.Message,
    prompt: str,
    project: Dict[str, str] | None = None,
) -> None:
    """Create a new thread (or reset DM conversation) optionally bound to `project`."""
    if project is None:
        project = resolve_channel_project(message.channel)
    proj_name = project["name"] if project else None
    thread_name = make_thread_name(prompt, project_name=proj_name)
    proj_banner = (
        f" in project `{project['name']}` (`{project['folder']}`)"
        if project and project.get("folder")
        else (f" in project `{project['name']}`" if project else "")
    )

    # In a server TextChannel: create a thread directly off the user's message
    if isinstance(message.channel, discord.TextChannel):
        try:
            thread = await message.create_thread(name=thread_name)
        except discord.Forbidden:
            await message.channel.send(
                "⚠️ I don't have permission to create threads in this channel. "
                "Please grant the bot **Create Public Threads** and **Send Messages in Threads** permissions."
            )
            return

        if project:
            channel_projects[thread.id] = project

        if prompt:
            await run_prompt_in_channel(
                target_channel=thread,
                author=message.author,
                prompt=prompt,
                project=project,
            )
        else:
            await thread.send(
                f"🆕 **New thread started{proj_banner}!** Send a message here to start this thread's Antigravity conversation."
            )
        return

    # Inside an existing Thread: create a new sibling thread in the parent channel
    if isinstance(message.channel, discord.Thread) and isinstance(
        message.channel.parent, discord.TextChannel
    ):
        try:
            thread = await message.channel.parent.create_thread(
                name=thread_name,
                type=discord.ChannelType.public_thread,
            )
        except discord.Forbidden:
            await message.channel.send(
                "⚠️ I don't have permission to create threads in the parent channel."
            )
            return

        if project:
            channel_projects[thread.id] = project

        await message.channel.send(f"🆕 Created new thread{proj_banner}: {thread.mention}")
        if prompt:
            await run_prompt_in_channel(
                target_channel=thread,
                author=message.author,
                prompt=prompt,
                project=project,
            )
        else:
            await thread.send(
                f"🆕 **New thread started{proj_banner}!** Send a message here to start this thread's Antigravity conversation."
            )
        return

    # In a DM (Discord doesn't support threads in DMs): reset DM conversation
    channel_conversations.pop(message.channel.id, None)
    if project:
        channel_projects[message.channel.id] = project
    else:
        channel_projects.pop(message.channel.id, None)
    save_channel_map(channel_conversations)
    if prompt:
        await run_prompt_in_channel(
            target_channel=message.channel,
            author=message.author,
            prompt=prompt,
            project=project,
        )
    else:
        await message.channel.send(
            f"🆕 Started a fresh Antigravity conversation{proj_banner} for this DM. Send your next message!"
        )


def format_projects_list(projects: list[Dict[str, str]]) -> str:
    if not projects:
        return "No projects found in `~/.gemini/config/projects`."
    lines = ["📁 **Available Antigravity Projects**"]
    for p in projects:
        folder_info = f" — `{p['folder']}`" if p.get("folder") else ""
        lines.append(f"• `{p['name']}`{folder_info}")
    lines.append("\n**Usage:** `!project <name> [prompt]`")
    return "\n".join(lines)


def auto_attach_guild_channels() -> None:
    """Scan all guild text channels and auto-attach them to matching Antigravity projects."""
    if desktop_bridge is None:
        return
    attached: list[str] = []
    for guild in client.guilds:
        for channel in guild.text_channels:
            proj = resolve_channel_project(channel)
            if proj:
                attached.append(f"#{channel.name} -> {proj['name']} ({proj['id']})")
    if attached:
        print(
            "[Antigravity Discord Bot] Auto-attached text channels to projects:\n  • "
            + "\n  • ".join(attached)
        )


@client.event
async def on_ready() -> None:
    global desktop_bridge, sdk_bridge
    if AGY_MODE == "sdk":
        sdk_bridge = SdkAgentBridge()
        print(f"[Antigravity Discord Bot] Logged in as {client.user} (Mode: Python SDK)")
    else:
        desktop_bridge = DesktopAgentBridge(default_model=AGY_MODEL)
        print(
            f"[Antigravity Discord Bot] Logged in as {client.user} (Mode: Desktop App Bridge)"
        )
        auto_attach_guild_channels()


@client.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel) -> None:
    if isinstance(channel, discord.TextChannel):
        proj = resolve_channel_project(channel)
        if proj:
            print(
                f"[Antigravity Discord Bot] Auto-attached new channel #{channel.name} -> {proj['name']}"
            )


@client.event
async def on_guild_channel_update(
    before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
) -> None:
    if isinstance(after, discord.TextChannel) and before.name != after.name:
        channel_projects.pop(after.id, None)
        proj = resolve_channel_project(after)
        if proj:
            print(
                f"[Antigravity Discord Bot] Re-attached renamed channel #{after.name} -> {proj['name']}"
            )


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    if not is_authorized(message.author):
        await message.channel.send("⛔ You are not authorized to use this Antigravity bot.")
        return

    content = message.content.strip()
    if client.user:
        for mention in (f"<@{client.user.id}>", f"<@!{client.user.id}>"):
            if content.startswith(mention):
                content = content[len(mention) :].strip()
    if not content:
        return

    # Commands
    if content.startswith("!help"):
        help_text = (
            "**Antigravity Discord Bridge Commands**\n"
            "• Each server channel, thread, and DM has its own dedicated Antigravity conversation.\n"
            "• Text channels automatically attach to a matching Antigravity project if one exists.\n"
            "• `!new [prompt]` — Create a new thread (or reset DM) with a fresh Antigravity conversation.\n"
            "• `!project [name] [prompt]` — List projects, or start a new conversation in an existing project.\n"
            "• `!status` — Show the Antigravity `conversation_id` and active project for this channel/thread/DM.\n"
        )
        await message.channel.send(help_text)
        return

    if content.startswith("!status"):
        conv_id = channel_conversations.get(
            message.channel.id, "(None yet — will be created on your next message)"
        )
        active_tier = get_model_for_channel(message.channel)
        surface = "Thread" if isinstance(message.channel, discord.Thread) else "Channel / DM"
        proj = resolve_channel_project(message.channel)
        if proj:
            folder_suffix = f" — `{proj['folder']}`" if proj.get("folder") else ""
            proj_str = f"`{proj['name']}` (`{proj['id']}`){folder_suffix}"
        else:
            proj_str = "`outside-of-project`"
        await message.channel.send(
            f"**Mode:** `{AGY_MODE}`\n"
            f"**Surface:** {surface} (uses `{active_tier}`)\n"
            f"**Project:** {proj_str}\n"
            f"**Channel Conversation ID:** `{conv_id}`\n"
            f"**Chat Model (`AGY_CHAT_MODEL`):** `{AGY_CHAT_MODEL}`\n"
            f"**Thread Model (`AGY_THREAD_MODEL`):** `{AGY_THREAD_MODEL}`"
        )
        return

    if content.startswith("!project"):
        assert desktop_bridge is not None
        raw_args = content[len("!project") :].strip()
        projects = desktop_bridge.list_projects()
        if not raw_args:
            await message.channel.send(format_projects_list(projects))
            return

        # Match multi-word project names first, then fall back to first token
        matched_project: Dict[str, str] | None = None
        prompt = ""
        for p in sorted(projects, key=lambda x: len(x["name"]), reverse=True):
            pname = p["name"]
            if raw_args.lower() == pname.lower() or raw_args.lower().startswith(pname.lower() + " "):
                matched_project = p
                prompt = raw_args[len(pname) :].strip()
                break

        if matched_project is None:
            parts = raw_args.split(None, 1)
            project_query = parts[0]
            prompt = parts[1].strip() if len(parts) > 1 else ""
            matched_project = desktop_bridge.find_project(project_query)

        if matched_project is None:
            await message.channel.send(
                f"⚠️ Project `{raw_args.split()[0]}` not found.\n\n{format_projects_list(projects)}"
            )
            return

        await start_new_session(message, prompt=prompt, project=matched_project)
        return

    if content.startswith("!new"):
        prompt = content[4:].strip()
        await start_new_session(message, prompt=prompt, project=None)
        return

    # Regular message: route to this channel/thread/DM's dedicated conversation
    await run_prompt_in_channel(
        target_channel=message.channel,
        author=message.author,
        prompt=content,
    )


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN or DISCORD_BOT_TOKEN == "your_discord_bot_token_here":
        raise SystemExit(
            "Please copy .env.example to .env and set your DISCORD_BOT_TOKEN first."
        )
    client.run(DISCORD_BOT_TOKEN)
