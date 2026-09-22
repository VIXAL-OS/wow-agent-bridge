# Agent guide: install and run Agent Bridge

For agents (Claude Code, Codex) asked to install, run, update or troubleshoot
this repository. Editing docs alone does not authorize reinstalling anything.

## Layout

- `addon/AgentBridge/`: the WoW 3.3.5a addon (Lua, TOC, Bindings.xml).
- `companion/`: the Python desktop companion (tkinter GUI, strip capture,
  font publisher, Claude Code and Codex runners).
- `tools/install_addon.py`: installer. Copies code and creates missing
  `selftest.ttf`, `Epoch.lua` and font-bank slots. It never overwrites existing
  slots.
- `Launch Agent Bridge.pyw`: desktop launcher using `state/settings.json`.

## Boundaries

1. Use documented addon APIs, ordinary file writes and screen capture only.
   Never inject into WoW, read its memory, synthesize input or bypass client
   checks. The user performs all in-game actions, including `/reload`.
2. If the font format changes, `tools.install_addon` rebuilds the bank, but
   only with the game closed; `tools.reset_bank` forces it. Never otherwise
   reset, delete or rewrite font-bank slots or `selftest.ttf` while WoW runs, and never write through a hard-linked slot (publish via `atomic_write`).
   `Epoch.lua` is managed by the companion; don't edit it by hand while WoW runs.
3. Run one companion per addon/inbox. Don't start a second one against the same
   `state/` directory. Don't terminate or restart WoW.
4. Keep the agent access level `read-only` unless the user asked for edits.
   Prompts are passed on stdin; never interpolate prompt text into a command line.
5. Keep machine paths, `state/` and generated fonts out of commits.

## Install

1. Find the game folder: the one containing `Wow.exe` and `Interface/AddOns`.
   Confirm it with the user if more than one is plausible.
2. Create or reuse `.venv` (Python 3.12+ with tkinter), then
   `pip install -r requirements.txt`.
3. Run `python -m tools.install_addon "<game>\Interface\AddOns\AgentBridge"`.
   This takes about 3 minutes for 65,535 slots and can be resumed. It saves the
   addon path in `state/settings.json`.
4. Check for a CLI: `python -c "from companion.launching import find_claude, find_codex; print(find_claude()); print(find_codex())"`.
   For Claude Code, auth problems show up as `is_error` results mentioning
   authentication; the user fixes them by running `claude` and `/login`. Never
   ask the user to paste tokens into chat.
5. First install: the user fully restarts WoW so it lists the addon. After an
   update, `/reload` is enough.

## Verify with the user

1. In game, `/ab test` must print `Font channel OK at size N`. If every size
   fails, the font channel cannot work on this client: report the printed table.
2. Start the companion (`pythonw "Launch Agent Bridge.pyw"`). Try the Mock
   backend first. It should report `Strip found at TOP of WoW window` once a
   prompt is sent.
3. Send a short prompt, then a follow-up. WoW may be covered or in the
   background while it transfers; only minimizing stops it. Both replies must appear in the
   panel, and the strip must disappear after each completes. A desktop banner
   alone does not prove the in-game channel works.
4. Report: install path, backend, access level, self-test size, and whether
   the round trip was verified in game or only on disk.

## Development

`python -m pip install -r requirements-dev.txt`, then
`python -m unittest discover -s tests -t .`. The end-to-end tests run the
production Lua under Lua 5.1 against a pessimistic client model.
