-- Fixed binary frames. Mirrors companion/protocol.py; nothing here is executable.
local NS = AgentBridge
local U16, U32, Adler = NS.U16, NS.U32, NS.Adler
local ZERO = string.char(0)
local PACKET, CHUNK = NS.REPLY_SIZE, NS.REPLY_SIZE - 36
local EMPTY = string.rep(ZERO, PACKET)
NS.MAX_PROMPT = 1280

-- CPB1: prompt fragments of 40 bytes, at most 32 per prompt.
function NS.EncodePrompt(text, session, request)
    assert(#text > 0 and #text <= NS.MAX_PROMPT and #session == 8)
    local frames, total = {}, math.ceil(#text/40)
    for part = 0, total-1 do
        local chunk = text:sub(part*40+1, part*40+40)
        local body = 'CPB1'..string.char(1, #chunk, part, total)..session..U32(request)
            ..chunk..string.rep(ZERO, 40-#chunk)
        frames[#frames+1] = body..Adler(body)
    end
    return frames
end

-- CPBN v3: which slot the addon loads next, how long until then, which reply
-- fragment it needs, whether it is receiving, and for which request.
function NS.EncodeControl(session, slot, remaining, part, active, request)
    local data = U32(slot)..U32(remaining)..U16(part)..U16(active and 1 or 0)..U32(request)..U32(slot-1)
    local body = 'CPBN'..string.char(3, #data, 0, 1)..session..U32(0)..data..string.rep(ZERO, 40-#data)
    return body..Adler(body)
end

local function uint(s, a, n)
    local result = 0
    for i = a, a+n-1 do result = result*256 + s:byte(i) end
    return result
end

-- CFN2: 32-byte header, 988-byte padded payload, Adler-32.
function NS.ParseReply(data, session, request, slot)
    if #data ~= PACKET then return nil, 'Invalid reply packet' end
    if data == EMPTY then return nil, 'Empty reply slot' end
    if data:sub(1, 4) ~= 'CFN2' or data:byte(5) ~= 2 then return nil, 'Invalid reply packet' end
    if Adler(data:sub(1, PACKET-4)) ~= data:sub(PACKET-3) then return nil, 'Invalid reply checksum' end
    local state, length = data:byte(6), uint(data, 7, 2)
    local revision, part, total = uint(data, 25, 4), uint(data, 29, 2), uint(data, 31, 2)
    if state > 6 or length > CHUNK or part < 1 or total < 1 or total > 127 or part > total then
        return nil, 'Invalid reply fields'
    end
    if data:sub(9, 16) ~= session or uint(data, 17, 4) ~= request or uint(data, 21, 4) ~= slot then
        return nil, 'Stale reply packet'
    end
    if part < total and length ~= CHUNK then return nil, 'Short reply fragment' end
    if data:sub(33+length, PACKET-4):find('[^%z]') then return nil, 'Invalid reply padding' end
    return {state = state, revision = revision, part = part, total = total, text = data:sub(33, 32+length)}
end

-- Read just the 32-byte header of a slot: enough to tell an empty or stale
-- slot, or an unchanged status, from something worth measuring in full. The
-- checksum still gates anything that gets displayed.
function NS.PeekReply(head, session, request, slot)
    if #head < 32 then return nil, 'Invalid reply packet' end
    if head:sub(1, 32) == string.rep(ZERO, 32) then return nil, 'Empty reply slot' end
    if head:sub(1, 4) ~= 'CFN2' or head:byte(5) ~= 2 then return nil, 'Invalid reply packet' end
    if head:sub(9, 16) ~= session or uint(head, 17, 4) ~= request or uint(head, 21, 4) ~= slot then
        return nil, 'Stale reply packet'
    end
    return {state = head:byte(6), revision = uint(head, 25, 4), part = uint(head, 29, 2), total = uint(head, 31, 2)}
end

function NS.NewAssembly()
    return {revision = nil, parts = {}, total = 0, nextPart = 1, state = 0}
end

-- Returns text, state, complete for an accepted fragment (text is the prefix
-- assembled so far); nil, reason on conflict; nil, nil for an ignored fragment.
function NS.AcceptFragment(a, p)
    if a.revision ~= p.revision then
        a.revision, a.parts, a.total, a.state, a.nextPart = p.revision, {}, p.total, p.state, 1
    end
    if a.total ~= p.total or a.state ~= p.state then return nil, 'Conflicting reply packet' end
    if p.part ~= a.nextPart then return nil, nil end
    a.parts[p.part] = p.text
    a.nextPart = p.part + 1
    local text = table.concat(a.parts)
    if p.part == p.total then
        a.nextPart, a.parts = 1, {}
        return text, p.state, true
    end
    return text, p.state, false
end
