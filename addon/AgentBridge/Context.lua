-- What the agent is told alongside a prompt: who and where you are (so "what
-- should I do here at my level?" needs no explaining) and the tooltip text of
-- anything you link. Only your own character's state; /ab context off stops it.
local NS = AgentBridge

local function money(copper)
    return string.format('%dg %ds %dc', math.floor(copper / 10000), math.floor(copper / 100) % 100, copper % 100)
end

local function talents()
    local out = {}
    for tab = 1, tonumber((GetNumTalentTabs and GetNumTalentTabs())) or 0 do
        local name, _, points = GetTalentTabInfo(tab)
        if name then out[#out+1] = name..' '..(points or 0) end
    end
    return table.concat(out, ' / ')
end

-- enUS headers; on other locales the list is simply left out.
local PROFESSIONS = {Professions = true, ['Secondary Skills'] = true}
local function professions()
    local out, header = {}, nil
    for i = 1, tonumber((GetNumSkillLines and GetNumSkillLines())) or 0 do
        local name, isHeader, _, rank, _, _, maxRank = GetSkillLineInfo(i)
        if isHeader then
            header = name
        elseif header and PROFESSIONS[header] and name then
            out[#out+1] = string.format('%s %d/%d', name, rank or 0, maxRank or 0)
        end
    end
    return table.concat(out, ', ')
end

local function location()
    local zone, sub = GetRealZoneText() or '', GetSubZoneText() or ''
    local where = (sub ~= '' and sub ~= zone) and (zone..' - '..sub) or zone
    -- Only move the map to your zone when it is closed, so an open map is left alone.
    if SetMapToCurrentZone and not (WorldMapFrame and WorldMapFrame:IsShown()) then SetMapToCurrentZone() end
    local x, y = GetPlayerMapPosition('player')
    if x and x > 0 then where = where..string.format(' (%.1f, %.1f)', x * 100, y * 100) end
    local inside, kind = IsInInstance()
    if inside then
        local name = GetInstanceInfo and GetInstanceInfo()
        where = where..', inside '..(name or kind or 'an instance')
    end
    return where
end

function NS.GameContext()
    local version, build = GetBuildInfo()
    local guild = GetGuildInfo('player')
    local lines = {
        string.format('Client: World of Warcraft %s (build %s), realm %s', tostring(version), tostring(build),
            tostring(GetRealmName())),
        string.format('Character: %s, level %s %s %s (%s)%s', tostring(UnitName('player')),
            tostring(UnitLevel('player')), tostring(UnitRace('player')), tostring(UnitClass('player')),
            tostring(UnitFactionGroup('player')), guild and (', guild <'..guild..'>') or ''),
        'Location: '..location(),
        'Money: '..money(tonumber((GetMoney())) or 0),
    }
    local t, p = talents(), professions()
    if t ~= '' then lines[#lines+1] = 'Talents: '..t end
    if p ~= '' then lines[#lines+1] = 'Professions: '..p end
    return table.concat(lines, '\n')
end

-- The text of a link's tooltip (an item's stats, a spell's description), read
-- from a hidden tooltip. Colour and texture codes are dropped: the agent gets
-- words, and nothing here can reach the chat as markup.
local tip = CreateFrame('GameTooltip', 'AgentBridgeTip', nil, 'GameTooltipTemplate')
local function plain(s)
    return (s:gsub('|c%x%x%x%x%x%x%x%x', ''):gsub('|r', ''):gsub('|T.-|t', ''))
end
function NS.LinkTooltip(data)
    tip:SetOwner(WorldFrame, 'ANCHOR_NONE')
    tip:ClearLines()
    if not pcall(tip.SetHyperlink, tip, data) then tip:Hide(); return nil end
    local out = {}
    for i = 1, tonumber((tip:NumLines())) or 0 do
        local left, right = _G['AgentBridgeTipTextLeft'..i], _G['AgentBridgeTipTextRight'..i]
        local l = left and left:GetText()
        local r = right and right:IsShown() and right:GetText()
        if l and l ~= '' then out[#out+1] = plain((r and r ~= '') and (l..'   '..r) or l) end
    end
    tip:Hide()
    local text = table.concat(out, '\n')
    return text ~= '' and text or nil
end
