-- Chat panel, minimap button and completion badge (3.3.5a frame APIs only).
local NS = AgentBridge
local Size = NS.Size

local panel = CreateFrame('Frame', 'AgentBridgePanel', UIParent)
NS.Panel = panel
Size(panel, 720, 520); panel:SetPoint('CENTER'); panel:Hide()
panel:SetFrameStrata('DIALOG'); panel:SetToplevel(true); panel:SetClampedToScreen(true)
panel:SetBackdrop({bgFile = 'Interface\\DialogFrame\\UI-DialogBox-Background',
    edgeFile = 'Interface\\DialogFrame\\UI-DialogBox-Border', tile = true, tileSize = 32, edgeSize = 32,
    insets = {left = 11, right = 12, top = 12, bottom = 11}})
panel:SetMovable(true); panel:SetResizable(true); panel:SetMinResize(560, 320)
panel:EnableMouse(true); panel:RegisterForDrag('LeftButton')
tinsert(UISpecialFrames, 'AgentBridgePanel')

local function savePanel()
    local point, _, relative, x, y = panel:GetPoint()
    NS.S.panel = {point = point, relative = relative, x = x, y = y, w = panel:GetWidth(), h = panel:GetHeight(),
                  sidebar = true}
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

local SIDEBAR = 150  -- the chat list down the left
local inset = CreateFrame('Frame', nil, panel)
inset:SetPoint('TOPLEFT', 18 + SIDEBAR + 4, -48); inset:SetPoint('BOTTOMRIGHT', -18, 80)
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
-- The template only updates the slider's limits a frame after the content
-- changes, and SetValue clamps to them: without setting them here first, a
-- scroll to the end of a panel that was just opened lands at 0, the top.
local pin = 0
local function scrollTo(offset)
    local range = scroll:GetVerticalScrollRange() or 0
    offset = math.max(0, math.min(range, offset))
    if bar and bar.SetMinMaxValues then
        bar:SetMinMaxValues(0, range)
        bar:SetValue(offset)
    else
        scroll:SetVerticalScroll(offset)
    end
