-- Outbound channel: a 128 x 8 cell strip the companion reads from the screen.
-- Each bit is a pair of neighbouring cells, one light and one dark, so the
-- companion reads the difference and the strip stays decodable at any opacity.
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
local alpha = 1
function NS.SetStripAlpha(value)
    alpha = math.max(.2, math.min(1, tonumber(value) or 1))
    NS.S.alpha = alpha
    return alpha
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
local pending, frames, cursor = {}, {}, 1
local lastSubmit, lastData, lastAlpha, elapsed, tick = -1000, nil, nil, 0, 0
local function rebuild()
    frames, cursor = {}, 1
    for _, item in ipairs(pending) do
        for _, frame in ipairs(item.frames) do frames[#frames+1] = frame end
    end
end
function NS.QueuePrompt(request, promptFrames)
    pending[#pending+1] = {request = request, frames = promptFrames}
    while #pending > 17 do table.remove(pending, 1) end
    rebuild(); lastSubmit = GetTime()
end
function NS.AckPrompt(request)
    if NS.CharacterAck then NS.CharacterAck(request) end
    for i = #pending, 1, -1 do
        if pending[i].request == request then table.remove(pending, i); rebuild() end
    end
end
function NS.ClearPrompts() pending = {}; rebuild() end

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
    if elapsed < .15 then return end
    elapsed = 0
    if not NS.session or not wanted() then
        if strip:IsShown() then strip:Hide(); lastData = nil end
        return
    end
    if not strip:IsShown() then strip:Show() end
    -- While a prompt is being sent, three of every four frames carry it: a long
    -- prompt with tooltips and context gets through sooner, and the control
    -- frame still comes round often enough to keep replies on schedule.
    tick = tick + 1
    local data
    if #frames == 0 or tick % 4 == 0 then
        data = NS.ControlFrame()
    else
        data = frames[cursor]; cursor = cursor % #frames + 1
    end
    if data == lastData and alpha == lastAlpha then return end
    NS.Profile('strip-paint', paint, data)
end)
