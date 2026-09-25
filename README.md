# Agent Bridge for ChromieCraft (WoW 3.3.5a)

Chat with **Claude Code**, **Codex**, or **Hermes Agent** from a panel inside World of Warcraft 3.3.5a, keep playing while the agent works, and get a chime and minimap badge when the reply is ready.

This is a port of [0xInuarashi/wow-forever-codex](https://github.com/0xInuarashi/wow-forever-codex), which does the same for WoW: Forever. The core idea is theirs: an addon can't open a connection, so prompts leave the game as a pixel strip and replies come back as font glyph widths. This version re-implements it for the Wrath 3.3.5a client that ChromieCraft uses, and adds a Claude Code backend alongside Codex.

No DLL injection, no memory access, no synthetic input. The addon uses documented 3.3.5a APIs only (textures, `SetFont`, `SetText`, `GetStringWidth`, `LoadAddOn`). The companion reads a small screen rectangle and writes ordinary files.

## How the two channels work

| Direction | Carrier | Receiver reads |
| --- | --- | --- |
| Game → companion | 128 × 8 cell strip at the top of the screen, any opacity | Differences between cell pairs in a screen capture |
| Companion → game | An unused font file in the addon's bank | Glyph advance widths via `GetStringWidth` |
| Companion → game, finished replies over 4,060 bytes | One of 16 optional load-on-demand addons | A fixed hex-data assignment, announced by a checksummed font packet |

**Prompts.** The addon splits the UTF-8 prompt (max 8,000 bytes, including the header, linked tooltips and game context described under [In game](#in-game)) into 64-byte checksummed frames and flashes them on the strip. Every bit is a *pair* of neighbouring cells, one light and one dark, and the companion reads the difference between them. Because only the difference matters, the strip decodes at any opacity, so you can turn it down with `/ab alpha 0.5` and still see the UI through it. A pair straddling a hard UI edge reads as low contrast and rejects the frame rather than guessing. The strip also carries a *control* frame: which font slot the addon will load next, how many milliseconds until it does, and which reply fragment it needs. The strip is only shown during an exchange.

**Replies.** A reply packet is 4,096 bytes: a 32-byte header, 4,060 bytes of text, and an Adler-32 checksum. The companion writes it into the requested slot as a TrueType font. Each byte uses two glyphs, one per 4-bit nibble: glyph `U+E000 + i` has advance `(2 + value) × 128` units at 1,024 units/em. Lua measures each glyph, recovers the nibbles, validates the packet, joins the fragments, and displays plain text with native item links. Polls read only the 32-byte header first, so an unchanged status, an empty slot or a stale one costs a fraction of a second instead of a full packet.

**Why four bits per glyph.** Measured in the live 3.3.5a client: the rasterised em is capped around 32 px, and asking for a bigger font size changes measured widths not at all — sizes 128, 192 and 256 all returned identical widths. A whole byte per glyph (the original's `em/64` step) therefore lands byte values ~0.5 px apart, and they collide. A nibble step of `em/8` keeps them ~4 px apart at the cost of two glyphs per byte.

**Markdown, in a narrow panel.** Replies are reformatted rather than rendered. A table is laid out in aligned columns when it fits and as one block per row when it does not, which is what usually happens to a six-column table at 70 characters. Aligned tables and fenced code use a fixed-width font, line by line, while prose stays in the normal chat font. The installer copies one from this machine's Windows fonts to `mono.ttf` (a local copy, never redistributed); without it, tables simply use the block form. Headings and lists are kept, bold and inline-code marks are stripped, and typographic punctuation becomes ASCII because the 2010 fonts lack it. Column widths count characters, not bytes, so accented text still lines up. The untouched reply is always in the companion, which also writes every finished reply to `state/replies/*.md`.

**Why a bank of 65,535 fonts.** The client caches a font file forever once it has loaded it. Changing it on disk later has no effect until the game restarts. Pre-created, never-loaded filenames can be filled in just before first use. Slots are NTFS hard links to 128 shared placeholders, so a fresh bank is about 5 MB. Publishing replaces one name atomically.

**Hybrid long replies.** [wow-claude](https://github.com/chelinho139/wow-claude) demonstrated returning replies through pre-made load-on-demand addons. Earlier probes on this client (2026-09-24) measured:
- A load-on-demand addon's file is read fresh on its first load after launch, and again after each `/reload`.
- 60 KB of hostile text (quotes, long brackets, escapes, UTF-8, NULs) arrived intact in 1–2 ms.
- `PlaySoundFile` returns `1` for an empty file and a valid one alike, so it cannot tell the addon a reply is ready.

The integrated hybrid keeps readiness checks, progress and short replies on fonts. When a finished reply exceeds 4,060 bytes (including its small agent/model header), the companion atomically writes it into the unused slot advertised by the addon, then publishes a font packet announcing it. The game loads that slot once. Sixteen slots are available per UI load; `/reload` frees them. Missing, disabled or exhausted slots fall back to fonts. A failed or invalid load disables the hybrid until the next UI load and retries the reply through fonts. Slots are never loaded in combat: a long reply announced during a fight is held, its slot reserved, and it loads as soon as combat ends, without spending font slots meanwhile or turning the hybrid off.

Only one fixed Lua assignment is generated: `AgentBridgeHybridData = "<lowercase hex>"`. The writer validates the entire source template immediately before publication. Reply text never enters Lua source directly. The receiver clears the global before and after loading, decodes hex, and checks the session, request, slot, final state, exact length and checksum against the font descriptor before displaying anything. The payload limit remains 60,000 bytes including metadata. There is no `loadstring` or reply-driven function call. This protects against reply text becoming code; it is not a sandbox for someone who can independently replace local addon files.

The 16 slots appear as `AgentBridgeReply01` through `AgentBridgeReply16` in AddOns. Run the updated installer and fully exit/relaunch WoW once so it discovers these new folders; restart the companion to load the updated publisher too. Until the slots are discovered, normal font replies continue working. Subsequent edits to existing code files only need `/reload`; new filenames require a full client restart; font paths still cannot be reused until a full game restart.

**Performance.** Published fonts now map the 8,192 data codepoints onto 16 shared glyph shapes with exactly the same widths and outlines as before. This reduces file size and glyph loading work without changing the font protocol or rebuilding the installed bank. Existing cached fonts and the self-test remain valid. Glyph measurement uses a 1 ms frame budget (up to 256 measurements when the profiling clock is unavailable). The strip repaints only changed bits, and the transcript defers layout while hidden and reuses unchanged line properties.

Use `/ab perf on`, reproduce a transfer, then `/ab perf` to print call counts, average/max addon timings and the worst frame interval. `/ab perf reset` clears the measurements; `/ab perf off` stops recording. Font loading, receive steps (including font/hybrid loading), hybrid loading, transcript formatting/layout and strip painting are timed separately. Recording is opt-in and does not reset the client's shared profiling clock. Frame intervals cover the whole game, including loading screens, so they can reveal a dip without proving this addon caused it. For comparable measurements, stay in the same area and avoid loading screens or alt-tabbing during the recording. The new implementation is covered by Lua 5.1 simulation tests; live frame-time improvement still needs measurement on this client.

## What changed for 3.3.5a

- **API port.**
  - `SetColorTexture` → `SetTexture(r,g,b,a)`.
  - `BasicFrameTemplate` → backdrop frames.
  - `SetShown`, `SetSize` and `GetServerTime` replaced.
  - Timers via `OnUpdate`.
  - Item links use the 3.3.5a `item:id:enchant:gems…:level` format and `ChatEdit_InsertLink` hooks.
  - Uncached items are fetched with a scan tooltip.
- **Calibration table instead of a linear formula.** Glyph advances are rounded to whole pixels, so a linear decode drifts (±0.65 of a byte value, measured). At login the addon measures the width of all 16 nibble values from a static `selftest.ttf`, then decodes by nearest-width lookup, which holds under any rounding. It picks the first font size (64, 96, 128, 48, 32) that decodes 2,032 check glyphs perfectly, and re-calibrates if the window is resized. `/ab test` prints the margin per size.
- **Scale-independent strip.** The strip and meter have no parent frame, at scale 1. UI scale can't move or resize them, and Alt+Z doesn't hide them. The companion finds the WoW window and predicts the strip position from the client size. It tries six anchors and two pixel-aspect models, then locks on. No manual crop is needed.
- **Alt-tabbing is fine.** Reading screen pixels would mean any window over the strip breaks the channel — including the companion itself. When the screen copy fails, the companion asks the WoW window to render itself (`PrintWindow` with `PW_RENDERFULLCONTENT`), which returns the game's own content while it is covered or in the background. The cheap screen path is still used when WoW is visible, because window rendering costs ~75 ms against ~17 ms.
- **Automatic bank recycling.** While no client from this game folder is running, the companion rewrites `Epoch.lua`. The next client process sees a new value at login and restarts at slot 1. This is safe because nothing has been loaded yet in that process.
- **Faster, cheaper transfers.**
  - Packets are 4 KB (up from 512 bytes), so most replies fit in one slot.
  - Polls peek at the header before measuring a whole packet.
  - Measuring is budgeted by frame time, not a fixed glyph count.
  - A slot may be rewritten with fresher content until its deadline.
  - Multi-part transfers are served from a frozen copy, so all fragments share one revision even while the agent is still writing.
  - Polling backs off while nothing changes.
  - Prompts stop repeating once the companion acknowledges them.
  - Long final replies can use one checked addon slot instead of many font fragments.
- **Three agents.**
  - Claude Code runs `claude -p --output-format stream-json`, with live streaming and tool-activity status. Follow-ups use native `--resume`.
  - Codex runs `codex exec --json`, resuming threads with `exec resume` (its sandbox goes through a config override there), and falls back to passing history as data.
  - Hermes runs its headless CLI in its own Python environment, with JSONL streaming, native session resume, project tools, persistent memory, reusable skills, and bounded delegation. Optional browser, vision and speech tools use its separate bridge profile.
  - All three can search the web, which is on by default. The status line shows each real query, so you can tell a search that happened from a reply that only claims a source.
  - Prompts go on stdin, never on a command line.

### Hermes setup

Install [Hermes Agent](https://github.com/NousResearch/hermes-agent) separately. The adapter was tested with release `v2026.9.24` (`0.21.5`). It finds the Python executable under `~/.hermes/hermes-agent/venv` or `.venv`; another installation can be selected with the companion's `--hermes <python-path>` option. Hermes dependencies stay out of the companion's environment. The Anthropic provider needs Hermes's optional Anthropic SDK.

For a Hydra bot using `ModelProvider` declarations in `bot.py`, import its configured routes with Hermes's Python:

```powershell
& "$env:USERPROFILE\.hermes\hermes-agent\venv\Scripts\python.exe" -m tools.configure_hermes --hydra '<Hydra folder>'
```

This reads configuration as data, copies only enabled model API keys and the Tavily key, and creates `~/.hermes/agentbridge`. It never starts or modifies the Discord bot. It refuses to overwrite an existing profile. The profile contains local `.env` credentials, `config.yaml` provider routes, and `models.json` aliases. No credentials belong in this repository. Set `--hermes-home <folder>` to use another bridge profile.

In game, right-click a chat and choose **Use Hermes**, or type `/ab agent hermes`. Pick an alias with **Model...** or `/ab model deepseek`; `/ab model default` uses the companion's saved Hermes choice, then the profile default. The companion's Model dropdown lists the configured aliases. To configure routes manually, `models.json` has this shape:

```json
{"default":"deepseek","models":{"deepseek":{"provider":"bridge-deepseek","model":"deepseek-flash","reasoning":"none"}}}
```

Each `provider` names a route in Hermes's `config.yaml`. Optional `reasoning` defaults to `none`; Gemini models that require thinking should use `low`. Availability and charges depend on the selected API provider and account tier. Missing or invalid aliases fail without silently switching providers.

Enable project tools and persistent memory in a configured profile using Hermes's Python:

```powershell
& "$env:USERPROFILE\.hermes\hermes-agent\venv\Scripts\python.exe" -m tools.upgrade_hermes
```

The companion's **Access** dropdown applies to Hermes:

| Access | Hermes capabilities |
| --- | --- |
| `read-only` | Read and search files; use existing memory and skills without changing them; conversation search, task lists and delegation. |
| `workspace-write` | Also create and patch files inside the selected **Work folder**, and save memory and skills. |
| `workspace-write+shell` | Also run terminal commands and tests, manage its processes, and interact with browser forms. |

File tools resolve paths and reject edits outside the work folder, including symlinks/junctions, Git metadata and Windows alternate data streams. **Shell access runs commands with your Windows account's permissions; it is not an OS sandbox.** Memory, skills, sessions and media caches are stored in the separate Hermes bridge profile even at read-only project access. Project `AGENTS.md` instructions are loaded normally. Native desktop Hermes settings and conversations remain separate.

Memory persists across new chats, which is also why it is guarded: an instruction planted in a web page could otherwise steer every later run. Once a run has used web search, a web page, the browser, image analysis or conversation search, memory and skills are locked for the rest of that run, including its children. Hermes then says what it would have saved, and you can ask it to save that in a new message. Requests sharing a Hermes profile queue behind one another so memory and skills cannot be updated by competing bridge processes. Each run can delegate to at most two children, with no recursive spawning and a three-minute timeout per child. Children inherit the selected tools and cannot write the parent's memory. Automatic background review and title-generation calls stay disabled.

Optional tools require their dependencies before enabling them:

- **Browser:** install `agent-browser@0.26.0` with npm under `<Hermes bridge home>/runtime`, run its `agent-browser install` command to download Chrome for Testing, then pass the resulting Chrome path to `tools.upgrade_hermes --browser-executable '<path>'`. The bridge launches headless sessions with GPU rendering disabled. Reading pages is available at all access levels; clicks, typing and key presses require `workspace-write+shell`. Turning **Web search** off hides both search and browser tools.
- **Image analysis:** add `--vision` when the `bridge-gemini` provider route is configured. This uses that provider to inspect image URLs or local image files; the WoW panel does not upload screenshots automatically.
- **Speech:** install `edge-tts==7.2.7` in the Hermes environment and add `--speech`. Audio files are saved under the bridge profile's `cache/audio` folder; the WoW panel does not play them automatically.

Browser workers close their sessions on normal shutdown. On Windows, each Hermes
worker and shared MCP worker also joins a private job object before starting tools:
when that worker exits or is forcibly stopped, Windows terminates its remaining
child processes, including detached browsers and failed browser launches. This
does not target personal browsers or the WoW client. Processes started by a worker
are scoped to that worker's lifetime; persistent services should be started separately.
If Windows cannot establish this protection, the worker fails before launching tools.
The browser daemon additionally has a five-minute idle shutdown, enforced after
profile environment loading. Hermes's own inactivity cleanup may close it earlier.

For an opt-in browser cleanup check on Windows, run
`python -m tools.check_browser_cleanup` from the repository using Hermes's venv
Python (which includes `psutil`). It uses one disposable Chrome for Testing
session at a time, on `about:blank`, and verifies normal close, forced worker
termination, and idle expiry against the exact test processes. It does not call
an AI provider. A short three-second idle deadline is used only for that test;
production workers use five minutes. Unit tests also cover detached descendants
and ensure an unrelated process survives.

Pass all optional flags you want to keep enabled when rerunning the upgrade command. It backs up the original config, preserves provider routes, and creates a starter Agent Bridge skill. General plugin discovery, MCP servers and shell hooks remain disabled. Image generation, scheduled jobs and messaging integrations are not wired into this adapter: they need a supported image provider or a persistent scheduler/integration setup, respectively. See the upstream [tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools) and [memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory/) documentation.

Runs allow 40 tool iterations and use the companion's timeout (30 minutes by default), with a 30-second shutdown margin. Changing model, work folder, Access, Web search or optional tools starts a fresh native session using recent bridge history. Partial output is never treated as a successful completion. Backend-only upgrades need a companion restart; existing in-game Hermes chats continue to work without reloading WoW.

### Shared extras for Claude Code and Codex

Claude Code and Codex already provide project tools and native delegation. The
bridge can also give both CLIs the configured Hermes browser, image-analysis and
speech tools through a private MCP process. This does not run a Hermes agent or
switch the chat's main model. Vision uses the configured Gemini route and its API
billing; speech uses Edge TTS. Credentials remain in the Hermes bridge profile.

After setting up the optional Hermes tools above, install its MCP dependencies
in the Hermes environment (the tested SDK is `mcp==2.0.0`, with
`httpx2==2.7.0` and `starlette==1.3.1`), then run from this repository:

```powershell
.\.venv\Scripts\python.exe -m tools.configure_extras
```

The helper enables `coding_agents` in `bridge-tools.json`, backs up that file,
and installs an Agent Bridge skill into `~/.claude/skills` and `~/.agents/skills`.
Existing skills are preserved. Use `--home` for a custom Hermes bridge profile.
Restart the companion after updating its Python code; WoW does not need a reload.
Set `coding_agents` to `false` to stop adding these extras to future bridge runs.

| Capability | Claude Code | Codex |
| --- | --- | --- |
| Persistent memory | Native auto-memory enabled per bridge invocation, only while web search is off | Native memory use enabled; generation only while web search is off |
| Skills | Native `Skill` tool allowed | Native skills discovery |
| Delegation | Native `Agent` tool allowed | Native subagents enabled, at most two open children per session |
| Browser, image analysis, speech | Bridge MCP tools allowed according to Access and optional feature flags | Same MCP tools and flags, with automatic approval review |

**Memory and web search.** A page an agent reads could try to plant an
instruction that memory would carry into every later run. So whenever **Web
search** is on, neither CLI may write memory, with or without these extras and
whatever your own settings say. Claude Code's auto memory is off (by setting and
by `CLAUDE_CODE_DISABLE_AUTO_MEMORY`); its one switch also covers reading.
Codex still reads existing memories but generates none. Hermes follows the same
rule per run (see its access levels above). Turn Web search off in the
companion when you want a chat to build memory.

Each agent keeps its own native memory; this does not synchronize memories with
Hermes or ChatGPT. Codex generates memories asynchronously from eligible idle
sessions, so enabling it does not guarantee immediate recall after a short chat.
See [Codex memory](https://learn.chatgpt.com/docs/customization/memories).

The shared browser uses isolated headless Chrome sessions with GPU rendering
disabled. Browsing follows **Web search**; clicking, typing and key presses
require `workspace-write+shell`. Image analysis and speech follow their separate
feature flags. Audio defaults to the Hermes cache; custom output paths require
write access and must remain inside the work folder. Images and audio are not
automatically captured or played inside WoW.

Claude receives an explicit tool allowlist. Codex uses a granular approval policy
and automatic review for eligible MCP requests, while shell/file escalation and
permission-expansion requests remain disabled. Its selected sandbox stays in
place. The review may reject a tool request; the agent should report that result.
These options apply only to bridge-launched runs, not the CLIs' global settings.
The server also rejects disabled tools even if called directly. See
[Codex auto-review](https://learn.chatgpt.com/docs/sandboxing/auto-review).

Verified with Claude Code `2.1.223`, Codex `0.155.0-alpha.16.4` and the Hermes
version above: native skill discovery and a single child delegation in both
CLIs; browser navigation and generated speech through both adapters; browser
forms, image analysis and access restrictions directly through the shared MCP
server. Older CLI versions may not understand these options. The new extras
have not yet been verified through a live in-game prompt.

This setup does not add persistent scheduling, messaging integrations or a
shared image-generation provider. Desktop-only plugins and automation tools do
not automatically become available to CLI runs launched by the companion.

## Install

Requirements: Windows with NTFS, Python 3.12+ with tkinter, and a 3.3.5a client in windowed or borderless mode. You also need at least one configured backend: the [Claude Code](https://docs.claude.com/en/docs/claude-code) CLI, the Codex CLI (also bundled with the Codex desktop app), or Hermes with a credentialed provider profile as described above.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m tools.install_addon 'E:\Games\ChromieCraft_3.3.5a\Interface\AddOns\AgentBridge'
```

The installer copies the Lua, creates `selftest.ttf` and `Epoch.lua`, and builds the bank (about 3 minutes, resumable). It records the addon path for the companion. Rerunning it never overwrites existing slots. After the first install, **restart WoW** so the client lists the addon. For updates to existing Lua files, `/reload` is enough. If an update adds new filenames, fully restart the 3.3.5a client so it discovers them.

If the font format ever changes, the installer rebuilds the bank — but only while WoW is closed, because the client keeps serving any font it has already loaded until it restarts. `python -m tools.reset_bank '<addon path>'` forces that rebuild on demand.

## Run the companion

```powershell
.\.venv\Scripts\pythonw.exe 'Launch Agent Bridge.pyw'
```

To restart the companion, run the included helper from the repository folder:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\Restart Agent Bridge.ps1"
```

The helper closes this installation's companion gracefully and opens it again. If a job keeps it open, it asks you to let the job finish instead of starting another companion. For a desktop shortcut, use the same command with an absolute path to the script; add `-WindowStyle Hidden` before `-File` to hide the helper's console. The companion window still opens normally.

On first launch, pick the folder the agent should work in. In the window you can:
- choose which agent new chats use: **Claude Code**, **Codex**, **Hermes** or **Mock agent** (the mock tests the transport without an agent)
- choose the access level (see below)
- pick each agent's default model: the dropdown follows the agent buttons and lists the models that CLI offers on this machine, read from its own data, so new ones appear without an update. You can also type any name the CLI accepts; blank uses the CLI's own default
- turn **Web search** on or off

Capture starts automatically and finds the strip by itself.

**Chats and sessions.** Each chat in the panel is its own agent conversation. A follow-up resumes that chat's previous session — `--resume` for Claude Code, `exec resume` for Codex — across `/reload` and game restarts. Up to three agents run at once, one per chat; a second prompt in the same chat waits for the first, since it resumes the session that one produces.

**Agents and models per chat.** So one chat can be on Codex while two others are on Claude Code, each chat has its own agent:
- the one you set in game (`/ab agent codex`, or right-click the chat);
- otherwise the agent it last used, since only that one can resume its session;
- for a new chat, the companion's selection.

The model works the same way: `/ab model <name>` for the chat, otherwise the companion's default for that agent. The chat list shows each chat's agent, and hovering shows the model. Changing a chat's agent starts a new session with the new agent, given the chat's recent turns as history.

**Continuing a conversation you started elsewhere.** To pick up a conversation you had at your desk, click **Continue a conversation…** and choose it from the list; the work folder switches to match it. The next prompt that agent answers continues it, so send from a new chat or one set to that agent. Claude Code branches with `--fork-session`, leaving the original transcript untouched. Codex has no branching, so in-game turns are appended to that thread.

| Access | Claude Code | Codex |
| --- | --- | --- |
| `read-only` (default) | `--permission-mode dontAsk`, allowing `Read`, `Glob`, `Grep` | `--sandbox read-only` |
| `workspace-write` | `--permission-mode acceptEdits` | `--sandbox workspace-write` |
| `workspace-write+shell` | `acceptEdits`, also allowing `Edit`, `Write`, `Bash` | `--sandbox workspace-write` |

A headless run cannot stop to ask for approval, so anything a level does not allow is refused. Claude's `workspace-write+shell` level also allows shell commands unattended; Codex can run commands inside its selected sandbox at either write level. With web search on, Claude also gets `WebSearch` and `WebFetch`, and Codex gets `web_search="live"`. Shared extras add the tools and automatic MCP review described above without disabling the sandbox.

## In game

| Action | How |
| --- | --- |
| Show / hide the panel | `/ab` (also `/agent`, `/claude`, `/codex`), minimap button, or a key binding |
| Resize the panel | Drag the labelled grip in the bottom-right corner. Width and height are saved automatically (minimum 560 × 320) |
| Type a prompt quickly | `/ai <message>` from the chat box, right-click the minimap button, or bind "Open panel and type a prompt" |
| Retry a failed or interrupted reply | **Retry** or `/ab retry` resends the selected chat's last prompt with a fresh request ID, current game context, and the chat's current agent/model. It preserves linked items/spells and any new draft, and is disabled while that chat is working. The last prompt survives `/reload` |
| Link an item, spell or quest | Focus the input box, then Shift-click or drag it in; with `/ai`, Shift-click into the chat box as usual |
| Scroll back through a chat | Mouse wheel or the scrollbar; Shift+wheel pages |
| Start another chat | **+ New chat** in the list, or `/ab new [name]`. The others keep running |
| Switch chats | Click one in the list, or `/ab chat <number or name>`; `/ab chats` lists them |
| Rename or delete a chat | Right-click it in the list, or `/ab rename <name>` and `/ab delete` |
| Choose a chat's agent or model | Right-click it in the list, or `/ab agent claude\|codex` and `/ab model <name>\|default`; with no name, they show the current choice |
| Copy text inside the panel | **Select text**, then drag to highlight a passage and press Ctrl+C. **Last reply** / **Whole chat** choose the scope; **Done** or Escape returns to the formatted view. `/ab copy` and `/ab copy all` also open this view |
| Open a web source | Click its blue row under **Sources**. The running companion opens it in your default browser; hover to see the full URL |
| Inspect an item or spell in a reply | Hover its coloured link for the tooltip; click to open its detail popup. With the prompt box focused, Shift-click inserts the link into your prompt |
| Replies in the chat frame | `/ab echo short` (default, 800 characters), `full`, `off`, or a number |
| Game context | `/ab context on` (default), `off`, or `show` to see exactly what is sent |
| Continue an existing chat | In the companion: **Continue a conversation…**, pick one, then send from the game |
| Check the font channel | **Self-test** or `/ab test` (prints per-size results) |
| See through the strip | `/ab alpha 0.5` (0.2 to 1) |
| Read the full, unformatted reply | **Saved replies** in the companion |
| Channel state | `/ab status` |
| Measure transfer performance | `/ab perf on`, then `/ab perf`; `off` stops and `reset` clears measurements |
| Move the strip | `/ab strip top` (or `topleft`, `topright`, `bottom`, `bottomleft`, `bottomright`); the companion follows |

The panel lists your chats down the left: the one you are reading is highlighted, `...` marks one still working, and `*` one with a reply you have not read. Each chat is a scrolling transcript. Sending a prompt scrolls it into view, and updates to a reply keep your place, so you can read back while it arrives. Under a prompt still under way, a line shows what the agent is doing (how many actions so far and the latest one, such as a file it read or a search) and a running clock. The last 30 exchanges per chat (up to 200 KB in all) are kept across `/reload` and restarts; the companion keeps every reply permanently.

A reply that finishes while you are not reading it — the panel is closed, or you are in another chat — is copied into the chat frame, labelled with its chat, with item links intact.

**What the agent is told.** Each prompt carries a small header: which chat it belongs to, the chat's name, and, unless `/ab context off`, your character's state when you sent it:
- name, level, race, class, faction and guild
- zone, subzone, map coordinates and any instance
- money, talent points per tree and professions

Anything you link is spelled out with its ID, followed by the text of its in-game tooltip (up to six links, 700 bytes each, as room allows), so the agent answers from the item's actual stats. The companion passes game state to Claude Code as part of the system prompt, and to Codex and Hermes ahead of your message, marked as data about your character rather than instructions.

**Gear, bags and learned recipes.** With context enabled, the addon maintains
character snapshots using ordinary game APIs:

- **Gear:** equipped slots 0–19, item names, full item strings (including enchant
  and gem fields), item levels and available `GetItemStats` values. These stats
  are not complete tooltips and do not describe every proc or set effect.
- **Bags:** occupied slots in the backpack and bags 1–4, with item names, full
  item strings and stack quantities. Bank, guild bank, mail and keyring are excluded.
- **Recipes:** learned recipe names, recipe spell IDs and output item IDs when
  available. Open each of your own profession windows to scan it. Clear search
  and makeable filters, choose all categories/slots, and expand categories for a
  complete scan. The addon does not change these controls. Linked professions
  belonging to other players are ignored. Recipe reagent lists are not included.

Gear and bag scans are triggered by events, debounced, and spread over frames;
they pause during combat. Recipe scans also run incrementally while their window
is open. Partial scans preserve previously observed recipes, and never claim that
an omitted recipe is unlearned. Recipe caches survive `/reload` and are scoped to
realm and character. Unlearning a profession removes its cache from future prompt
references. Use `/ab context show` to see record counts, coverage and scan age.

Only sending a prompt starts data transfer. The prompt identifies the exact
snapshot revisions it needs; the companion requests missing revisions, receives
checked `CPBC` pages, and commits each full section atomically. Changes can use
row patches; a missing patch baseline automatically falls back to a full section.
The companion does not start the agent until every referenced section is present.
Unchanged data is reused across chats/backends and companion restarts. Cancelling
an incomplete send does not allow a later upload to resurrect the old prompt.

The first sync, especially a large recipe collection, adds transfer time. Later
prompts send revision references and changed rows. Each data page remains below
the existing 8,000-byte packet-message limit, separately from the typed prompt.
Sections are capped at 2,000 records and 160 KB; a limit or incomplete game data
is explicitly labelled partial. `/ab perf` includes `character-context` timings.

All three agents receive freshness summaries and absolute paths to immutable
UTF-8 snapshot files under the companion's `state/characters` directory. They
read relevant files with their native file tools when answering inventory or
recipe questions, instead of receiving the entire recipe catalogue in every
model prompt. Observations are labelled current, cached or stale and timestamped;
they are not live queries while the agent is working.

`/ab context off` stops new sharing/scanning and cancels unfinished context syncs.
Previously saved local snapshots and earlier agent conversation context remain.
After installing these changes, restart the companion and run `/reload` once;
then open profession windows. All Lua changes are in existing addon files, so a
full WoW restart is not required.

Verified with production Lua 5.1 simulations: initial and multi-page sync,
unchanged reuse, bag deltas, missing-baseline recovery, partial/linked recipe
scans, opt-out and bounded event scans. Claude Code, Codex and Hermes also read
synthetic gear/bag/recipe snapshot files successfully through their real adapters.
Live in-game confirmation and actual client frame-time measurements remain pending.

Replies render as plain text. `[Name](item:ID)` references become item links; the same notation with `spell:ID` produces spell links. Markdown web links and bare HTTP/HTTPS URLs also appear as clickable source rows below the reply, up to 32 distinct sources. The full destination appears on hover. Clicking sends a separate browser request to the companion and uses no agent tokens. The status line reports the browser result; if the companion does not acknowledge within 45 seconds, start it and click again. Repeated captures of one click do not open duplicate tabs, including after a companion restart. Only HTTP/HTTPS URLs without embedded credentials are accepted.

**Select text** shows a selectable snapshot inside the panel, including any reply still streaming. New text does not disturb your selection. The snapshot preserves the original reply and source URLs; typing into it does not edit the chat. Close and reopen it to include newer text.

Reply text is escaped and cannot run as code or perform a game action. A web source opens only after a user clicks its source row.

## What has been verified, and what has not

**Verified in the live client** (ChromieCraft, 3.3.5a, 1920×1080 windowed, 2026-09-22 through 24):

- **The whole round trip.** Real Claude Code replies were sent from the game, answered, and delivered back into the panel. That proves the channel's central assumption: a pre-created font file loads fresh from disk on its first use.
- **The font self-test** passes at every size, with byte values ~4 px apart and 0 of 2,032 check glyphs misread. The original byte-per-glyph encoding failed here — sizes 128, 192 and 256 returned identical widths — which is how the capped em was found.
- **Reading the strip at 20% opacity** over the game scene, and **through the window** while WoW was covered by another app.
- **Latency:** Claude answered in ~4 s, and the reply was in the panel ~10 s after the companion picked the prompt up.
- **The scrolling reply panel and web search**, in real WoW questions answered through the game.
- **Source links opening the default browser, reply rendering after reload, and item/spell link interaction**, confirmed by the user on 2026-09-24.

**Not yet exercised live** (covered by simulation and unit tests only):

- Parallel chats and the chat list, per-chat agents and models, the game context, linked tooltips, the progress line, `/ai`, the copy box and chat-frame echo.
- The multi-prompt transcript, beyond the scroll-on-open fix.
- Replies longer than one 4,060-byte packet, and streaming previews.
- Aligned tables in the fixed-width font.
- Bank recycling across a game restart, and the Codex backend through the game (Codex works through the companion on its own).

In simulation, a short reply appears ~3.5 s after the agent finishes and a 14.7 KB reply transfers in ~9 s. Only the first 4,060 bytes preview while the agent is still writing. The in-game preview is capped at 60 KB; the companion keeps everything.

**Limits:**
- WoW may be covered or in the background, but not minimized: a minimized window stops rendering, so nothing can be read.
- If WoW starts while the companion is not running, that game session continues from the saved slot instead of recycling. With 65,535 slots, that is rarely a problem.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

The end-to-end tests load the real addon Lua in Lua 5.1 against a stubbed 3.3.5a API. That client model reads each font path once per process, then caches it, and rounds every glyph to whole pixels at 1080p. The tests drive the real Python publisher through the strip. They cover:
- single and multi-part UTF-8 replies
- consecutive prompts without ever reloading a slot
- a companion that starts late
- `/reload` mid-request
- window resize recalibration
- epoch recycling
- hybrid hostile-text delivery, stale/corrupt payload rejection and font fallback
- hybrid slot reuse after reload, exhaustion and concurrent-chat isolation
- compact-font widths, fixed source validation and locked-file recovery
- hidden transcript deferral, incremental strip painting and performance reporting
- explicit browser-click delivery and acknowledgement, duplicate suppression, URL validation and offline timeout
- selectable transcript snapshots, streaming selection stability and clickable source rows
- native item/spell hyperlink events, Shift-click insertion, uncached items and link-row resizing

Unit tests cover the wire formats and pixel sampling under display scaling and low opacity, fonts and bank hard-link isolation, publisher deadlines, both agent parsers and their permission flags, model discovery, strip geometry and the installer. UI tests drive the panel itself: Markdown formatting, scroll position, and the transcript across prompts, `/reload` and its size cap. They also run two chats at once end to end, give chats their own agents and models, and check the envelope each prompt carries (chat, name, agent, model, game context, tooltips), the progress line's clock, chat-frame echo, `/ai`, the copy box, renaming and deleting through the dialogs, and migrating a single-conversation install to chats. Companion tests cover the per-chat scheduler, how each prompt's agent and model are chosen (and unusable names refused), the header that names them in replies, the inbox migration and how game context reaches each agent.

## Credits and licence

The idea is [0xInuarashi](https://github.com/0xinuarashi)'s: an addon cannot open a socket, so
prompts leave as pixels and replies return as font metrics.
[wow-forever-codex](https://github.com/0xinuarashi/wow-forever-codex) does that for WoW: Forever.

This is a separate implementation for the 3.3.5a client, written against that published design
rather than copied from it. The upstream repository carries no licence file, so it grants no
redistribution rights; nothing here is derived from its source.

Thanks to [chelinho139](https://github.com/chelinho139) for
[wow-claude](https://github.com/chelinho139/wow-claude), whose use of load-on-demand addons
for replies inspired the optional hybrid long-reply transport in this project.

This code is MIT licensed (see [LICENSE](LICENSE)). Two things it deliberately does not ship:

- `mono.ttf` — the installer copies a fixed-width font from the machine's own Windows fonts. That
  copy stays local; redistributing Microsoft's fonts is not permitted.
- The font bank and `selftest.ttf` — generated at install time, and the bank alone is 65,535 files.

World of Warcraft is a trademark of Blizzard Entertainment, which has nothing to do with this
project. ChromieCraft is an independent server; check its rules before using anything here.
