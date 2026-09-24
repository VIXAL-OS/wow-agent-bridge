-- Agent Bridge for WoW 3.3.5a: shared namespace, saved settings and helpers.
-- Uses only documented addon APIs. No game input, no protected calls.
AgentBridge = AgentBridge or {}
local NS = AgentBridge
NS.VERSION = '0.1.0'
NS.BANK_SIZE = 65535
NS.REPLY_SIZE = 4096
NS.PATH = 'Interface\\AddOns\\AgentBridge\\'

function NS.Size(region, w, h) region:SetWidth(w); region:SetHeight(h) end
function NS.Escape(text) return (tostring(text):gsub('|', '||')) end
function NS.Print(text) DEFAULT_CHAT_FRAME:AddMessage('|cff66bbffAgent Bridge:|r '..text) end
function NS.SlotPath(slot) return NS.PATH..string.format('reply%05d.ttf', slot) end

-- Screen pixels per UI unit at effective scale 1, for diagnostics only.
-- Windowed clients may render at a different size than gxResolution.
function NS.PixelsPerUnit()
    local height = tonumber((GetCVar('gxResolution') or ''):match('%d+x(%d+)'))
    return (height or 768) / 768
end

function NS.U16(n) return string.char(math.floor(n/256)%256, n%256) end
function NS.U32(n)
    return string.char(math.floor(n/16777216)%256, math.floor(n/65536)%256, math.floor(n/256)%256, n%256)
end
function NS.Adler(s)
    local a, b = 1, 0
    for i = 1, #s do a = (a + s:byte(i)) % 65521; b = (b + a) % 65521 end
    return NS.U32(b*65536 + a)
end

-- Drop a trailing partial UTF-8 sequence so previews never show broken characters.
function NS.TrimUTF8(s)
    local n = #s
    for back = 0, math.min(3, n-1) do
        local c = s:byte(n - back)
        if c < 128 then return s end
        if c >= 192 then
            local need = c >= 240 and 4 or c >= 224 and 3 or 2
            if back + 1 >= need then return s end
            return s:sub(1, n - back - 1)
        end
    end
    return s
end

-- A chat's title when you have not named it: the start of its first message.
function NS.ShortTitle(text)
    local line = tostring(text or ''):gsub('%s+', ' '):gsub('^ ', '')
    if #line <= 28 then return line end
    return NS.TrimUTF8(line:sub(1, 26))..'...'
end

-- echo: characters of each reply copied into the chat frame (0 = off).
local DEFAULTS = {nextSlot = 1, strip = 'TOP', sound = true, minimapAngle = 215, context = true, echo = 800}
local listeners = {}
function NS.OnLoad(fn) listeners[#listeners+1] = fn end

-- The session identifies one UI load (stale-packet protection). Which chat a
-- prompt belongs to travels in the prompt itself, so every chat shares it.
function NS.NewSession()
    NS.session = NS.U32(time() % 4294967296)..NS.U32(math.floor(GetTime()*1000) % 4294967296)
    return NS.session
end

-- Chats, newest first: {id, name, auto (title from the first message), unread}.
-- Earlier versions had one conversation number with the transcript tagged by
-- it (and before that only the last reply); each conversation becomes a chat
-- with the same number, so the companion carries on with the same session.
local function migrate(S)
    if type(S.history) ~= 'table' then
        local last = S.last
        S.history = {}
        if type(last) == 'table' and type(last.reply) == 'string' then
            S.history[1] = {c = tonumber(S.conversation), p = last.prompt, r = last.reply, s = last.state or 4}
        end
    end
    S.last = nil
    if type(S.chats) ~= 'table' then
        local ids, seen = {}, {}
        local function add(id)
            id = tonumber(id)
            if id and not seen[id] then seen[id] = true; ids[#ids+1] = id end
        end
        add(S.conversation)
        for _, exchange in ipairs(S.history) do add(type(exchange) == 'table' and exchange.c) end
        table.sort(ids, function(a, b) return a > b end)
        S.chats = {}
        for _, id in ipairs(ids) do S.chats[#S.chats+1] = {id = id} end
        S.chat = tonumber(S.conversation)
    end
    S.conversation = nil
    local chats, byID = {}, {}
    for _, chat in ipairs(S.chats) do
        local id = type(chat) == 'table' and tonumber(chat.id)
        if id and not byID[id] then chat.id = id; chats[#chats+1] = chat; byID[id] = chat end
    end
    if #chats == 0 then chats[1] = {id = time()}; byID[chats[1].id] = chats[1] end
    S.chats = chats
    if not byID[tonumber(S.chat)] then S.chat = chats[1].id end
    -- Drop exchanges whose chat is gone; title unnamed chats from their first message.
    local kept = {}
    for _, exchange in ipairs(S.history) do
        local chat = type(exchange) == 'table' and byID[tonumber(exchange.c)]
        if chat then
            kept[#kept+1] = exchange
            if not chat.name and not chat.auto and type(exchange.p) == 'string' then chat.auto = NS.ShortTitle(exchange.p) end
        end
    end
    S.history = kept
end

local loader = CreateFrame('Frame')
loader:RegisterEvent('ADDON_LOADED')
loader:SetScript('OnEvent', function(self, _, name)
    if name ~= 'AgentBridge' then return end
    self:UnregisterEvent('ADDON_LOADED')
    AgentBridgeState = type(AgentBridgeState) == 'table' and AgentBridgeState or {}
    local S = AgentBridgeState
    for k, v in pairs(DEFAULTS) do if S[k] == nil then S[k] = v end end
    local slot = tonumber(S.nextSlot)
    if not slot or slot < 1 or slot ~= math.floor(slot) then slot = NS.BANK_SIZE + 1 end
    S.nextSlot = math.min(slot, NS.BANK_SIZE + 1)
    -- Epoch.lua is rewritten by the companion only while no WoW process is
    -- running. A new value therefore means no bank font has been loaded by
    -- this client process yet, so every slot is safe to reuse.
    local epoch = AgentBridgeEpoch
    if type(epoch) == 'string' and epoch:match('^%d+$') and S.epoch ~= epoch then
        S.epoch = epoch
        if S.nextSlot > 1 then NS.recycledFrom = S.nextSlot; S.nextSlot = 1 end
    end
    migrate(S)
    NS.S = S
    NS.NewSession()
    for _, fn in ipairs(listeners) do fn(S) end
end)
