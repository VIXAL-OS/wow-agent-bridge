-- Hyperlinks in both directions. Outgoing links become readable text with IDs;
-- only explicit [label](item:ID[:fields]) references in replies become links.
-- Blizzard functions are only post-hooked, never replaced.
local NS = AgentBridge
local edit, panel, body = NS.Input, NS.Panel, NS.Body

local function linkData(link)
    if type(link) ~= 'string' then return end
    return link:match('|H([^|]+)|h(.-)|h')
end

-- Outgoing: "[Frostmourne] (item 36942; link item:36942:0:0:0:0:0:0:0:80; Weapon / Two-Handed Swords; item level 284)"
function NS.MakePromptText(raw)
    local plain = raw:gsub('|c%x%x%x%x%x%x%x%x', ''):gsub('|r', '')
    return (plain:gsub('|H([^|]+)|h(.-)|h', function(data, label)
        local kind, id = data:match('^(%a+):(%-?%d+)')
        if not kind then return label end
        if kind ~= 'item' then return label..' ('..kind..' '..id..')' end
        local details = {'item '..id, 'link '..data}
        local name, _, _, level, minimum, class, subclass = GetItemInfo(data)
        if class and class ~= '' then details[#details+1] = class..(subclass and subclass ~= '' and (' / '..subclass) or '') end
        if level and level > 0 then details[#details+1] = 'item level '..level end
        if minimum and minimum > 0 then details[#details+1] = 'requires level '..minimum end
        return (name and ('['..name..']') or label)..' ('..table.concat(details, '; ')..')'
    end))
end

function NS.InsertLink(link)
    if not panel:IsShown() or not edit:HasFocus() or not linkData(link) then return false end
    if #edit:GetText() + #link + 1 > NS.MAX_PROMPT then
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
local current, allowed, queried, queryCount, retryUntil = nil, {}, {}, 0, 0
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

-- Returns panel markup, whether an item is still loading, and whether the
-- reply needs the fixed-width font (tables and code blocks do).
function NS.RenderReply(text)
    local entries, mono = NS.FormatLines(text, NS.BodyColumns())
    local out, missing = {}, false
    allowed, links = {}, 0
    for _, entry in ipairs(entries) do
        local line, gap = renderInline(entry.text)
        missing = missing or gap
        local color = entry.style and STYLE[entry.style]
        out[#out+1] = color and ('|c'..color..line..'|r') or line
    end
    return table.concat(out, '\n'), missing, mono
end

local STATE_NOTE = {[0] = 'waiting for the companion', [1] = 'queued', [2] = 'working', [3] = 'writing...',
    [5] = 'finished with a problem', [6] = 'interrupted'}
function NS.ShowReply(text, state, complete)
    local markup, missing, mono = NS.RenderReply(text)
    local note = STATE_NOTE[state]
    if not complete and state >= 4 then note = 'receiving the rest...' end
    current = {text = text, state = state, complete = complete, markup = markup}
    NS.RenderBody(NS.currentPrompt, markup, note and ('('..note..')'), mono)
    if complete and state >= 4 then NS.S.last = {prompt = NS.currentPrompt, reply = text, state = state} end
    return missing
end
function NS.ClearReply() current, allowed, queried, queryCount, retryUntil = nil, {}, {}, 0, 0 end

-- Re-render once uncached items arrive from the server (bounded to 10 s).
local poll, elapsed = CreateFrame('Frame'), 0
poll:SetScript('OnUpdate', function(_, dt)
    elapsed = elapsed + dt
    if elapsed < 1 or not current or GetTime() > retryUntil then return end
    elapsed = 0
    local markup, missing = NS.RenderReply(current.text)
    if markup ~= current.markup then NS.ShowReply(current.text, current.state, current.complete) end
    if not missing then retryUntil = 0 end
end)

body:SetScript('OnHyperlinkEnter', function(self, data)
    if not allowed[data] then return end
    GameTooltip:SetOwner(self, 'ANCHOR_CURSOR')
    if pcall(GameTooltip.SetHyperlink, GameTooltip, data) then GameTooltip:Show() else GameTooltip:Hide() end
end)
body:SetScript('OnHyperlinkLeave', function() GameTooltip:Hide() end)
body:SetScript('OnHyperlinkClick', function(self, data, link, button)
    if not allowed[data] then return end
    if IsModifiedClick('CHATLINK') and edit:HasFocus() then NS.InsertLink(link); return end
    SetItemRef(data, link, button)
end)
