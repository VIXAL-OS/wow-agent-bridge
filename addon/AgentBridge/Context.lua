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

-- Character snapshots. Work is debounced and spread across frames; the strip
-- sends missing revisions only when a prompt asks for them. No background I/O.
local MAX_ROWS, MAX_BYTES = 2000, 160000
local documents, dirty, generation, tasks = {}, {}, {}, {}
local scanErrors = {}
local requests, uploads, active = {}, {}, nil
local owner, recipeOpen = nil, false
local recipeEpoch = 0
local function clean(value, limit)
    local text = tostring(value or ''):gsub('[%c|]', ' ')
    return NS.TrimUTF8(text:sub(1, limit or 160))
end
local function json(value)
    if type(value) == 'table' then
        local out = {}; for i, v in ipairs(value) do out[i] = json(v) end
        return '['..table.concat(out, ',')..']'
    elseif type(value) == 'number' then return tostring(value)
    end
    return '"'..tostring(value):gsub('[%z\1-\31\\"]', function(c)
        if c == '"' then return '\\"' end
        if c == '\\' then return '\\\\' end
        return string.format('\\u%04x', c:byte())
    end)..'"'
end
local function keys(rows)
    local out = {}; for key in pairs(rows) do out[#out+1] = key end
    table.sort(out); return out
end
local function checksum(body)
    local a, b = 1, 0
    for i = 1, #body do
        a = (a + body:byte(i)) % 65521; b = (b + a) % 65521
        if i % 1024 == 0 then coroutine.yield() end
    end
    return string.format('%08x-%d', b*65536+a, #body)
end
local function mark(section)
    generation[section] = (generation[section] or 0) + 1
    dirty[section] = GetTime() + .4
end
local function store(section, rows, status, detail, epoch)
    local order, encoded, count, kept = keys(rows), {}, 0, {}
    for _, key in ipairs(order) do
        local line = json(rows[key]); count = count + #line
        if #encoded >= MAX_ROWS or count > MAX_BYTES - 2000 then
            status, detail = 'partial', detail..'; snapshot size limit reached'; break
        end
        encoded[#encoded+1] = line; kept[key] = rows[key]; coroutine.yield()
    end
    local body = '[1,'..json(section)..','..json(status)..','..json(clean(detail, 500))..',['..table.concat(encoded, ',')..']]'
    local rev = checksum(body)
    if section:sub(1, 8) == 'recipes:' and recipeEpoch ~= epoch then return end
    if epoch and generation[section] ~= epoch then return end
    local previous = documents[section]
    local doc = {body = body, revision = rev, rows = kept, seen = time(), session = NS.session,
        status = status, detail = clean(detail, 500), count = #encoded}
    -- A bounded one-revision baseline permits compact row patches. Do not keep
    -- a linked chain of all prior bag contents in SavedVariables.
    if previous and previous.revision ~= rev then
        local changes, removed = {}, {}
        for _, key in ipairs(keys(kept)) do
            if json(kept[key]) ~= json(previous.rows[key] or {}) then changes[#changes+1] = kept[key] end
            coroutine.yield()
        end
        for _, key in ipairs(keys(previous.rows)) do
            if not kept[key] then removed[#removed+1] = key end
            coroutine.yield()
        end
        local patch = json({2, section, status, doc.detail, previous.revision, changes, removed})
        if #patch < #body then doc.patch = patch end
    elseif previous then doc.patch = previous.patch end
    if epoch and generation[section] ~= epoch then return end
    if section:sub(1, 8) == 'recipes:' and recipeEpoch ~= epoch then return end
    documents[section] = doc
    scanErrors[section] = nil
    if section:sub(1, 8) == 'recipes:' then scanErrors.recipes = nil end
    dirty[section] = nil
    if section:sub(1, 8) == 'recipes:' and NS.S then
        NS.S.characterRecipes = NS.S.characterRecipes or {}
        NS.S.characterRecipes[owner] = NS.S.characterRecipes[owner] or {}
        NS.S.characterRecipes[owner][section] = {body = body, revision = rev, rows = kept,
            seen = doc.seen, status = status, detail = detail, count = doc.count}
    end
end
local function item(link)
    if not link then return '', '', '', false end
    local data = link:match('(item:[%d:%-]+)') or 'item:0'
    local name, _, _, level = GetItemInfo(link)
    name = name or link:match('|h%[(.-)%]|h') or 'Uncached item'
    return data, clean(name), level and tostring(level) or '', name ~= 'Uncached item'
end
local function gear(epoch)
    local rows, complete = {}, true
    for slot = 0, 19 do
        local link = GetInventoryItemLink('player', slot)
        local data, name, level, known = item(link)
        if not link and GetInventoryItemTexture and GetInventoryItemTexture('player', slot) then
            data, name, known = 'item:0', 'Uncached item', false
        end
        local stats, out = link and GetItemStats and GetItemStats(link), {}
        for _, key in ipairs(keys(stats or {})) do
            out[#out+1] = clean(_G[key] or key, 60)..'='..clean(stats[key], 24)
        end
        if data ~= '' and (not known or not level or level == '' or not stats) then complete = false end
        rows[tostring(slot)] = {tostring(slot), name, data, level, table.concat(out, ', ')}
        coroutine.yield()
    end
    store('gear', rows, complete and 'complete' or 'partial', 'Equipped slots 0-19; base item stats, not full tooltips.', epoch)
end
local function bags(epoch)
    local rows, complete, capacity, used = {}, true, 0, 0
    for bag = 0, 4 do
        local available = tonumber(GetContainerNumSlots(bag))
        if not available or available > 100 then complete = false end
        local slots = math.min(100, available or 0)
        capacity = capacity + slots
        for slot = 1, slots do
            local texture, count = GetContainerItemInfo(bag, slot)
            local link = GetContainerItemLink(bag, slot)
            if texture or link then
                local data, name, _, known = item(link)
                if data == '' then data, name = 'item:0', 'Uncached item' end
                if not known then complete = false end
                local key = bag..':'..slot
                rows[key] = {key, name, data, tostring(count or 1)}; used = used + 1
            end
            coroutine.yield()
        end
    end
    store('bags', rows, complete and 'complete' or 'partial',
        string.format('Carried bags only; %d/%d occupied slots; bank, mail and keyring excluded.', used, capacity), epoch)
end
local function recipeIdentity()
    if not recipeOpen or not GetTradeSkillLine or not IsTradeSkillLinked or IsTradeSkillLinked() then return end
    local name, rank = GetTradeSkillLine()
    if not name or name == 'UNKNOWN' or name == '' then return end
    return 'recipes:'..clean(name, 100), tonumber(rank) or 0
end
local function recipeFilters()
    -- Never clear filters or expand headers behind the user's back. Missing
    -- filter introspection is partial coverage, not an assertion of completeness.
    local reasons = {}
    if not GetTradeSkillSubClassFilter or not GetTradeSkillSubClassFilter(0) then reasons[#reasons+1] = 'category filter' end
    if not GetTradeSkillInvSlotFilter or not GetTradeSkillInvSlotFilter(0) then reasons[#reasons+1] = 'slot filter' end
    local edit, available = TradeSkillFrameEditBox, TradeSkillFrameAvailableFilterCheckButton
    local search = edit and edit:GetText()
    -- The stock 3.3.5 UI writes localized SEARCH into an empty edit box on
    -- show/focus loss; TradeSkillFilter_OnTextChanged treats it as no name filter.
    if SEARCH and search == SEARCH then search = '' end
    local text = GetTradeSkillItemNameFilter and GetTradeSkillItemNameFilter() or search
    if text == nil or text ~= '' then reasons[#reasons+1] = 'text/level filter or unknown filter state' end
    if search and search ~= '' then reasons[#reasons+1] = 'profession search field' end
    if GetTradeSkillItemLevelFilter then
        local low, high = GetTradeSkillItemLevelFilter()
        if (tonumber(low) or 0) > 0 or (tonumber(high) or 0) > 0 then reasons[#reasons+1] = 'level filter' end
    elseif not edit then
        reasons[#reasons+1] = 'level filter state unavailable'
    end
    if not available or available:GetChecked() then reasons[#reasons+1] = 'makeable filter or unknown filter state' end
    return table.concat(reasons, ', ')
end
local function recipes(epoch)
    local section, rank = recipeIdentity()
    if not section then return end
    local total = math.min(MAX_ROWS, tonumber(GetNumTradeSkills()) or 0)
    if total == 0 then return end -- transient empty lists must not erase a saved scan
    local rows, reasons = {}, recipeFilters()
    for index = 1, total do
        if recipeIdentity() ~= section or recipeEpoch ~= epoch then return end
        local name, kind, _, expanded = GetTradeSkillInfo(index)
        if not name then reasons = 'recipe data still loading'
        elseif kind == 'header' or kind == 'subheader' then
            if not expanded then reasons = 'collapsed categories' end
        else
            local recipe = GetTradeSkillRecipeLink and GetTradeSkillRecipeLink(index) or ''
            local id = recipe:match('enchant:(%d+)') or recipe:match('spell:(%d+)')
            local output = GetTradeSkillItemLink and GetTradeSkillItemLink(index) or ''
            local key = id or ('name:'..clean(name))
            rows[key] = {key, clean(name), output:match('item:(%d+)') or '0'}
        end
        coroutine.yield()
    end
    if tonumber(GetNumTradeSkills()) > MAX_ROWS then reasons = 'recipe count limit reached' end
    if recipeIdentity() ~= section or recipeEpoch ~= epoch then return end
    local status = reasons == '' and 'complete' or 'partial'
    local detail = 'Profession rank '..rank..'. '
    if status == 'partial' then
        detail = detail..reasons..'; includes previously observed recipes; absence does not mean unlearned.'
        for key, row in pairs(documents[section] and documents[section].rows or {}) do
            if not rows[key] then rows[key] = row end
            coroutine.yield()
        end
    else detail = detail..'Unfiltered scan with expanded categories.' end
    generation[section] = epoch
    store(section, rows, status, detail, epoch)
end
local function professionKnown(section)
    local name = section:sub(9)
    for i = 1, GetNumSkillLines and GetNumSkillLines() or 0 do
        if GetSkillLineInfo(i) == name then return true end
    end
    return false
end
function NS.CharacterFields(fields)
    local bundle = {owner = owner, docs = {}}
    if not owner then return bundle end
    fields[#fields+1] = {'character', owner}
    for _, section in ipairs(keys(documents)) do
        local doc = documents[section]
        if section:sub(1, 8) ~= 'recipes:' or professionKnown(section) then
            local isRecipe = section:sub(1, 8) == 'recipes:'
            local stale = dirty[section] or scanErrors[section] or (isRecipe and scanErrors.recipes)
            local freshness = stale and 'stale' or doc.session ~= NS.session and 'cached' or 'current'
            if not stale and isRecipe and recipeIdentity() ~= section then freshness = 'cached' end
            fields[#fields+1] = {'snapshot', section..'|'..doc.revision..'|'..doc.seen..'|'..freshness}
            bundle.docs[section] = doc
        end
    end
    fields[#fields+1] = {'ctx', 'Gear/bags are event-cached observations. Unlisted recipe professions are unscanned; open each profession to scan. Bank/mail/keyring are not included.'}
    if dirty.gear or dirty.bags then fields[#fields+1] = {'ctx', 'Inventory scan pending; cached inventory may be stale. Send again after the scan finishes.'} end
    for section in pairs(scanErrors) do fields[#fields+1] = {'ctx', section..' scan failed; do not treat cached data as current.'} end
    return bundle
end
function NS.RememberCharacter(request, bundle) requests[request] = bundle end
function NS.CharacterAck(request) requests[request] = nil end
local function failSync(request, reason)
    requests[request] = nil
    NS.AckPrompt(request); NS.ForgetRequest(request)
    NS.ShowReply(request, 'Character sync failed: '..reason..' Send the prompt again to retry.', 5, true)
    NS.SetStatus('Character sync failed. The agent was not started; send again to retry.')
end
local function uploadBody(section, doc, full)
    return not full and doc.patch or doc.body
end
function NS.CharacterNeed(request, text)
    if text:sub(1, 8) ~= '\1ABCTX1\n' then return false end
    local bundle = requests[request]
    if not bundle or not NS.S.context then return true end
    bundle.queued = bundle.queued or {}
    for line in text:sub(9):gmatch('[^\n]+') do
        local section, rev = line:match('^([^|]+)|([%x]+%-%d+)$')
        local doc = section and bundle.docs[section]
        if doc and doc.revision == rev and not bundle.queued[section] then
            bundle.queued[section] = true
            uploads[#uploads+1] = {parent = request, section = section, doc = doc, owner = bundle.owner}
        end
    end
    NS.ShowReply(request, 'Syncing gear, bags and recipe changes with the companion...', 0, true)
    return true
end
local function uploadStep()
    if active then return end
    local job = table.remove(uploads, 1)
    if not job then return end
    if not requests[job.parent] or not NS.S.context then return end
    active = job
    local payload = uploadBody(job.section, job.doc, job.full)
    local pages, pos = {}, 1
    while pos <= #payload do
        local chunk = NS.TrimUTF8(payload:sub(pos, pos+4999))
        pages[#pages+1] = chunk; pos = pos + #chunk
    end
    local function sendPage(index)
        if not requests[job.parent] then active = nil; return end
        local seq = NS.NextRequest(); job.request = seq
        local fields = {{'character', job.owner}, {'section', job.section}, {'revision', job.doc.revision},
                        {'page', index}, {'total', #pages}}
        local ok = NS.BeginRequest(seq, nil, function(reply, state)
            if state ~= 4 or reply ~= 'ABCTX_OK' then
                active = nil
                if reply == 'ABCTX_BASE_MISSING' and not job.full then
                    job.full = true; table.insert(uploads, 1, job)
                else failSync(job.parent, reply) end
            elseif index < #pages then sendPage(index+1)
            else active = nil end
        end, 120)
        if not ok then active = nil; failSync(job.parent, 'Reply channel unavailable.'); return end
        NS.QueuePrompt(seq, NS.EncodeCharacter(NS.Envelope(fields, pages[index]), NS.session, seq))
        NS.SetStatus('Syncing '..job.section..' ('..index..'/'..#pages..')...')
    end
    sendPage(1)
end
function NS.CharacterContextOff()
    for request in pairs(requests) do failSync(request, 'Context sharing was turned off.') end
    if active then NS.AckPrompt(active.request); NS.ForgetRequest(active.request); active = nil end
    uploads, tasks = {}, {}
end
function NS.CharacterStatus()
    local out = {}
    for _, section in ipairs(keys(documents)) do
        local doc = documents[section]
        out[#out+1] = section..': '..doc.count..' records, '..doc.status..', scanned '..math.max(0, time()-doc.seen)..'s ago'
    end
    return table.concat(out, '\n')
end
local function loadOwner()
    if not NS.S then return end
    local realm, name = GetRealmName(), UnitName('player')
    if not realm or realm == '' or not name or name == '' or name == UNKNOWNOBJECT or name == 'Unknown' then return end
    local identity = clean(realm..':'..(UnitGUID and UnitGUID('player') or name), 256)
    if identity == owner then return end
    if owner then NS.CharacterContextOff() end
    owner = identity
    documents, dirty, generation, tasks, scanErrors = {}, {}, {}, {}, {}
    local saved = type(NS.S.characterRecipes) == 'table' and NS.S.characterRecipes[owner]
    if type(saved) == 'table' then
        for section, doc in pairs(saved) do
            if type(section) == 'string' and section:sub(1, 8) == 'recipes:' and type(doc) == 'table'
                and type(doc.body) == 'string' and #doc.body <= MAX_BYTES and type(doc.rows) == 'table'
                and type(doc.revision) == 'string' and type(doc.seen) == 'number'
                and type(doc.count) == 'number' and type(doc.status) == 'string' and type(doc.detail) == 'string' then
                documents[section] = doc
            end
        end
    end
    mark('gear'); mark('bags')
end
local driver = CreateFrame('Frame')
for _, event in ipairs({'PLAYER_ENTERING_WORLD', 'PLAYER_EQUIPMENT_CHANGED', 'UNIT_INVENTORY_CHANGED', 'BAG_UPDATE',
    'GET_ITEM_INFO_RECEIVED', 'TRADE_SKILL_SHOW', 'TRADE_SKILL_UPDATE', 'TRADE_SKILL_FILTER_UPDATE',
    'TRADE_SKILL_CLOSE', 'SKILL_LINES_CHANGED'}) do pcall(driver.RegisterEvent, driver, event) end
driver:SetScript('OnEvent', function(_, event, unit)
    if event == 'PLAYER_ENTERING_WORLD' then loadOwner() end
    if event == 'UNIT_INVENTORY_CHANGED' and unit ~= 'player' then return end
    if event == 'TRADE_SKILL_CLOSE' then recipeOpen = false; recipeEpoch = recipeEpoch + 1
    elseif event:find('TRADE_SKILL') then
        if event == 'TRADE_SKILL_SHOW' then recipeOpen = true end
        recipeEpoch = recipeEpoch + 1; dirty.recipes = GetTime() + .5
        local section = recipeIdentity()
        if section then dirty[section] = GetTime() end
    elseif event == 'BAG_UPDATE' then mark('bags')
    else
        mark('gear'); mark('bags')
        if event == 'SKILL_LINES_CHANGED' then
            for section in pairs(documents) do if section:sub(1, 8) == 'recipes:' then dirty[section] = GetTime() end end
            recipeEpoch = recipeEpoch + 1; dirty.recipes = GetTime() + .5
        end
    end
end)
local function work()
    if not NS.S or not NS.S.context or not owner then return end
    uploadStep()
    if InCombatLockdown and InCombatLockdown() then return end
    local now = GetTime()
    for section, fn in pairs({gear = gear, bags = bags, recipes = recipes}) do
        local available = section == 'gear' and GetInventoryItemLink or section == 'bags' and GetContainerNumSlots
            or section == 'recipes' and recipeIdentity()
        if available and dirty[section] and now >= dirty[section] and not tasks[section] then
            local epoch = section == 'recipes' and recipeEpoch or generation[section]
            tasks[section] = coroutine.create(function() fn(epoch) end)
            if section == 'recipes' then dirty.recipes = nil end
        end
        local task = tasks[section]
        if task then
            local started = debugprofilestop and debugprofilestop()
            for _ = 1, 8 do
                local ok, error = coroutine.resume(task)
                if not ok then
                    tasks[section] = nil; dirty[section] = nil; scanErrors[section] = true
                    NS.Print('Character '..section..' scan unavailable: '..clean(error, 160)); break
                end
                if coroutine.status(task) == 'dead' then tasks[section] = nil; break end
                if started and debugprofilestop() - started >= .5 then break end
            end
        end
    end
end
driver:SetScript('OnUpdate', function() NS.Profile('character-context', work) end)
NS.OnLoad(function(S)
    loadOwner()
end)