end
scroll:EnableMouseWheel(true)
scroll:SetScript('OnMouseWheel', function(self, delta)
    pin = 0  -- you are scrolling; stop holding the view at the end
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

local lines, shown, blocks, lastPrompt, liveLine = {}, 0, {}, nil, nil
local lineState = {}
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
        local previous = lineState[n] or {}
        local size = bodySize()
        if previous.mono ~= mono or previous.size ~= size then
            if not (mono and monoCharWidth() and applyFont(fs, size)) then fs:SetFontObject(ChatFontNormal) end
            fs:SetTextColor(.95, .95, .95)
        end
        if previous.width ~= width then fs:SetWidth(width) end
        if previous.y ~= y then fs:ClearAllPoints(); fs:SetPoint('TOPLEFT', body, 'TOPLEFT', 0, -y) end
        local value = text ~= '' and text or ' '
        if fs:GetText() ~= value then fs:SetText(value) end
        lineState[n] = {mono = mono, size = size, width = width, y = y}
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
    -- The note under a prompt still under way is updated in place each second.
    local last = blocks[#blocks]
    liveLine = last and last.live and last.note and lines[n] or nil
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
function NS.SetLiveNote(text)
    if liveLine then liveLine:SetText('|cff999999'..text..'|r') end
end
function NS.BodyText()
    local out = {}
    for i = 1, shown do out[#out+1] = lines[i]:GetText() end
    return table.concat(out, '\n')
end
-- A new width changes how tables fit, so rebuild the rows, not just the layout.
-- Wait until the next frame so anchored children have their new dimensions.
local resizePending = false
panel:SetScript('OnSizeChanged', function()
    resizePending = true
end)

-- Opening the panel lands on the newest line. The layout is redone first: a
-- frame laid out while hidden may not have known its real width yet. The view
-- is then held at the end for a few frames, in case the scroll range itself
-- settles late; scrolling the wheel lets go at once.
local settle = CreateFrame('Frame', nil, panel)
function NS.ScrollToEnd()
    scrollTo(scroll:GetVerticalScrollRange() or 0)
    pin = 10
end
settle:SetScript('OnUpdate', function()
    if resizePending then
        resizePending = false
        if NS.RefreshTranscript then NS.RefreshTranscript() else layout() end
    end
    if pin <= 0 then return end
    pin = pin - 1
    scrollTo(scroll:GetVerticalScrollRange() or 0)
end)
panel:HookScript('OnShow', function()
    if NS.RefreshTranscript then NS.RefreshTranscript() end
    NS.ScrollToEnd()
end)

local edit = CreateFrame('EditBox', 'AgentBridgeInput', panel, 'InputBoxTemplate')
NS.Input = edit
edit:SetPoint('BOTTOMLEFT', 28 + SIDEBAR + 4, 48); edit:SetPoint('BOTTOMRIGHT', -112, 48); edit:SetHeight(24)
edit:SetAutoFocus(false); edit:SetMaxBytes(NS.MAX_TYPED)
edit:SetScript('OnEscapePressed', function(self) self:ClearFocus() end)

local function button(label, width, ...)
    local b = CreateFrame('Button', nil, panel, 'UIPanelButtonTemplate')
    Size(b, width, 22); b:SetPoint(...); b:SetText(label)
    return b
end
local send = button('Send', 84, 'LEFT', edit, 'RIGHT', 8, 0)
local pause = button('Pause', 90, 'BOTTOMLEFT', 20, 18)
local copy = button('Copy', 90, 'LEFT', pause, 'RIGHT', 6, 0)
local test = button('Self-test', 90, 'LEFT', copy, 'RIGHT', 6, 0)
local hide = button('Hide', 70, 'BOTTOMRIGHT', -94, 18)
NS.SendButton = send
hide:SetScript('OnClick', function() panel:Hide() end)
pause:SetScript('OnClick', function() if NS.IsReceiving() then NS.Pause() else NS.Resume() end end)
copy:SetScript('OnClick', function() NS.ShowCopy(false) end)
test:SetScript('OnClick', function() NS.RunSelfTest(true) end)
function NS.OnReceiveState(active) pause:SetText(active and 'Pause' or 'Resume') end

-- Chats down the left: the one you are reading is highlighted, "..." marks one
-- still working and "*" one with a reply you have not read. Right-click a chat
-- to rename or delete it; the mouse wheel scrolls a long list.
local ROW = 20
local side = CreateFrame('Frame', nil, panel)
side:SetPoint('TOPLEFT', 18, -48); side:SetPoint('BOTTOMLEFT', 18, 44); side:SetWidth(SIDEBAR)
side:SetBackdrop({bgFile = 'Interface\\Tooltips\\UI-Tooltip-Background',
    edgeFile = 'Interface\\Tooltips\\UI-Tooltip-Border', tile = true, tileSize = 16, edgeSize = 16,
    insets = {left = 4, right = 4, top = 4, bottom = 4}})
side:SetBackdropColor(0, 0, 0, .65)
local newChat = CreateFrame('Button', nil, side, 'UIPanelButtonTemplate')
Size(newChat, SIDEBAR - 14, 22); newChat:SetPoint('TOP', 0, -6); newChat:SetText('+ New chat')
newChat:SetScript('OnClick', function() NS.NewChat(); edit:SetFocus() end)

local MENU = {
    {'Rename...', function(id) NS.AskRename(id) end},
    {'Delete...', function(id) NS.AskDelete(id) end},
    {'Use Claude Code', function(id) NS.UseAgent(id, 'claude') end},
    {'Use Codex', function(id) NS.UseAgent(id, 'codex') end},
    {'Model...', function(id) NS.AskModel(id) end},
    {'Cancel', function() end},
}
local menu = CreateFrame('Frame', 'AgentBridgeChatMenu', panel)
Size(menu, 128, 12 + #MENU * 22); menu:SetFrameStrata('FULLSCREEN_DIALOG'); menu:Hide()
menu:SetBackdrop({bgFile = 'Interface\\Tooltips\\UI-Tooltip-Background',
    edgeFile = 'Interface\\Tooltips\\UI-Tooltip-Border', tile = true, tileSize = 16, edgeSize = 16,
    insets = {left = 4, right = 4, top = 4, bottom = 4}})
menu:SetBackdropColor(0, 0, 0, .9)
tinsert(UISpecialFrames, 'AgentBridgeChatMenu')
for index, entry in ipairs(MENU) do
    local b = CreateFrame('Button', nil, menu, 'UIPanelButtonTemplate')
    Size(b, 112, 20); b:SetPoint('TOP', 0, -8 - (index - 1) * 22); b:SetText(entry[1])
    b:SetScript('OnClick', function() menu:Hide(); entry[2](menu.chat) end)
end

local rows, first = {}, 1
local function row(i)
    if rows[i] then return rows[i] end
    local b = CreateFrame('Button', nil, side)
    Size(b, SIDEBAR - 14, ROW); b:SetPoint('TOPLEFT', 7, -32 - (i - 1) * ROW)
    b:RegisterForClicks('LeftButtonUp', 'RightButtonUp')
    b:SetHighlightTexture('Interface\\QuestFrame\\UI-QuestTitleHighlight', 'ADD')
    b.selected = b:CreateTexture(nil, 'BACKGROUND')
    b.selected:SetAllPoints(b); b.selected:SetTexture(.25, .45, 1, .35)
    -- Which agent answers this chat, at the right; the title takes the rest.
    b.tag = b:CreateFontString(nil, 'OVERLAY', 'GameFontDisableSmall')
    b.tag:SetPoint('RIGHT', b, 'RIGHT', -3, 0); b.tag:SetJustifyH('RIGHT')
    b.label = b:CreateFontString(nil, 'OVERLAY', 'GameFontHighlightSmall')
    b.label:SetPoint('LEFT', b, 'LEFT', 4, 0); b.label:SetPoint('RIGHT', b.tag, 'LEFT', -3, 0)
    b.label:SetHeight(ROW); b.label:SetJustifyH('LEFT')
    b:SetScript('OnEnter', function(self)
        GameTooltip:SetOwner(self, 'ANCHOR_RIGHT')
        GameTooltip:SetText(NS.ChatTitle(self.chat), 1, 1, 1)
        GameTooltip:AddLine(NS.DescribeAgent(self.chat), .8, .8, .8, true)
        GameTooltip:AddLine('Right-click to rename, delete, or change agent or model.', .6, .6, .6, true)
        GameTooltip:Show()
    end)
    b:SetScript('OnLeave', function() GameTooltip:Hide() end)
    b:SetScript('OnClick', function(self, which)
        if which == 'RightButton' then
            menu.chat = self.chat
            menu:ClearAllPoints(); menu:SetPoint('TOPLEFT', self, 'TOPRIGHT', 2, 0); menu:Show()
        else
            menu:Hide(); NS.SelectChat(self.chat)
        end
    end)
    rows[i] = b
    return b
end

function NS.RefreshChats()
    if not NS.S then return end
    local list = NS.S.chats
    local fit = math.max(1, math.floor(((side:GetHeight() or 300) - 40) / ROW))
    first = math.max(1, math.min(first, #list - fit + 1))
    for i = 1, fit do
        local chat = list[first + i - 1]
        if chat then
            local b = row(i)
            b.chat = chat.id
            local mark = NS.IsChatBusy(chat.id) and '|cffffd100...|r ' or chat.unread and '|cff66ff88*|r ' or ''
            b.label:SetText(mark..NS.Escape(NS.ChatTitle(chat.id)))
            local agent = NS.ChatAgent(chat.id)
            b.tag:SetText(agent and NS.AGENTS[agent] or '')
            if chat.id == NS.S.chat then b.selected:Show() else b.selected:Hide() end
            b:Show()
        elseif rows[i] then
            rows[i]:Hide()
        end
    end
    for i = fit + 1, #rows do rows[i]:Hide() end
end
side:EnableMouseWheel(true)
side:SetScript('OnMouseWheel', function(_, delta) first = first - delta; NS.RefreshChats() end)
side:SetScript('OnSizeChanged', function() NS.RefreshChats() end)
panel:SetScript('OnHide', function() edit:ClearFocus(); menu:Hide() end)
panel:HookScript('OnShow', function()
    -- Opening the panel reads the chat on show, so it is no longer unread.
    local chat = NS.S and NS.CurrentChat and NS.CurrentChat()
    if chat then chat.unread = nil end
    NS.RefreshChats()
end)

-- A visible, generous target instead of a tiny unlabelled corner.
local grip = CreateFrame('Button', 'AgentBridgeResizeGrip', panel)
Size(grip, 28, 28); grip:SetPoint('BOTTOMRIGHT', -8, 8)
grip:EnableMouse(true); grip:RegisterForDrag('LeftButton')
grip:SetNormalTexture('Interface\\ChatFrame\\UI-ChatIM-SizeGrabber-Up')
grip:SetHighlightTexture('Interface\\ChatFrame\\UI-ChatIM-SizeGrabber-Highlight')
grip:SetPushedTexture('Interface\\ChatFrame\\UI-ChatIM-SizeGrabber-Down')
local resizeLabel = panel:CreateFontString(nil, 'OVERLAY', 'GameFontNormalSmall')
resizeLabel:SetPoint('RIGHT', grip, 'LEFT', -4, 0); resizeLabel:SetText('Resize')
local resizing = false
local function stopResize()
    if not resizing then return end
    resizing = false
    panel:StopMovingOrSizing()
    savePanel()
    resizePending = true
end
grip:SetScript('OnDragStart', function()
    resizing = true
    pin = 0
    GameTooltip:Hide()
    panel:StartSizing('BOTTOMRIGHT')
end)
grip:SetScript('OnDragStop', stopResize)
grip:SetScript('OnMouseUp', function(_, which) if which == 'LeftButton' then stopResize() end end)
panel:HookScript('OnHide', stopResize)
grip:SetScript('OnEnter', function(self)
    GameTooltip:SetOwner(self, 'ANCHOR_TOP')
    GameTooltip:SetText('Resize Agent Bridge')
    GameTooltip:AddLine('Drag this corner to change the width and height.', 1, 1, 1)
    GameTooltip:AddLine('Your window size is saved automatically.', .7, .7, .7)
    GameTooltip:Show()
end)
grip:SetScript('OnLeave', function() GameTooltip:Hide() end)

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
        -- Panels saved before the chat list existed get wider once, to make room.
        Size(panel, math.max(p.sidebar and 560 or 720, p.w), math.max(320, p.h))
    end
    placeMinimap()
end)
