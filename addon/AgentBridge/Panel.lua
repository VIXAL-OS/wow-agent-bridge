-- Chat panel, minimap button and completion badge (3.3.5a frame APIs only).
local NS = AgentBridge
local Size = NS.Size

local panel = CreateFrame('Frame', 'AgentBridgePanel', UIParent)
NS.Panel = panel
Size(panel, 560, 520); panel:SetPoint('CENTER'); panel:Hide()
panel:SetFrameStrata('DIALOG'); panel:SetToplevel(true); panel:SetClampedToScreen(true)
panel:SetBackdrop({bgFile = 'Interface\\DialogFrame\\UI-DialogBox-Background',
    edgeFile = 'Interface\\DialogFrame\\UI-DialogBox-Border', tile = true, tileSize = 32, edgeSize = 32,
    insets = {left = 11, right = 12, top = 12, bottom = 11}})
panel:SetMovable(true); panel:SetResizable(true); panel:SetMinResize(420, 300)
panel:EnableMouse(true); panel:RegisterForDrag('LeftButton')
tinsert(UISpecialFrames, 'AgentBridgePanel')

local function savePanel()
    local point, _, relative, x, y = panel:GetPoint()
    NS.S.panel = {point = point, relative = relative, x = x, y = y, w = panel:GetWidth(), h = panel:GetHeight()}
end
panel:SetScript('OnDragStart', function(self) self:StartMoving() end)
panel:SetScript('OnDragStop', function(self) self:StopMovingOrSizing(); savePanel() end)

local header = panel:CreateTexture(nil, 'ARTWORK')
header:SetTexture('Interface\\DialogFrame\\UI-DialogBox-Header'); Size(header, 320, 64)
header:SetPoint('TOP', 0, 12)
local title = panel:CreateFontString(nil, 'OVERLAY', 'GameFontNormal')
title:SetPoint('TOP', header, 'TOP', 0, -14); title:SetText('Agent Bridge')
function NS.SetTitle(text) title:SetText(text) end

local close = CreateFrame('Button', nil, panel, 'UIPanelCloseButton')
close:SetPoint('TOPRIGHT', -6, -6)

local status = panel:CreateFontString(nil, 'OVERLAY', 'GameFontNormalSmall')
status:SetPoint('TOPLEFT', 22, -32); status:SetPoint('TOPRIGHT', -36, -32); status:SetJustifyH('LEFT')
status:SetText('Send a prompt. The reply appears here while the companion runs.')
function NS.SetStatus(text) status:SetText(text) end

local inset = CreateFrame('Frame', nil, panel)
inset:SetPoint('TOPLEFT', 18, -48); inset:SetPoint('BOTTOMRIGHT', -18, 80)
inset:SetBackdrop({bgFile = 'Interface\\Tooltips\\UI-Tooltip-Background',
    edgeFile = 'Interface\\Tooltips\\UI-Tooltip-Border', tile = true, tileSize = 16, edgeSize = 16,
    insets = {left = 4, right = 4, top = 4, bottom = 4}})
inset:SetBackdropColor(0, 0, 0, .65)

-- A document view: a scroll frame whose child holds one font string per line.
-- (A chat-style message frame stacks text against its bottom edge and scrolls
-- by whole messages, which reads badly for a reply.)
local scroll = CreateFrame('ScrollFrame', 'AgentBridgeScroll', inset, 'UIPanelScrollFrameTemplate')
scroll:SetPoint('TOPLEFT', 10, -8); scroll:SetPoint('BOTTOMRIGHT', -30, 8)
local body = CreateFrame('Frame', 'AgentBridgeBody', scroll)
NS.Body = body
Size(body, 10, 10)
scroll:SetScrollChild(body)
if body.SetHyperlinksEnabled then body:SetHyperlinksEnabled(true) end
local bar = _G.AgentBridgeScrollScrollBar

local function bodySize()
    local ok, _, size = pcall(function() return ChatFontNormal:GetFont() end)
    return ok and tonumber(size) or 12
