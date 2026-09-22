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

local DEFAULTS = {nextSlot = 1, strip = 'TOP', sound = true, minimapAngle = 215}
local listeners = {}
function NS.OnLoad(fn) listeners[#listeners+1] = fn end

-- The session identifies one UI load (stale-packet protection); its first four
-- bytes are the conversation, which survives /reload until "New chat".
function NS.NewSession()
    NS.session = NS.U32(NS.S.conversation)..NS.U32(math.floor(GetTime()*1000) % 4294967296)
    return NS.session
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
    S.conversation = tonumber(S.conversation) or time()
    NS.S = S
    NS.NewSession()
    for _, fn in ipairs(listeners) do fn(S) end
end)
