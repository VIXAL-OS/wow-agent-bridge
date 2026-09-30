-- Outbound channel: a 128 x 8 cell strip the companion reads from the screen.
-- Each bit is a pair of neighbouring cells, one light and one dark, so the
-- companion reads the difference. Sharp background edges can overwhelm faint
-- pairs, so uploads and stalled exchanges temporarily use stronger contrast.
-- The frame has no parent, so its effective scale is exactly 1 (768 units =
-- window height) whatever the UI scale, and Alt+Z does not hide it.
local NS = AgentBridge
local COLS, ROWS, CELL = 128, 8, 4
local strip = CreateFrame('Frame', 'AgentBridgeStrip')
NS.Size(strip, COLS*CELL, ROWS*CELL); strip:SetScale(1)
strip:SetFrameStrata('TOOLTIP'); strip:EnableMouse(false); strip:Hide()
local cells = {}
for i = 0, COLS*ROWS-1 do
    local t = strip:CreateTexture(nil, 'OVERLAY')
    NS.Size(t, CELL, CELL)
    t:SetPoint('TOPLEFT', strip, 'TOPLEFT', (i % COLS)*CELL, -math.floor(i/COLS)*CELL)
    t:SetTexture(0, 0, 0, 1)
    cells[i+1] = t
end
local preferred, alpha = 1, 1
function NS.SetStripAlpha(value)
    preferred = math.max(.2, math.min(1, tonumber(value) or 1))
    NS.S.alpha = preferred
    return preferred
end

NS.ANCHORS = {TOP = true, TOPLEFT = true, TOPRIGHT = true, BOTTOM = true, BOTTOMLEFT = true, BOTTOMRIGHT = true}
function NS.PlaceStrip(anchor)
    strip:ClearAllPoints(); strip:SetPoint(anchor, UIParent, anchor, 0, 0)
end
NS.OnLoad(function(S)
    if not NS.ANCHORS[S.strip] then S.strip = 'TOP' end
    NS.PlaceStrip(S.strip)
    NS.SetStripAlpha(S.alpha or 1)
end)

-- Prompts repeat until the companion acknowledges them (any reply state past
-- "waiting"), so a missed capture is recovered without resending by hand.
local pending, frames, cursor, rotation = {}, {}, 1, 0
local recovery = {}
function NS.StripStalled(request) recovery[request] = true end
function NS.StripProgress(request) recovery[request] = nil end
function NS.StripTransferProgress(request)
    for _, item in ipairs(pending) do
        if item.request == request then item.started = GetTime() end
    end
end
local function effectiveAlpha()
    if next(recovery) then return 1 end
    local value, now = preferred, GetTime()
    for _, item in ipairs(pending) do
        if not item.held then
            if item.character then value = math.max(value, .6) end
            -- Allow two complete sweeps of the shared strip before treating an
            -- unacknowledged prompt/page as stalled. Long pages need more time.
            if now - item.started >= math.max(12, #frames * .4 + 8) then return 1 end
        end
    end
    return value
end
function NS.StripAlphaInfo() return preferred, alpha end
local lastSubmit, lastData, lastAlpha, elapsed, tick = -1000, nil, nil, 0, 0
local function rebuild()
    frames, cursor, rotation = {}, 1, 0
    for _, item in ipairs(pending) do
        if not item.held then
            for _, frame in ipairs(item.frames) do frames[#frames+1] = frame end
        end
    end
end
function NS.HoldPrompt(request, hold)
    for _, item in ipairs(pending) do
        if item.request == request and item.held ~= hold then
            item.held, item.started = hold, GetTime(); rebuild(); return
        end
    end
end
function NS.QueuePrompt(request, promptFrames, character)
    pending[#pending+1] = {request = request, frames = promptFrames, character = character, started = GetTime()}
    while #pending > 17 do table.remove(pending, 1) end
    rebuild(); lastSubmit = GetTime()
end
function NS.AckPrompt(request)
    NS.StripProgress(request)
    if NS.CharacterAck then NS.CharacterAck(request) end
    for i = #pending, 1, -1 do
        if pending[i].request == request then table.remove(pending, i); rebuild() end
    end
end
function NS.ClearPrompts() pending, recovery = {}, {}; rebuild() end

local function wanted()
    return NS.IsReceiving() or (#frames > 0 and GetTime() - lastSubmit < 120)
end

local function paint(data)
    for byte = 1, #data do
        local value, old = data:byte(byte), lastData and lastData:byte(byte)
        if value ~= old or alpha ~= lastAlpha then
            for bit = 0, 7 do
                local v = math.floor(value / 2^(7-bit)) % 2
                if not old or alpha ~= lastAlpha or v ~= math.floor(old / 2^(7-bit)) % 2 then
                    local i = (byte-1)*8 + bit
                    local index = math.floor(i/64)*COLS + 2*(i % 64) + 1
                    cells[index]:SetTexture(v, v, v, alpha)
                    cells[index+1]:SetTexture(1-v, 1-v, 1-v, alpha)
                end
            end
        end
    end
    lastData, lastAlpha = data, alpha
end

local driver = CreateFrame('Frame')
driver:SetScript('OnUpdate', function(_, dt)
    elapsed = elapsed + dt
    -- After two unacknowledged sweeps, hold each page fragment long enough for
    -- covered-window capture (~0.3s) to observe it. Healthy transfers stay fast.
    local interval, now = .15, GetTime()
    for _, item in ipairs(pending) do
        if item.character and not item.held and now - item.started >= math.max(12, #frames * .4 + 8) then
            interval = .35; break
        end
    end
    if elapsed < interval then return end
    elapsed = 0
    if not NS.session or not wanted() then
        alpha = preferred
        if strip:IsShown() then strip:Hide(); lastData = nil end
        return
    end
    alpha = effectiveAlpha()
    if not strip:IsShown() then strip:Show() end
    -- While a prompt is being sent, three of every four frames carry it: a long
    -- prompt with tooltips and context gets through sooner, and the control
    -- frame still comes round often enough to keep replies on schedule.
    tick = tick + 1
    local data
    if #frames == 0 or tick % 4 == 0 then
        data = NS.ControlFrame()
    else
        -- Shift each retransmission by one fragment. A slower capture loop can
        -- otherwise sample the same subset forever when its cadence lines up
        -- with this strip, especially while the game is in the background.
        data = frames[(cursor + rotation - 1) % #frames + 1]
        cursor = cursor % #frames + 1
        if cursor == 1 then rotation = (rotation + 1) % #frames end
    end
    if data == lastData and alpha == lastAlpha then return end
    NS.Profile('strip-paint', paint, data)
end)
