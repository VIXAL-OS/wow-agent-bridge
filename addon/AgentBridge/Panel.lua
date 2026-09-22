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

local body = CreateFrame('ScrollingMessageFrame', 'AgentBridgeBody', inset)
NS.Body = body
body:SetPoint('TOPLEFT', 10, -8); body:SetPoint('BOTTOMRIGHT', -10, 8)
body:SetFontObject(ChatFontNormal); body:SetJustifyH('LEFT'); body:SetFading(false); body:SetMaxLines(3000)
if body.SetHyperlinksEnabled then body:SetHyperlinksEnabled(true) end
body:EnableMouseWheel(true)
body:SetScript('OnMouseWheel', function(self, delta)
    local step = IsShiftKeyDown() and self.PageUp or self.ScrollUp
    if delta < 0 then step = IsShiftKeyDown() and self.PageDown or self.ScrollDown end
    for _ = 1, IsShiftKeyDown() and 1 or 3 do step(self) end
end)

-- Tables and code need equal-width characters. The installer copies a fixed
-- width font from this machine's Windows fonts; without it, the formatter
-- falls back to one block per table row instead of aligned columns.
local MONO, monoWidth = NS.PATH..'mono.ttf', nil
local probe = panel:CreateFontString(nil, 'ARTWORK')
probe:SetPoint('TOPLEFT'); probe:SetAlpha(0)
local function bodySize()
    local _, size = body:GetFont()
    return tonumber(size) or 12
end
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

-- Characters that fit across the panel, or 0 when there is no fixed-width font.
function NS.BodyColumns()
    local char = monoCharWidth()
    if not char then return 0 end
    return math.floor((body:GetWidth() - 12) / char)
end

local function setBodyFont(mono)
    if mono and monoCharWidth() and applyFont(body, bodySize()) then return end
    body:SetFontObject(ChatFontNormal)
end

-- A scrolling message frame stacks messages against its bottom edge, which
-- leaves a reply floating at the bottom under empty space and pushes later
-- lines out of view. Trailing blank lines put the text at the top instead.
local function fillBelow()
    local _, size = body:GetFont()
    for _ = 1, math.ceil(body:GetHeight() / ((tonumber(size) or 12) + 3)) + 2 do
        body:AddMessage(' ')
    end
end

-- Show the current exchange. `markup` is already escaped (see Links.lua).
function NS.RenderBody(prompt, markup, note, mono)
    setBodyFont(mono)
    body:Clear()
    if prompt and prompt ~= '' then
        body:AddMessage('|cff88bbffYou:|r '..NS.Escape(prompt))
        body:AddMessage(' ')
    end
    for line in ((markup or '')..'\n'):gmatch('(.-)\r?\n') do
        body:AddMessage(line ~= '' and line or ' ', 1, 1, 1)
    end
    if note then body:AddMessage('|cff999999'..note..'|r') end
    fillBelow()
    body:ScrollToTop()
end

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