end
-- Move through the slider when the template provides one, so it stays in step.
local function scrollTo(offset)
    local range = scroll:GetVerticalScrollRange() or 0
    offset = math.max(0, math.min(range, offset))
    if bar and bar.SetValue then bar:SetValue(offset) else scroll:SetVerticalScroll(offset) end
end
scroll:EnableMouseWheel(true)
scroll:SetScript('OnMouseWheel', function(self, delta)
    local step = IsShiftKeyDown() and self:GetHeight() * .9 or (bodySize() + 2) * 3
    scrollTo((self:GetVerticalScroll() or 0) - delta * step)
end)

-- Tables and code need equal-width characters. The installer copies a fixed
-- width font from this machine's Windows fonts; without it, the formatter
-- falls back to one block per table row instead of aligned columns.
local MONO, monoWidth = NS.PATH..'mono.ttf', nil
local probe = panel:CreateFontString(nil, 'ARTWORK')
probe:SetPoint('TOPLEFT'); probe:SetAlpha(0)
-- SetFont reports failure by returning nothing, and measuring an unset font
-- raises, so check the result before trusting the font.
local function applyFont(region, size)
    local ok, result = pcall(region.SetFont, region, MONO, size)
    return ok and (result or ((region:GetFont() or ''):lower():find('mono') ~= nil))
end
local function monoCharWidth()
    if monoWidth ~= nil then return monoWidth end
    monoWidth = false
    if applyFont(probe, bodySize()) then
        local ok, measured = pcall(function()
            probe:SetText(string.rep('M', 10))
            return probe:GetStringWidth()
        end)
        if ok and measured and measured > 0 then monoWidth = measured / 10 end
    end
    return monoWidth
end

local function textWidth() return math.max(60, (scroll:GetWidth() or 480) - 4) end

-- Characters that fit across the panel, or 0 when there is no fixed-width font.
function NS.BodyColumns()
    local char = monoCharWidth()
    if not char then return 0 end
    return math.floor(textWidth() / char)
end

local lines, shown, blocks, lastPrompt = {}, 0, {}, nil
local function line(i)
    if not lines[i] then
        local fs = body:CreateFontString(nil, 'ARTWORK')
        fs:SetJustifyH('LEFT')
        if fs.SetJustifyV then fs:SetJustifyV('TOP') end
        if fs.SetNonSpaceWrap then fs:SetNonSpaceWrap(true) end  -- long paths and URLs
        lines[i] = fs
    end
    return lines[i]
end

-- Lay out every exchange in order. focus = 'last' brings the newest exchange
-- to the top of the view; otherwise you stay exactly where you were.
local function layout(focus)
    local width, y, n, lastTop = textWidth(), 0, 0, 0
    Size(body, width, 10)
    local function add(text, mono)
        n = n + 1
        local fs = line(n)
        if not (mono and monoCharWidth() and applyFont(fs, bodySize())) then fs:SetFontObject(ChatFontNormal) end
        fs:SetTextColor(.95, .95, .95)
        fs:SetWidth(width)
        fs:ClearAllPoints(); fs:SetPoint('TOPLEFT', body, 'TOPLEFT', 0, -y)
        fs:SetText(text ~= '' and text or ' ')
        fs:Show()
        local height = fs.GetStringHeight and tonumber((fs:GetStringHeight()))
        y = y + math.max(height or 0, bodySize()) + 2
    end
    for index, block in ipairs(blocks) do
        if index > 1 then add(''); add('|cff505050'..string.rep('-', 48)..'|r'); add('') end
        lastTop = y
        if block.prompt and block.prompt ~= '' then
            add('|cff88bbffYou:|r '..NS.Escape(block.prompt)); add('')
        end
        for _, row in ipairs(block.rows or {}) do add(row.text, row.mono) end
        if block.note then add('|cff999999'..block.note..'|r') end
    end
    for i = n + 1, #lines do lines[i]:Hide() end
    shown = n
    body:SetHeight(math.max(1, y))
    if scroll.UpdateScrollChildRect then scroll:UpdateScrollChildRect() end
    scrollTo(focus == 'last' and lastTop or (scroll:GetVerticalScroll() or 0))
