-- Minimal WoW 3.3.5a API surface for running the addon under plain Lua 5.1.
-- Only APIs the addon uses exist; missing methods fall back to no-ops, except
-- APIs absent from 3.3.5a, which are nil so feature checks see them missing
-- and direct calls fail the tests.
local py = ...  -- Python bridge: py.now(), py.measure(path, size, text), py.load(path)

local ABSENT = {SetColorTexture = true, SetShown = true, SetSize = true, SetResizeBounds = true,
    SetHyperlinksEnabled = true, SetWordWrap = true}
-- Plain data fields read back as nil when unset, like real widget state;
-- only missing *methods* fall back to a no-op.
local DATA = {font = true, size = true, color = true, file = true, focus = true, width = true, height = true,
    child = true, vscroll = true}
local allFrames = {}
STUB = {frames = allFrames, events = {}, messages = {}, prints = {}, sounds = {}}

local Object = {}
Object.__index = function(self, key)
    if ABSENT[key] or DATA[key] then return nil end
    local method = rawget(Object, key)
    if method then return method end
    return function() end
end
local function new(kind, name, parent)
    local o = setmetatable({kind = kind, name = name, parent = parent, scripts = {}, shown = true,
        children = {}, textures = {}, lines = {}, text = '', points = {}}, Object)
    if name then _G[name] = o end
    allFrames[#allFrames+1] = o
    return o
end
function Object:SetScript(event, fn) self.scripts[event] = fn end
function Object:GetScript(event) return self.scripts[event] end
function Object:HookScript(event, fn)
    local old = self.scripts[event]
    self.scripts[event] = function(...) if old then old(...) end fn(...) end
end
function Object:Show() self.shown = true end
function Object:Hide() self.shown = false end
function Object:IsShown() return self.shown end
function Object:RegisterEvent(e) STUB.events[e] = STUB.events[e] or {}; STUB.events[e][self] = true end
function Object:UnregisterEvent(e) if STUB.events[e] then STUB.events[e][self] = nil end end
function Object:CreateTexture() local t = new('Texture'); self.textures[#self.textures+1] = t; return t end
function Object:SetTexture(a, g, b) if type(a) == 'number' then self.color = a end self.file = a end
-- Frames are positioned by anchors here, so report the real panel's rough size.
function Object:GetWidth() return self.width or 520 end
function Object:GetHeight() return self.height or 400 end
function Object:SetWidth(w) self.width = w end
function Object:SetHeight(h) self.height = h end
function Object:GetPoint() return 'CENTER', nil, 'CENTER', 0, 0 end
function Object:GetEffectiveScale() return 1 end
function Object:GetCenter() return 0, 0 end
-- ScrollingMessageFrame
function Object:AddMessage(text) self.lines[#self.lines+1] = text end
function Object:Clear() self.lines = {} end
-- EditBox
function Object:SetText(t) self.text = t end
function Object:GetText() return self.text end
function Object:Insert(t) self.text = self.text..t end
function Object:HasFocus() return self.focus end
function Object:SetFocus() self.focus = true end
function Object:ClearFocus() self.focus = false end
-- FontString: widths come from the real font file via Python.
function Object:CreateFontString()
    local fs = new('FontString'); fs.parent = self
    return fs
end
function Object:SetFont(path, size)
    if self.kind ~= 'FontString' then return end
    if not py.load(path) then return nil end
    self.font, self.size = path, size
    return 1
end
function Object:GetFont() return self.font, self.size end
function Object:GetStringHeight() return 14 end
-- ScrollFrame: the range is how far the child overhangs the visible area.
function Object:SetScrollChild(child) self.child = child end
function Object:GetVerticalScrollRange()
    return self.child and math.max(0, (self.child.height or 0) - self:GetHeight()) or 0
end
function Object:GetVerticalScroll() return self.vscroll or 0 end
function Object:SetVerticalScroll(value) self.vscroll = value end
function Object:GetStringWidth()
    if not self.font then error('Font not set') end
    return py.measure(self.font, self.size, self.text)
end

function CreateFrame(kind, name, parent)
    local f = new(kind, name, parent)
    if kind == 'ScrollingMessageFrame' or kind == 'EditBox' then f.lines = {} end
    return f
end
UIParent, Minimap, WorldFrame = new('Frame', 'UIParent'), new('Frame', 'Minimap'), new('Frame', 'WorldFrame')
DEFAULT_CHAT_FRAME = {AddMessage = function(_, text) STUB.prints[#STUB.prints+1] = text end}
GameTooltip = new('GameTooltip', 'GameTooltip')
ChatFontNormal = {}
UISpecialFrames = {}
tinsert = table.insert
SlashCmdList = {}
function GetTime() return py.now() end
function time() return 1758000000 + math.floor(py.now()) end
function GetCVar(name) if name == 'gxResolution' then return '1920x1080' end end
function GetItemInfo() return nil end
function GetCursorInfo() return nil end
function IsShiftKeyDown() return false end
function IsModifiedClick() return false end
function GetCursorPosition() return 0, 0 end
function PlaySound(s) STUB.sounds[#STUB.sounds+1] = s end
function hooksecurefunc() end
function ClearCursor() end
function SetItemRef() end
ChatEdit_InsertLink = function() return false end
OpenStackSplitFrame = function() end

function STUB.fire(event, ...)
    for frame in pairs(STUB.events[event] or {}) do
        local fn = frame.scripts.OnEvent
        if fn then fn(frame, event, ...) end
    end
end
function STUB.update(dt)
    for _, f in ipairs(allFrames) do
        local fn = f.scripts.OnUpdate
        if fn and f.shown then fn(f, dt) end
    end
end
-- Read the strip back as a 64-byte string (nil while hidden). Each bit is a
-- pair of neighbouring cells: light then dark means 1, as the companion reads it.
function STUB.strip()
    local strip = _G.AgentBridgeStrip
    if not strip.shown then return nil end
    local bits, bytes = {}, {}
    for i = 0, 511 do
        local index = math.floor(i/64)*128 + 2*(i % 64) + 1
        local light, dark = strip.textures[index].color or 0, strip.textures[index+1].color or 0
        if light == dark then return nil end  -- never transmit an unreadable frame
        bits[i] = light > dark and 1 or 0
    end
    for i = 0, 63 do
        local v = 0
        for bit = 0, 7 do v = v*2 + bits[i*8 + bit] end
        bytes[#bytes+1] = string.char(v)
    end
    return table.concat(bytes)
end
