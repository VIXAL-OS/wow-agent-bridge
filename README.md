# Agent Bridge for ChromieCraft (WoW 3.3.5a)

Chat with **Claude Code** or **Codex** from a panel inside World of Warcraft 3.3.5a, keep playing while the agent works, and get a chime and minimap badge when the reply is ready.

This is a port of [0xInuarashi/wow-forever-codex](https://github.com/0xInuarashi/wow-forever-codex), which does the same for WoW: Forever. The core idea is theirs: an addon can't open a connection, so prompts leave the game as a pixel strip and replies come back as font glyph widths. This version re-implements it for the Wrath 3.3.5a client that ChromieCraft uses, and adds a Claude Code backend alongside Codex.

No DLL injection, no memory access, no synthetic input. The addon uses documented 3.3.5a APIs only (textures, `SetFont`, `SetText`, `GetStringWidth`). The companion reads a small screen rectangle and writes files in the addon folder.

## How the two channels work

| Direction | Carrier | Receiver reads |
| --- | --- | --- |
| Game → companion | 128 × 8 cell strip at the top of the screen, any opacity | Differences between cell pairs in a screen capture |
| Companion → game | An unused font file in the addon's bank | Glyph advance widths via `GetStringWidth` |

**Prompts.** The addon splits the UTF-8 prompt (max 8,000 bytes, including the header, linked tooltips and game context described under [In game](#in-game)) into 64-byte checksummed frames and flashes them on the strip. Every bit is a *pair* of neighbouring cells, one light and one dark, and the companion reads the difference between them. Because only the difference matters, the strip decodes at any opacity, so you can turn it down with `/ab alpha 0.5` and still see the UI through it. A pair straddling a hard UI edge reads as low contrast and rejects the frame rather than guessing. The strip also carries a *control* frame: which font slot the addon will load next, how many milliseconds until it does, and which reply fragment it needs. The strip is only shown during an exchange.

**Replies.** A reply packet is 4,096 bytes: a 32-byte header, 4,060 bytes of text, and an Adler-32 checksum. The companion writes it into the requested slot as a TrueType font. Each byte uses two glyphs, one per 4-bit nibble: glyph `U+E000 + i` has advance `(2 + value) × 128` units at 1,024 units/em. Lua measures each glyph, recovers the nibbles, validates the packet, joins the fragments, and displays plain text with native item links. Polls read only the 32-byte header first, so an unchanged status, an empty slot or a stale one costs a fraction of a second instead of a full packet.

**Why four bits per glyph.** Measured in the live 3.3.5a client: the rasterised em is capped around 32 px, and asking for a bigger font size changes measured widths not at all — sizes 128, 192 and 256 all returned identical widths. A whole byte per glyph (the original's `em/64` step) therefore lands byte values ~0.5 px apart, and they collide. A nibble step of `em/8` keeps them ~4 px apart at the cost of two glyphs per byte.

**Markdown, in a narrow panel.** Replies are reformatted rather than rendered. A table is laid out in aligned columns when it fits and as one block per row when it does not, which is what usually happens to a six-column table at 70 characters. Aligned tables and fenced code use a fixed-width font, line by line, while prose stays in the normal chat font. The installer copies one from this machine's Windows fonts to `mono.ttf` (a local copy, never redistributed); without it, tables simply use the block form. Headings and lists are kept, bold and inline-code marks are stripped, and typographic punctuation becomes ASCII because the 2010 fonts lack it. Column widths count characters, not bytes, so accented text still lines up. The untouched reply is always in the companion, which also writes every finished reply to `state/replies/*.md`.

**Why a bank of 65,535 fonts.** The client caches a font file forever once it has loaded it. Changing it on disk later has no effect until the game restarts. Pre-created, never-loaded filenames can be filled in just before first use. Slots are NTFS hard links to 128 shared placeholders, so a fresh bank is about 5 MB. Publishing replaces one name atomically.

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
- **Two agents.**
  - Claude Code runs `claude -p --output-format stream-json`, with live streaming and tool-activity status. Follow-ups use native `--resume`.
  - Codex runs `codex exec --json`, resuming threads with `exec resume` (its sandbox goes through a config override there), and falls back to passing history as data.
  - Both can search the web, which is on by default. The status line shows each real query, so you can tell a search that happened from a reply that only claims a source.
  - Either way, prompts go on stdin, never on a command line.

## Install

Requirements: Windows with NTFS, Python 3.12+ with tkinter, and a 3.3.5a client in windowed or borderless mode. You also need at least one of these, logged in: the [Claude Code](https://docs.claude.com/en/docs/claude-code) CLI, or the Codex CLI (also bundled with the Codex desktop app).

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m tools.install_addon 'E:\Games\ChromieCraft_3.3.5a\Interface\AddOns\AgentBridge'
```

The installer copies the Lua, creates `selftest.ttf` and `Epoch.lua`, and builds the bank (about 3 minutes, resumable). It records the addon path for the companion. Rerunning it never overwrites existing slots. After the first install, **restart WoW** so the client lists the addon. After later updates, `/reload` is enough.

If the font format ever changes, the installer rebuilds the bank — but only while WoW is closed, because the client keeps serving any font it has already loaded until it restarts. `python -m tools.reset_bank '<addon path>'` forces that rebuild on demand.

## Run the companion

```powershell
.\.venv\Scripts\pythonw.exe 'Launch Agent Bridge.pyw'
```

On first launch, pick the folder the agent should work in. In the window you can:
- choose which agent new chats use: **Claude Code**, **Codex** or **Mock agent** (the mock tests the transport without an agent)
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

A headless run cannot stop to ask for approval, so anything a level does not allow is refused. Only `workspace-write+shell` runs shell commands unattended — git, tests, tools — so choose it deliberately. With web search on, Claude also gets `WebSearch` and `WebFetch`, and Codex gets `web_search="live"`. There is no bypass mode.

## In game

| Action | How |
| --- | --- |
| Show / hide the panel | `/ab` (also `/agent`, `/claude`, `/codex`), minimap button, or a key binding |
| Type a prompt quickly | `/ai <message>` from the chat box, right-click the minimap button, or bind "Open panel and type a prompt" |
| Link an item, spell or quest | Focus the input box, then Shift-click or drag it in; with `/ai`, Shift-click into the chat box as usual |
| Scroll back through a chat | Mouse wheel or the scrollbar; Shift+wheel pages |
| Start another chat | **+ New chat** in the list, or `/ab new [name]`. The others keep running |
| Switch chats | Click one in the list, or `/ab chat <number or name>`; `/ab chats` lists them |
| Rename or delete a chat | Right-click it in the list, or `/ab rename <name>` and `/ab delete` |
| Choose a chat's agent or model | Right-click it in the list, or `/ab agent claude\|codex` and `/ab model <name>\|default`; with no name, they show the current choice |
| Copy a reply out of the game | **Copy** or `/ab copy` (last reply), `/ab copy all` (whole chat); then Ctrl+C |
| Replies in the chat frame | `/ab echo short` (default, 800 characters), `full`, `off`, or a number |
| Game context | `/ab context on` (default), `off`, or `show` to see exactly what is sent |
| Continue an existing chat | In the companion: **Continue a conversation…**, pick one, then send from the game |
| Check the font channel | **Self-test** or `/ab test` (prints per-size results) |
| See through the strip | `/ab alpha 0.5` (0.2 to 1) |
| Read the full, unformatted reply | **Saved replies** in the companion |
| Channel state | `/ab status` |
| Move the strip | `/ab strip top` (or `topleft`, `topright`, `bottom`, `bottomleft`, `bottomright`); the companion follows |

The panel lists your chats down the left: the one you are reading is highlighted, `...` marks one still working, and `*` one with a reply you have not read. Each chat is a scrolling transcript. Sending a prompt scrolls it into view, and updates to a reply keep your place, so you can read back while it arrives. Under a prompt still under way, a line shows what the agent is doing (how many actions so far and the latest one, such as a file it read or a search) and a running clock. The last 30 exchanges per chat (up to 200 KB in all) are kept across `/reload` and restarts; the companion keeps every reply permanently.

A reply that finishes while you are not reading it — the panel is closed, or you are in another chat — is copied into the chat frame, labelled with its chat, with item links intact.

**What the agent is told.** Each prompt carries a small header: which chat it belongs to, the chat's name, and, unless `/ab context off`, your character's state when you sent it:
- name, level, race, class, faction and guild
- zone, subzone, map coordinates and any instance
- money, talent points per tree and professions

Anything you link is spelled out with its ID, followed by the text of its in-game tooltip (up to six links, 700 bytes each, as room allows), so the agent answers from the item's actual stats. The companion passes the game state to Claude Code as part of the system prompt, and to Codex ahead of your message, marked as data about your character rather than instructions. Only your own character's state is read, and only when you send.

Replies render as plain text. `[Name](item:ID)` references become item links; everything else is escaped. Nothing in a reply can run as code or perform a game action.

## What has been verified, and what has not

**Verified in the live client** (ChromieCraft, 3.3.5a, 1920×1080 windowed, 2026-09-22 and 23):

- **The whole round trip.** Real Claude Code replies were sent from the game, answered, and delivered back into the panel. That proves the channel's central assumption: a pre-created font file loads fresh from disk on its first use.
- **The font self-test** passes at every size, with byte values ~4 px apart and 0 of 2,032 check glyphs misread. The original byte-per-glyph encoding failed here — sizes 128, 192 and 256 returned identical widths — which is how the capped em was found.
- **Reading the strip at 20% opacity** over the game scene, and **through the window** while WoW was covered by another app.
- **Latency:** Claude answered in ~4 s, and the reply was in the panel ~10 s after the companion picked the prompt up.
- **The scrolling reply panel and web search**, in real WoW questions answered through the game.

**Not yet exercised live** (covered by simulation and unit tests only):

- Parallel chats and the chat list, per-chat agents and models, the game context, linked tooltips, the progress line, `/ai`, the copy box and chat-frame echo, all added after the last live session.
- The multi-prompt transcript, beyond the scroll-on-open fix.
- Replies longer than one 4,060-byte packet, and streaming previews.
- Aligned tables in the fixed-width font, and item-link tooltips. The panel is now a plain frame, and 3.3.5a may not send hover events there; if not, item links show as coloured names without tooltips.
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

Unit tests cover the wire formats and pixel sampling under display scaling and low opacity, fonts and bank hard-link isolation, publisher deadlines, both agent parsers and their permission flags, model discovery, strip geometry and the installer. UI tests drive the panel itself: Markdown formatting, scroll position, and the transcript across prompts, `/reload` and its size cap. They also run two chats at once end to end, give chats their own agents and models, and check the envelope each prompt carries (chat, name, agent, model, game context, tooltips), the progress line's clock, chat-frame echo, `/ai`, the copy box, renaming and deleting through the dialogs, and migrating a single-conversation install to chats. Companion tests cover the per-chat scheduler, how each prompt's agent and model are chosen (and unusable names refused), the header that names them in replies, the inbox migration and how game context reaches each agent.

## Credits and licence

The idea is [0xInuarashi](https://github.com/0xinuarashi)'s: an addon cannot open a socket, so
prompts leave as pixels and replies return as font metrics.
[wow-forever-codex](https://github.com/0xinuarashi/wow-forever-codex) does that for WoW: Forever.

This is a separate implementation for the 3.3.5a client, written against that published design
rather than copied from it. The upstream repository carries no licence file, so it grants no
redistribution rights; nothing here is derived from its source.

This code is MIT licensed (see [LICENSE](LICENSE)). Two things it deliberately does not ship:

- `mono.ttf` — the installer copies a fixed-width font from the machine's own Windows fonts. That
  copy stays local; redistributing Microsoft's fonts is not permitted.
- The font bank and `selftest.ttf` — generated at install time, and the bank alone is 65,535 files.

World of Warcraft is a trademark of Blizzard Entertainment, which has nothing to do with this
project. ChromieCraft is an independent server; check its rules before using anything here.
