# Discord Bridge for Google Antigravity

Access your local **Google Antigravity** desktop app (or standalone SDK agent) directly from Discord on your desktop or phone.

## How It Works

- **1-to-1 Conversations per Channel / Thread / DM**:
  - Every distinct Discord server channel, thread, or DM gets its own dedicated Antigravity conversation automatically on the first message.
  - Channel-to-conversation mappings are saved locally in `channel_map.json` so they persist across bot restarts.
- **Automatic Project Attachment by Channel Name**:
  - Server text channels automatically attach to a matching Antigravity project if one exists (e.g. `#discord-antigravity` -> `discord-antigravity`, `#asherman-io` -> `asherman.io`, `#spring` -> `spring`).
  - Threads created inside a project-attached text channel automatically inherit that project unless overridden via `!project`.
- **`!new [prompt]` Thread Workflow**:
  - Typing `!new` or `!new <prompt>` in a server channel automatically creates a **Discord thread** (named after your prompt or timestamp) and starts a brand-new Antigravity conversation inside that thread.
- **Built-in Commands**:
  - `!new [prompt]` — Create a new thread in a server channel with a fresh Antigravity conversation (or reset conversation in a DM).
  - `!project [name] [prompt]` — List projects, or start a new conversation attached to a specific project.
  - `!status` — Check the Antigravity `conversation_id` and active project linked to the current channel/thread/DM.
  - `!help` — Show available commands.

---

## Setup & Permissions

1. **Discord Bot Permissions:**
   Make sure the bot has these permissions in your server:
   - `View Channels`
   - `Send Messages`
   - `Read Message History`
   - `Create Public Threads`
   - `Send Messages in Threads`
2. **Run the Bot:**
   ```powershell
   C:\Code\discord-antigravity\run.bat
   ```
