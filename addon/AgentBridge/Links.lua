-- Hyperlinks in both directions. Outgoing links become readable text with IDs;
-- only explicit [label](item:ID[:fields]) references in replies become links.
-- Blizzard functions are only post-hooked, never replaced.
local NS = AgentBridge
local edit, panel, body = NS.Input, NS.Panel, NS.Body

local function linkData(link)
    if type(link) ~= 'string' then return end
    return link:match('|H([^|]+)|h(.-)|h')
end

-- Outgoing. What the agent reads spells each link out:
--   [Frostmourne] (item 36942; link item:36942:0:0:0:0:0:0:0:80; Weapon / Two-Handed Swords; item level 284)
-- then, while it fits in `budget` bytes, the tooltip text of up to six linked
-- things, so it can answer from the actual stats. The transcript shows only
-- what you typed, with each link as its [name].
local MAX_TIPS, TIP_BYTES = 6, 700
function NS.MakePromptText(raw, budget)
    local plain = raw:gsub('|c%x%x%x%x%x%x%x%x', ''):gsub('|r', '')
    local linked, seen = {}, {}
    local agent = plain:gsub('|H([^|]+)|h(.-)|h', function(data, label)
        local kind, id = data:match('^(%a+):(%-?%d+)')
        if not kind then return label end
        if not seen[data] and #linked < MAX_TIPS then seen[data] = true; linked[#linked+1] = data end
        if kind ~= 'item' then return label..' ('..kind..' '..id..')' end
        local details = {'item '..id, 'link '..data}
        local name, _, _, level, minimum, class, subclass = GetItemInfo(data)
        if class and class ~= '' then details[#details+1] = class..(subclass and subclass ~= '' and (' / '..subclass) or '') end
        if level and level > 0 then details[#details+1] = 'item level '..level end
        if minimum and minimum > 0 then details[#details+1] = 'requires level '..minimum end
        return (name and ('['..name..']') or label)..' ('..table.concat(details, '; ')..')'
    end)
    local display = plain:gsub('|H[^|]+|h(.-)|h', '%1')
    if budget then
        local header, tips, size = '\n\nLinked from the game (tooltip text):', {}, #agent
        for _, data in ipairs(linked) do
            local text = NS.LinkTooltip and NS.LinkTooltip(data)
            if text then
                if #text > TIP_BYTES then text = NS.TrimUTF8(text:sub(1, TIP_BYTES))..' ...' end
                local entry = '\n\n'..text
                local extra = #entry + (#tips == 0 and #header or 0)
                if size + extra > budget then break end
                tips[#tips+1], size = entry, size + extra
            end
        end
        if #tips > 0 then agent = agent..header..table.concat(tips) end
    end
    return agent, display
end

function NS.InsertLink(link)
    if not panel:IsShown() or not edit:HasFocus() or not linkData(link) then return false end
    if #edit:GetText() + #link + 1 > NS.MAX_TYPED then
        NS.SetStatus('Not enough room for this link. Shorten the message first.'); return false
    end
    edit:Insert(link..' ')
    return true
end

-- Shift-click on bag items reaches ChatEdit_InsertLink even with no chat box
-- open; it then opens the stack-split dialog. Close only that redundant dialog.
local justInserted = false
if type(ChatEdit_InsertLink) == 'function' then
    hooksecurefunc('ChatEdit_InsertLink', function(link)
        justInserted = NS.InsertLink(link) and IsModifiedClick('CHATLINK')
    end)
end
if type(OpenStackSplitFrame) == 'function' then
    hooksecurefunc('OpenStackSplitFrame', function()
        if justInserted and edit:HasFocus() and StackSplitFrame and StackSplitFrame:IsShown() then
            StackSplitFrame:Hide()
        end
        justInserted = false
    end)
end
local clear = CreateFrame('Frame')
clear:SetScript('OnUpdate', function() justInserted = false end)

edit:SetScript('OnReceiveDrag', function(self)
    local kind, id, link = GetCursorInfo()
    if kind ~= 'item' then return end
    if not linkData(link) then link = select(2, GetItemInfo(id)) end
    self:SetFocus()
    if NS.InsertLink(link) then ClearCursor() end
end)

-- Incoming ---------------------------------------------------------------
local MAX_LINKS, MAX_QUERIES = 128, 32
local allowed, queried, queryCount, retryUntil = {}, {}, 0, 0
local scan = CreateFrame('GameTooltip', 'AgentBridgeScanTooltip', nil, 'GameTooltipTemplate')

local function itemID(data)
    if type(data) ~= 'string' or #data > 200 or not data:match('^item:%d+[:%d%-]*$') then return end
    local id = tonumber(data:match('^item:(%d+)'))
    if not id or id < 1 or id > 2147483647 then return end
    for field in data:gmatch(':([^:]*)') do
        if #field > 11 then return end
    end
    return id
end
local function itemLink(data, id)
    local name, canonical = GetItemInfo(data)
    if type(name) ~= 'string' or type(canonical) ~= 'string' then return end
    if itemID(canonical:match('|H(item:[^|]+)|h')) ~= id then return end
    local color = canonical:match('^(|c%x%x%x%x%x%x%x%x)') or '|cffffffff'
    return color..'|H'..data..'|h['..NS.Escape(name:gsub('[%c%[%]]', ''))..']|h|r'
end
local function query(id)
    -- Ask the server for uncached items, as any tooltip addon does.
    if queried[id] or queryCount >= MAX_QUERIES then return end
    queried[id], queryCount = true, queryCount + 1
    scan:SetOwner(WorldFrame, 'ANCHOR_NONE')
    pcall(scan.SetHyperlink, scan, 'item:'..id..':0:0:0:0:0:0:0')
    retryUntil = GetTime() + 10
end

-- Links only become interactive where the client dispatches hyperlink events
-- for this frame type; elsewhere they still show as coloured names.
local function hook(script, handler) pcall(body.SetScript, body, script, handler) end

local STYLE = {heading = 'ffffd100', label = 'ff8dbdff', dim = 'ff909090', code = 'ffb7e4c7'}
local links = 0

-- One line: escape everything, then turn validated item references into links.
local function renderInline(text)
    local chunks, cursor, missing = {}, 1, false
    while links < MAX_LINKS do
        local first, last, label, data = text:find('%[([^%[%]\r\n]+)%]%((item:[^%s%(%)]+)%)', cursor)
        if not first then break end
        chunks[#chunks+1] = NS.Escape(text:sub(cursor, first-1))
        local id = itemID(data)
        local link = id and itemLink(data, id)
        if link then
            allowed[data] = true; chunks[#chunks+1] = link
        elseif id then
            chunks[#chunks+1] = NS.Escape(label)..' (item '..id..')'
            missing = true; query(id)
        else
            chunks[#chunks+1] = NS.Escape(text:sub(first, last))
        end
        cursor, links = last + 1, links + 1
    end
    chunks[#chunks+1] = NS.Escape(text:sub(cursor))
    return table.concat(chunks), missing
end

-- Returns the joined markup, whether an item is still loading, whether any
-- line needs the fixed-width font, and the rows for the panel. columns = 0
-- lays tables out for a proportional font (the chat frame).
function NS.RenderReply(text, columns)
    local entries, mono = NS.FormatLines(text, columns or NS.BodyColumns())
    local out, rows, missing = {}, {}, false
    -- Links accumulate across the whole transcript, so older replies keep
    -- their tooltips; the per-reply cap still applies.
    links = 0
    for _, entry in ipairs(entries) do
        local line, gap = renderInline(entry.text)
        missing = missing or gap
        local color = entry.style and STYLE[entry.style]
        line = color and ('|c'..color..line..'|r') or line
        out[#out+1] = line
        rows[#rows+1] = {text = line, mono = entry.mono}
    end
    return table.concat(out, '\n'), missing, mono, rows
end

local STATE_NOTE = {[0] = 'sending...', [1] = 'queued', [2] = 'working', [3] = 'writing...',
    [5] = 'finished with a problem', [6] = 'interrupted'}

-- Transcript ---------------------------------------------------------------
-- Finished exchanges live in saved settings, tagged with their chat, so every
-- chat survives a /reload. Kept small: saved variables are rewritten on every
-- logout. Requests still under way are kept here until their reply is final.
local KEEP_PER_CHAT, KEEP_BYTES = 30, 200000
local rendered = setmetatable({}, {__mode = 'k'})  -- exchange -> rows at a width
local inflight = {}  -- [request] = {request, chat, prompt, text, state, rows, sentAt}

local function history()
    if type(NS.S.history) ~= 'table' then NS.S.history = {} end
    return NS.S.history
end
local function size(exchange) return #(exchange.p or '') + #(exchange.r or '') end

local function record(chat, prompt, reply, state)
    local list = history()
    -- Showing a finished reply again must not store it twice.
    for i = #list, 1, -1 do
        if list[i].c == chat then
            if list[i].p == prompt and list[i].r == reply then return end
            break
        end
    end
    list[#list+1] = {c = chat, p = prompt, r = reply, s = state}
    local count, total = 0, 0
    for _, exchange in ipairs(list) do
        if exchange.c == chat then count = count + 1 end
        total = total + size(exchange)
    end
    local i = 1
    while count > KEEP_PER_CHAT and i <= #list do
        if list[i].c == chat then
            total, count = total - size(list[i]), count - 1
            table.remove(list, i)
        else
            i = i + 1
        end
    end
    while total > KEEP_BYTES and #list > 1 do
        total = total - size(list[1])
        table.remove(list, 1)
    end
end

local function blockFor(exchange, columns)
    local hit = rendered[exchange]
    if hit and hit.columns == columns then return hit.block end
    local _, missing, _, rows = NS.RenderReply(exchange.r or '')
    local note = exchange.s ~= 4 and ('('..(STATE_NOTE[exchange.s] or 'finished with a problem')..')') or nil
    local block = {prompt = exchange.p, rows = rows, note = note}
    rendered[exchange] = {columns = columns, block = block, missing = missing}
    return block
end

local function pending(chat)
    local out = {}
    for _, item in pairs(inflight) do
        if item.chat == chat then out[#out+1] = item end
    end
    table.sort(out, function(a, b) return a.request < b.request end)
    return out
end

local function clock(seconds)
    seconds = math.max(0, math.floor(seconds))
    return string.format('%d:%02d', math.floor(seconds / 60), seconds % 60)
end
-- One line under a prompt that is still under way: what the companion last
-- said about it (queued, working, the agent's latest action) and how long ago
-- it was sent, counted here so it keeps ticking between reply packets.
local function noteFor(item)
    local what
    if item.state >= 4 then
        what = 'receiving the rest...'
    elseif item.state == 3 then
        what = 'writing...'
    else
        what = item.text:match('^[^\n]*'):gsub('%s+', ' ')
        if what == '' then what = STATE_NOTE[item.state] end
        if #what > 110 then what = NS.TrimUTF8(what:sub(1, 107))..'...' end
        what = NS.Escape(what)
    end
    return '('..what..'  '..clock(GetTime() - item.sentAt)..')'
end

-- Redraw the chat you are reading: finished exchanges, then those under way.
function NS.RefreshTranscript(focus)
    if not NS.S then return end
    local chat, columns, blocks = NS.S.chat, NS.BodyColumns(), {}
    for _, exchange in ipairs(history()) do
        if exchange.c == chat then blocks[#blocks+1] = blockFor(exchange, columns) end
    end
    for _, item in ipairs(pending(chat)) do
        blocks[#blocks+1] = {prompt = item.prompt, rows = item.rows, note = noteFor(item), live = true}
    end
    if #blocks == 0 then blocks[1] = {rows = {}, note = '(new conversation)'} end
    NS.RenderTranscript(blocks, focus)
end
local function refreshChats() if NS.RefreshChats then NS.RefreshChats() end end

-- A prompt was just sent: show it at the bottom of its chat.
function NS.StartExchange(request, chat, prompt)
    inflight[request] = {request = request, chat = chat, prompt = prompt, text = '', state = 0, rows = {},
                         sentAt = GetTime()}
    queried, queryCount = {}, 0
    if chat == NS.S.chat then NS.RefreshTranscript('last') end
    refreshChats()
end

-- A finished reply you are not looking at is copied into the chat frame (up
-- to /ab echo characters), links and all, labelled with its chat.
local function echo(chat, text, state)
    local limit = tonumber(NS.S.echo) or 0
    if limit <= 0 or (panel:IsShown() and NS.S.chat == chat) then return end
    local cut = #text > limit and NS.TrimUTF8(text:sub(1, limit)) or text
    local _, _, _, rows = NS.RenderReply(cut, 0)
    local label = '|cff66bbffAgent Bridge|r |cffffd100['..NS.Escape(NS.ChatTitle(chat))..']|r'
    if state ~= 4 then label = label..' |cffff8080'..(STATE_NOTE[state] or 'finished with a problem')..'|r' end
    DEFAULT_CHAT_FRAME:AddMessage(label)
    for _, row in ipairs(rows) do
        if row.text:find('%S') then DEFAULT_CHAT_FRAME:AddMessage('  '..row.text) end
    end
    if #cut < #text then
        DEFAULT_CHAT_FRAME:AddMessage('|cff909090  ('..(#text - #cut)..' more characters: /ab to read the rest)|r')
    end
end

-- The companion names the agent and model answering ahead of the text:
-- \1agent=codex\nmodel=gpt-5.5\2. Only the text after it is shown or kept.
local function splitMeta(text)
    local head, rest = text:match('^\1([^\2]*)\2(.*)$')
    if not head then return nil, text end
    local meta = {}
    for key, value in head:gmatch('(%a+)=([^\n]*)') do meta[key] = value end
    return meta, rest
end

function NS.ShowReply(request, text, state, complete)
    local item = inflight[request]
    if not item then return end
    local meta
    meta, text = splitMeta(text)
    if meta then NS.NoteAgent(item.chat, meta.agent, meta.model) end
    if complete and state >= 4 then
        inflight[request] = nil
        record(item.chat, item.prompt, text, state)
        if item.chat == NS.S.chat then NS.RefreshTranscript() end
        echo(item.chat, text, state)
        refreshChats()
        return
    end
    item.text, item.state = text, state
    if state >= 3 then
        local _, missing, _, rows = NS.RenderReply(text)
        item.rows, item.missing = rows, missing
    else
        item.rows = {}
    end
    if item.chat == NS.S.chat then NS.RefreshTranscript() end
end

function NS.IsChatBusy(chat)
    for _, item in pairs(inflight) do
        if item.chat == chat then return true end
    end
    return false
end
function NS.HasHistory(chat)
    chat = chat or NS.S.chat
    for _, exchange in ipairs(history()) do
        if exchange.c == chat then return true end
    end
    return false
end
-- A deleted chat: stop waiting for its replies (the companion keeps them) and
-- drop its transcript.
function NS.ForgetChat(chat)
    for request, item in pairs(inflight) do
        if item.chat == chat then
            inflight[request] = nil
            NS.ForgetRequest(request); NS.AckPrompt(request)
        end
    end
    local list = history()
    for i = #list, 1, -1 do
        if list[i].c == chat then table.remove(list, i) end
    end
end

-- Plain text for the copy box: the last reply in this chat, or all of it.
function NS.CopyText(all)
    local out, last = {}, nil
    for _, exchange in ipairs(history()) do
        if exchange.c == NS.S.chat then
            last = exchange.r or ''
            out[#out+1] = 'You: '..(exchange.p or '')..'\n\n'..last
        end
    end
    if all then return table.concat(out, '\n\n----\n\n') end
    return last or ''
end

-- Once a second: re-render replies whose items were still loading (for up to
-- 10 s after the server was asked), and keep the elapsed time under a prompt
-- that is still under way ticking.
local poll, elapsed = CreateFrame('Frame'), 0
poll:SetScript('OnUpdate', function(_, dt)
    elapsed = elapsed + dt
    if elapsed < 1 or not NS.S then return end
    elapsed = 0
    if GetTime() <= retryUntil then
        local stale = false
        for exchange, hit in pairs(rendered) do
            if hit.missing then rendered[exchange] = nil; stale = true end
        end
        for _, item in pairs(inflight) do
            if item.missing and item.state >= 3 then
                local _, missing, _, rows = NS.RenderReply(item.text)
                item.rows, item.missing, stale = rows, missing, true
            end
        end
        if stale then NS.RefreshTranscript() end
    end
    if panel:IsShown() then
        local list = pending(NS.S.chat)
        if #list > 0 then NS.SetLiveNote(noteFor(list[#list])) end
    end
end)

hook('OnHyperlinkEnter', function(self, data)
    if not allowed[data] then return end
    GameTooltip:SetOwner(self, 'ANCHOR_CURSOR')
    if pcall(GameTooltip.SetHyperlink, GameTooltip, data) then GameTooltip:Show() else GameTooltip:Hide() end
end)
hook('OnHyperlinkLeave', function() GameTooltip:Hide() end)
hook('OnHyperlinkClick', function(self, data, link, button)
    if not allowed[data] then return end
    if IsModifiedClick('CHATLINK') and edit:HasFocus() then NS.InsertLink(link); return end
    SetItemRef(data, link, button)
end)
