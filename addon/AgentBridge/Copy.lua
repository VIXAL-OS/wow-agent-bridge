-- Selectable text over the transcript, inside the same panel. A snapshot keeps
-- incoming replies from disrupting a selection. Edits are discarded.
local NS = AgentBridge
local Size = NS.Size

local frame = CreateFrame('Frame', 'AgentBridgeCopy', NS.Panel)
frame:SetPoint('TOPLEFT', 172, -48); frame:SetPoint('BOTTOMRIGHT', -18, 80); frame:Hide()
frame:SetFrameLevel(NS.Panel:GetFrameLevel() + 20); frame:EnableMouse(true)
frame:SetBackdrop({bgFile = 'Interface\\DialogFrame\\UI-DialogBox-Background',
    edgeFile = 'Interface\\DialogFrame\\UI-DialogBox-Border', tile = true, tileSize = 32, edgeSize = 32,
    insets = {left = 11, right = 12, top = 12, bottom = 11}})
-- Selection stays inside the transcript area and is frozen while replies stream.
function NS.CloseCopy() frame:Hide() end
NS.Panel:HookScript('OnHide', NS.CloseCopy)

local title = frame:CreateFontString(nil, 'OVERLAY', 'GameFontNormal')
title:SetPoint('TOPLEFT', 16, -14); title:SetPoint('TOPRIGHT', -16, -14)
title:SetJustifyH('LEFT')

local scroll = CreateFrame('ScrollFrame', 'AgentBridgeCopyScroll', frame, 'UIPanelScrollFrameTemplate')
scroll:SetPoint('TOPLEFT', 16, -52); scroll:SetPoint('BOTTOMRIGHT', -34, 42)
local box = CreateFrame('EditBox', 'AgentBridgeCopyBox', scroll)
box:SetMultiLine(true); box:SetAutoFocus(false); box:SetFontObject(ChatFontNormal)
box:SetWidth(490); box:SetHeight(200); box:SetMaxLetters(0)
local function resize()
    box:SetWidth(math.max(80, scroll:GetWidth() - 4))
end
frame:SetScript('OnSizeChanged', resize)
frame:SetScript('OnHide', function() box:ClearFocus() end)
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
    title:SetText((which and 'Whole chat' or 'Last reply')..' - drag to select, Ctrl+C to copy. Escape returns.')
    if not NS.Panel:IsShown() then NS.Panel:Show() end
    NS.Input:ClearFocus()
    resize()
    frame:Show()
    box:SetText(shown)
    box:SetFocus(); box:HighlightText()
end

local function button(label, width, ...)
    local b = CreateFrame('Button', nil, frame, 'UIPanelButtonTemplate')
    Size(b, width, 22); b:SetPoint(...); b:SetText(label)
    return b
end
button('Last reply', 80, 'BOTTOMLEFT', 12, 12):SetScript('OnClick', function() NS.ShowCopy(false) end)
button('Whole chat', 84, 'BOTTOMLEFT', 96, 12):SetScript('OnClick', function() NS.ShowCopy(true) end)
button('Done', 60, 'BOTTOMRIGHT', -12, 12):SetScript('OnClick', function() frame:Hide() end)
