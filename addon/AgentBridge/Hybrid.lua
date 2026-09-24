-- Optional one-use addon slots. Only a checked font descriptor authorizes a
-- load. The companion emits a fixed assignment containing hex digits only.
-- Decoded bytes are validated as data; never passed to loadstring or a call.
local NS = AgentBridge
NS.HYBRID_SLOTS = 16
local nextSlot, disabled = 1, false

local function name(slot) return string.format('AgentBridgeReply%02d', slot) end
function NS.HybridSlot()
    if disabled or type(GetAddOnInfo) ~= 'function' or type(LoadAddOn) ~= 'function' then return 0 end
    if InCombatLockdown and InCombatLockdown() then return 0 end
    while nextSlot <= NS.HYBRID_SLOTS do
        local addon, _, _, enabled, loadable = GetAddOnInfo(name(nextSlot))
        if addon and enabled and loadable and not IsAddOnLoaded(name(nextSlot)) then return nextSlot end
        nextSlot = nextSlot + 1
    end
    return 0
end
function NS.HybridInfo()
    return disabled and 'disabled after error (fonts active)' or
        (NS.HybridSlot() > 0 and ('slot '..nextSlot..'/'..NS.HYBRID_SLOTS) or 'unavailable (fonts active)')
end
local function uint(s, a, n)
    local v = 0
    for i = a, a+n-1 do v = v*256 + s:byte(i) end
    return v
end
local function reject(why)
    AgentBridgeHybridData = nil
    disabled = true
    return nil, why
end
function NS.LoadHybrid(descriptor, session, request)
    if #descriptor ~= 15 or descriptor:sub(1, 4) ~= 'ABH1' then return reject('Invalid descriptor') end
    local slot, state, length = uint(descriptor, 5, 2), descriptor:byte(7), uint(descriptor, 8, 4)
    if slot ~= NS.HybridSlot() or slot == 0 or state < 4 or state > 6 or length > 60000 then
        return reject('Unavailable or invalid reply slot')
    end
    -- Reserve before attempting the load. A failed/partial load is never reused.
    nextSlot = slot + 1
    AgentBridgeHybridData = nil
    local ok, loaded = pcall(LoadAddOn, name(slot))
    local hex = AgentBridgeHybridData
    AgentBridgeHybridData = nil
    if not ok or not loaded then return reject('Reply addon did not load') end
    if type(hex) ~= 'string' or #hex ~= 2*(27 + length) or hex:find('[^0-9a-f]') then
        return reject('Invalid reply encoding')
    end
    local data = hex:gsub('%x%x', function(pair) return string.char(tonumber(pair, 16)) end)
    if data:sub(1, 4) ~= 'ABH1' or data:sub(5, 12) ~= session or uint(data, 13, 4) ~= request
        or uint(data, 17, 2) ~= slot or data:byte(19) ~= state or uint(data, 20, 4) ~= length
        or data:sub(24, 27) ~= descriptor:sub(12, 15) then
        return reject('Stale or mismatched reply')
    end
    local text = data:sub(28)
    if NS.Adler(string.char(state)..text) ~= data:sub(24, 27) then return reject('Invalid reply checksum') end
    return text, state
end