end

-- The conversation so far: a list of {prompt, rows, note}. Row text is already
-- escaped (see Links.lua).
function NS.RenderTranscript(list, focus)
    blocks = list or {}
    layout(focus)
end
-- A single exchange on its own page. A different prompt starts at the top;
-- updates to the same one keep your place.
function NS.RenderBody(prompt, rows, note)
    local focus = prompt ~= lastPrompt and 'last' or nil
    lastPrompt = prompt
    NS.RenderTranscript({{prompt = prompt, rows = rows or {}, note = note}}, focus)
end
function NS.BodyText()
    local out = {}
    for i = 1, shown do out[#out+1] = lines[i]:GetText() end
    return table.concat(out, '\n')
end
-- A new width changes how tables fit, so rebuild the rows, not just the layout.
panel:SetScript('OnSizeChanged', function()
    if NS.RefreshTranscript then NS.RefreshTranscript() else layout() end
end)

-- Opening the panel lands on the newest line. The layout is redone first: a
-- frame laid out while hidden may not have known its real width yet. The
-- scroll is repeated on the next frame, once the scroll range has settled.
local settle = CreateFrame('Frame', nil, panel)
local toEnd = false
function NS.ScrollToEnd()
    scrollTo(scroll:GetVerticalScrollRange() or 0)
    toEnd = true
end
settle:SetScript('OnUpdate', function()
    if not toEnd then return end
    toEnd = false
    scrollTo(scroll:GetVerticalScrollRange() or 0)
end)
panel:HookScript('OnShow', function()
    if NS.RefreshTranscript then NS.RefreshTranscript() end
    NS.ScrollToEnd()
end)

local edit = CreateFrame('EditBox', 'AgentBridgeInput', panel, 'InputBoxTemplate')
NS.Input = edit
edit:SetPoint('BOTTOMLEFT', 28, 48); edit:SetPoint('BOTTOMRIGHT', -112, 48); edit:SetHeight(24)
edit:SetAutoFocus(false); edit:SetMaxBytes(NS.MAX_PROMPT)
edit:SetScript('OnEscapePressed', function(self) self:ClearFocus() end)
panel:SetScript('OnHide', function() edit:ClearFocus() end)

local function button(label, width, ...)
    local b = CreateFrame('Button', nil, panel, 'UIPanelButtonTemplate')
    Size(b, width, 22); b:SetPoint(...); b:SetText(label)
    return b
end
local send = button('Send', 84, 'LEFT', edit, 'RIGHT', 8, 0)
local pause = button('Pause', 90, 'BOTTOMLEFT', 20, 18)
local newChat = button('New chat', 90, 'LEFT', pause, 'RIGHT', 6, 0)
local test = button('Self-test', 90, 'LEFT', newChat, 'RIGHT', 6, 0)
local hide = button('Hide', 70, 'BOTTOMRIGHT', -34, 18)
NS.SendButton = send
hide:SetScript('OnClick', function() panel:Hide() end)
pause:SetScript('OnClick', function() if NS.IsReceiving() then NS.Pause() else NS.Resume() end end)
newChat:SetScript('OnClick', function() NS.NewChat() end)
test:SetScript('OnClick', function() NS.RunSelfTest(true) end)
function NS.OnReceiveState(active) pause:SetText(active and 'Pause' or 'Resume') end

local grip = CreateFrame('Button', nil, panel)
Size(grip, 16, 16); grip:SetPoint('BOTTOMRIGHT', -10, 10)
grip:SetNormalTexture('Interface\\ChatFrame\\UI-ChatIM-SizeGrabber-Up')
grip:SetHighlightTexture('Interface\\ChatFrame\\UI-ChatIM-SizeGrabber-Highlight')
grip:SetPushedTexture('Interface\\ChatFrame\\UI-ChatIM-SizeGrabber-Down')
grip:SetScript('OnMouseDown', function() panel:StartSizing('BOTTOMRIGHT') end)
grip:SetScript('OnMouseUp', function() panel:StopMovingOrSizing(); savePanel() end)

function NS.Toggle()
    if panel:IsShown() then panel:Hide() else panel:Show() end
end
function NS.Focus()
    panel:Show(); edit:SetFocus()
end
BINDING_HEADER_AGENTBRIDGE = 'Agent Bridge'
BINDING_NAME_AGENTBRIDGE_TOGGLE = 'Show / hide panel'
BINDING_NAME_AGENTBRIDGE_FOCUS = 'Open panel and type a prompt'

-- Minimap button: drag to move around the minimap edge.
local mm = CreateFrame('Button', 'AgentBridgeMinimapButton', Minimap)
NS.MinimapButton = mm
Size(mm, 31, 31); mm:SetFrameStrata('MEDIUM'); mm:SetFrameLevel(8)
mm:SetHighlightTexture('Interface\\Minimap\\UI-Minimap-ZoomButton-Highlight')
local icon = mm:CreateTexture(nil, 'BACKGROUND')
icon:SetTexture('Interface\\Icons\\INV_Misc_Note_05'); Size(icon, 20, 20); icon:SetPoint('TOPLEFT', 7, -5)
local ring = mm:CreateTexture(nil, 'OVERLAY')
ring:SetTexture('Interface\\Minimap\\MiniMap-TrackingBorder'); Size(ring, 53, 53); ring:SetPoint('TOPLEFT')
local badge = mm:CreateFontString(nil, 'OVERLAY', 'GameFontNormalLarge')
badge:SetPoint('TOPRIGHT', 4, 4); badge:SetText('')
local function placeMinimap()
    local a = math.rad(NS.S and NS.S.minimapAngle or 215)
    mm:ClearAllPoints(); mm:SetPoint('CENTER', Minimap, 'CENTER', 80*math.cos(a), 80*math.sin(a))
end
mm:RegisterForClicks('LeftButtonUp', 'RightButtonUp')
mm:RegisterForDrag('LeftButton')
mm:SetScript('OnClick', function(_, which) if which == 'RightButton' then NS.Focus() else NS.Toggle() end end)
mm:SetScript('OnDragStart', function(self)
    self:SetScript('OnUpdate', function()
        local mx, my = Minimap:GetCenter()
        local cx, cy = GetCursorPosition()
        local scale = Minimap:GetEffectiveScale()
        NS.S.minimapAngle = math.deg(math.atan2(cy/scale - my, cx/scale - mx))
        placeMinimap()
    end)
end)
mm:SetScript('OnDragStop', function(self) self:SetScript('OnUpdate', nil) end)
mm:SetScript('OnEnter', function(self)
    GameTooltip:SetOwner(self, 'ANCHOR_LEFT')
    GameTooltip:SetText('Agent Bridge')
    GameTooltip:AddLine('Left-click: show/hide.  Right-click: type a prompt.', 1, 1, 1)
    GameTooltip:AddLine('Drag to move.', .7, .7, .7)
    GameTooltip:Show()
end)
mm:SetScript('OnLeave', function() GameTooltip:Hide() end)
function NS.SetBadge(state)
    if not state then badge:SetText(''); return end
    badge:SetText('!')
    if state == 4 then badge:SetTextColor(.35, 1, .55) else badge:SetTextColor(1, .5, .45) end
end
panel:HookScript('OnShow', function() NS.SetBadge(nil) end)

NS.OnLoad(function(S)
    local p = S.panel
    if type(p) == 'table' and p.point and p.w and p.h then
        panel:ClearAllPoints()
        panel:SetPoint(p.point, UIParent, p.relative or p.point, p.x or 0, p.y or 0)
        Size(panel, math.max(420, p.w), math.max(300, p.h))
    end
    placeMinimap()
end)
