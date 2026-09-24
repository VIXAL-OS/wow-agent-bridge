-- A box to copy replies out of the game: WoW text cannot be selected, but an
-- edit box can. The text is highlighted on open, so Ctrl+C then Escape is all
-- it takes. Edits are thrown away; the box always shows the reply as sent.
local NS = AgentBridge
local Size = NS.Size

local frame = CreateFrame('Frame', 'AgentBridgeCopy', UIParent)
Size(frame, 560, 400); frame:SetPoint('CENTER'); frame:Hide()
frame:SetFrameStrata('FULLSCREEN_DIALOG'); frame:SetToplevel(true); frame:SetClampedToScreen(true)
frame:SetBackdrop({bgFile = 'Interface\\DialogFrame\\UI-DialogBox-Background',
    edgeFile = 'Interface\\DialogFrame\\UI-DialogBox-Border', tile = true, tileSize = 32, edgeSize = 32,
    insets = {left = 11, right = 12, top = 12, bottom = 11}})
frame:SetMovable(true); frame:EnableMouse(true); frame:RegisterForDrag('LeftButton')
frame:SetScript('OnDragStart', function(self) self:StartMoving() end)
frame:SetScript('OnDragStop', function(self) self:StopMovingOrSizing() end)
tinsert(UISpecialFrames, 'AgentBridgeCopy')

local title = frame:CreateFontString(nil, 'OVERLAY', 'GameFontNormal')
title:SetPoint('TOPLEFT', 20, -18)

local scroll = CreateFrame('ScrollFrame', 'AgentBridgeCopyScroll', frame, 'UIPanelScrollFrameTemplate')
scroll:SetPoint('TOPLEFT', 20, -40); scroll:SetPoint('BOTTOMRIGHT', -38, 46)
local box = CreateFrame('EditBox', 'AgentBridgeCopyBox', scroll)
box:SetMultiLine(true); box:SetAutoFocus(false); box:SetFontObject(ChatFontNormal)
box:SetWidth(490); box:SetMaxLetters(0)
scroll:SetScrollChild(box)

local shown, which = '', false
box:SetScript('OnEscapePressed', function() frame:Hide() end)
box:SetScript('OnTextChanged', function(self, typed)
    if typed then self:SetText(shown); self:HighlightText() end
    if ScrollingEdit_OnTextChanged then ScrollingEdit_OnTextChanged(self, scroll) end
end)
if ScrollingEdit_OnCursorChanged then box:SetScript('OnCursorChanged', ScrollingEdit_OnCursorChanged) end
if ScrollingEdit_OnUpdate then
    box:SetScript('OnUpdate', function(self, elapsed) ScrollingEdit_OnUpdate(self, elapsed, scroll) end)
end

-- Pipes that would start WoW markup are doubled so they show as typed.
local function literal(text) return (text:gsub('|([cCrRhHtTkKnN])', '||%1')) end

function NS.ShowCopy(all)
    which = all and true or false
    local text = NS.CopyText(which)
    if text == '' then text = '(Nothing to copy in this chat yet.)' end
    shown = literal(text)
    title:SetText((which and 'Whole chat' or 'Last reply')..' - press Ctrl+C to copy, then Escape')
    frame:Show()
    box:SetText(shown)
    box:SetFocus(); box:HighlightText()
end

local function button(label, width, ...)
    local b = CreateFrame('Button', nil, frame, 'UIPanelButtonTemplate')
    Size(b, width, 22); b:SetPoint(...); b:SetText(label)
    return b
end
button('Last reply', 100, 'BOTTOMLEFT', 20, 16):SetScript('OnClick', function() NS.ShowCopy(false) end)
button('Whole chat', 100, 'BOTTOMLEFT', 126, 16):SetScript('OnClick', function() NS.ShowCopy(true) end)
button('Close', 80, 'BOTTOMRIGHT', -20, 16):SetScript('OnClick', function() frame:Hide() end)
