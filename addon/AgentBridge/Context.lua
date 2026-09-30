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
-- sends revisions for prompts or an explicit manual sync. No background I/O.
local MAX_ROWS, MAX_BYTES = 2000, 160000
local documents, dirty, generation, tasks = {}, {}, {}, {}
local synced, confirmations, confirming = {}, {}, nil
local scanErrors = {}
local requests, uploads, active = {}, {}, nil
local manual
local owner, recipeOpen = nil, false
local stableOpen = false
local COLLECTIONS = {mounts = 'MOUNT', pets = 'CRITTER'}
local SAVED_COLLECTIONS = {mounts = true, pets = true, stablepets = true}
-- Fixed partitions bound each document and keep small progress changes local.
local ACHIEVEMENT_PARTS = 8
local function achievementSection(section) return section:match('^achievements:[1-8]$') ~= nil end
local function achievementAPIs()
    return GetCategoryList and GetCategoryNumAchievements and GetAchievementInfo
        and GetAchievementNumCriteria and GetAchievementCriteriaInfo
end
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
local function rowHashes(doc)
    if doc.hashes then return doc.hashes end
    local hashes = {}
    for key, row in pairs(doc.rows) do
        hashes[key] = checksum(json(row)); coroutine.yield()
    end
    doc.hashes = hashes
    return hashes
end
local function advance(task)
    local started = debugprofilestop and debugprofilestop()
    for _ = 1, 32 do
        local ok, error = coroutine.resume(task)
        if not ok then return false, error end
        if coroutine.status(task) == 'dead' then return true end
        if started and debugprofilestop() - started >= .5 then break end
    end
end
local function saveSynced(section, doc)
    local baseline = {revision = doc.revision, hashes = doc.hashes}
    synced[section] = baseline
    if type(NS.S.characterSynced) ~= 'table' then NS.S.characterSynced = {} end
    if type(NS.S.characterSynced[owner]) ~= 'table' then NS.S.characterSynced[owner] = {} end
    NS.S.characterSynced[owner][section] = baseline
end
local function rememberSynced(identity, section, doc)
    if identity ~= owner then return end
    if doc.hashes then
        saveSynced(section, doc); confirmations[section] = nil
    else confirmations[section] = {owner = identity, doc = doc} end
end
local function confirmStep()
    if not confirming then
        local section, entry = next(confirmations)
        if not section then return end
        confirming = {section = section, entry = entry, task = coroutine.create(function()
            rowHashes(entry.doc)
            if owner ~= entry.owner or confirmations[section] ~= entry then return end
            -- Persist only one confirmed revision and compact row fingerprints.
            -- Scans never move this baseline; only a complete acknowledgement can.
            saveSynced(section, entry.doc)
        end)}
    end
    local done, error = advance(confirming.task)
    if done ~= nil then
        if confirmations[confirming.section] == confirming.entry then confirmations[confirming.section] = nil end
        confirming = nil
        if error then NS.Print('Sync cache unavailable: '..clean(error, 160)) end
    end
end
local function forgetSynced(identity, section, revision)
    if identity ~= owner or not synced[section] or synced[section].revision ~= revision then return end
    synced[section] = nil
    if NS.S.characterSynced and NS.S.characterSynced[owner] then NS.S.characterSynced[owner][section] = nil end
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
    if achievementSection(section) and generation.achievements ~= epoch then return end
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
    if achievementSection(section) and generation.achievements ~= epoch then return end
    documents[section] = doc
    scanErrors[section] = nil
    if section:sub(1, 8) == 'recipes:' then scanErrors.recipes = nil end
    dirty[section] = nil
    local bucket = section:sub(1, 8) == 'recipes:' and 'characterRecipes'
        or achievementSection(section) and 'characterAchievements'
        or SAVED_COLLECTIONS[section] and 'characterCollections'
    if bucket and NS.S then
        NS.S[bucket] = NS.S[bucket] or {}
        NS.S[bucket][owner] = NS.S[bucket][owner] or {}
        NS.S[bucket][owner][section] = {body = body, revision = rev, rows = kept,
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
local function positiveID(value)
    local n = tonumber(value)
    return n and n > 0 and n <= 2147483647 and n == math.floor(n) and string.format('%.0f', n) or nil
end
local function companions(section, epoch)
    local kind = COLLECTIONS[section]
    local total = tonumber(GetNumCompanions(kind))
    if not total or total < 0 or total ~= math.floor(total) then
        store(section, {}, 'unavailable', 'Collection count unavailable; absence does not mean unlearned.', epoch)
        return
    end
    local rows, complete = {}, total <= MAX_ROWS
    for index = 1, math.min(total, MAX_ROWS) do
        local creature, name, spell = GetCompanionInfo(kind, index)
        local key, creatureID = positiveID(spell), positiveID(creature)
        name = clean(name)
        if key and name ~= '' then
            if rows[key] or not creatureID then complete = false end
            rows[key] = {key, name, creatureID or '0'}
        else complete = false end
        coroutine.yield()
    end
    if tonumber(GetNumCompanions(kind)) ~= total then mark(section); return end
    if not complete then
        -- Preserve observations when the client has not loaded every entry.
        for key, row in pairs(documents[section] and documents[section].rows or {}) do
            if not rows[key] then rows[key] = row end
            coroutine.yield()
        end
    end
    local detail = section == 'mounts' and 'Learned mount collection; summon spell IDs and creature IDs.'
        or 'Learned companion pets (CRITTER); summon spell IDs and creature IDs.'
    if not complete then detail = detail..' Incomplete scan; includes prior observations; absence does not mean unlearned.' end
    store(section, rows, complete and 'complete' or 'partial', detail, epoch)
end
local function petLevel(value)
    local n = tonumber(value)
    return n and n >= 1 and n <= 255 and n == math.floor(n) and tostring(n) or ''
end
local function combatpet(epoch)
    local rows, complete = {}, true
    local detail = 'Active player pet only; an absent pet may be dismissed or stabled. Other summonable combat pets are not enumerated.'
    if UnitExists('pet') then
        local name = clean(UnitName('pet'))
        local family = clean(UnitCreatureFamily and UnitCreatureFamily('pet'))
        local level = petLevel(UnitLevel('pet'))
        local hasUI, hunter = nil, nil
        if HasPetUI then hasUI, hunter = HasPetUI() end
        local kind = hasUI and (hunter and 'hunter' or 'summoned') or 'unknown'
        local talent = clean(GetPetTalentTree and GetPetTalentTree())
        complete = name ~= '' and family ~= '' and level ~= ''
        rows.active = {'active', name, family, level, talent, kind}
    end
    store('combatpet', rows, complete and 'complete' or 'partial', detail, epoch)
end
local function stablepets(epoch)
    if not stableOpen then return end
    local slots = tonumber(GetNumStableSlots())
    if not slots or slots < 0 or slots > 4 or slots ~= math.floor(slots) then
        store('stablepets', {}, 'unavailable', 'Hunter stable slot count unavailable.', epoch); return
    end
    local rows, complete = {}, true
    for slot = 0, slots do
        if not stableOpen or generation.stablepets ~= epoch then return end
        local icon, name, level, family, talent = GetStablePetInfo(slot)
        if icon or name then
            name, family, level = clean(name), clean(family), petLevel(level)
            if name == '' or family == '' or level == '' then complete = false end
            local key = tostring(slot)
            rows[key] = {key, name, family, level, clean(talent)}
        end
        coroutine.yield()
    end
    if not stableOpen or generation.stablepets ~= epoch then return end
    store('stablepets', rows, complete and 'complete' or 'partial',
        'Hunter stable observed while open; slot 0 is the current/dismissed pet; slots 1-4 are stabled. Slot 0 may duplicate the active pet.', epoch)
end
local function achievementNumber(value)
    local n = tonumber(value)
    return n and n >= 0 and n <= 9007199254740991 and n == math.floor(n) and string.format('%.0f', n) or ''
end
local function achievementFlag(value) return value and value ~= 0 and '1' or '0' end
local function achievements(epoch)
    local groups, sizes, queue, seen = {}, {}, {}, {}
    for part = 1, ACHIEVEMENT_PARTS do groups[part], sizes[part] = {}, 0 end
    local complete, criteriaRead = true, 0
    local categories = GetCategoryList()
    local ready = type(categories) == 'table' and #categories > 0
    if not ready then categories = {}; complete = false end
    local function enqueue(id, category)
        id = positiveID(id)
        if not id or seen[id] then return end
        if #queue >= 4096 then complete = false; return end
        seen[id] = true; queue[#queue+1] = {id, category}
    end
    if #categories > 256 then complete = false end
    for index = 1, math.min(#categories, 256) do
        if generation.achievements ~= epoch then return end
        local category = categories[index]
        local name = GetCategoryInfo and GetCategoryInfo(category) or tostring(category)
        -- Capture only the total: the second return is completed count, which
        -- tonumber would otherwise interpret as its optional numeric base.
        local total = GetCategoryNumAchievements(category)
        total = tonumber(total)
        if not total or total < 0 or total ~= math.floor(total) or total > 4096 then
            complete = false; total = 0
        end
        -- The API enumerates the category independent of the achievement UI's
        -- All/Completed/Incomplete filter. Previous/next tiers are added below.
        for entry = 1, total do
            local id = GetAchievementInfo(category, entry)
            if positiveID(id) then enqueue(id, clean(name)) else complete = false end
            coroutine.yield()
            if generation.achievements ~= epoch then return end
        end
    end
    if not GetPreviousAchievement or not GetNextAchievement then complete = false end
    local index = 1
    while index <= #queue do
        if generation.achievements ~= epoch then return end
        local key, category = queue[index][1], queue[index][2]
        local id, name, points, completed, month, day, year, description = GetAchievementInfo(tonumber(key))
        if positiveID(id) == key and clean(name) ~= '' then
            local count = tonumber(GetAchievementNumCriteria(id))
            local coverage, criteria = 'complete', {}
            if not count or count < 0 or count ~= math.floor(count) or count > 128 then coverage = 'partial' end
            local expected = achievementNumber(count)
            for criterion = 1, math.min(count or 0, 128) do
                if criteriaRead >= 20000 then coverage = 'partial'; break end
                criteriaRead = criteriaRead + 1
                local label, kind, done, quantity, required, _, flags, asset, quantityText = GetAchievementCriteriaInfo(id, criterion)
                label = clean(label)
                local progress, target, kindID = achievementNumber(quantity), achievementNumber(required), achievementNumber(kind)
                if label == '' or progress == '' or target == '' or kindID == '' then coverage = 'partial' end
                -- Preserve unknown values as blank, not zero/not collected.
                local state = label ~= '' and achievementFlag(done) or ''
                criteria[#criteria+1] = {tostring(criterion), label, state, progress, target,
                    kindID, achievementNumber(asset), clean(quantityText)}
                -- Meta-achievement criteria can point to otherwise hidden tiers.
                if kind == 8 and positiveID(asset) then enqueue(asset, category) end
                coroutine.yield()
                if generation.achievements ~= epoch then return end
            end
            if GetPreviousAchievement then enqueue(GetPreviousAchievement(id), category) end
            if GetNextAchievement then enqueue(GetNextAchievement(id), category) end
            local date = ''
            if completed and tonumber(year) and tonumber(month) and tonumber(day) then
                date = string.format('%04d-%02d-%02d', year < 100 and year + 2000 or year, month, day)
            end
            local pointText = achievementNumber(points)
            if pointText == '' then coverage = 'partial' end
            local row = {key, clean(name), achievementFlag(completed), pointText, clean(description, 500),
                category, date, expected, coverage, json(criteria)}
            local part = tonumber(key) % ACHIEVEMENT_PARTS + 1
            local bytes = #json(row)
            if sizes[part] + bytes <= MAX_BYTES - 2000 then
                groups[part][key] = row; sizes[part] = sizes[part] + bytes
            else complete = false end
            if coverage ~= 'complete' then complete = false end
        else complete = false end
        index = index + 1
        coroutine.yield()
    end
    -- An empty startup response cannot erase a prior character's observations.
    if #queue == 0 then complete = false; ready = false end
    for part = 1, ACHIEVEMENT_PARTS do
        if generation.achievements ~= epoch then return end
        local section = 'achievements:'..part
        local detail = 'API-listed player achievements and individual criteria; part '..part..'/'..ACHIEVEMENT_PARTS
            ..'. Criteria completion is earned credit, not current bag contents. Blank progress is unknown; hidden/unlisted achievements are not inferred.'
        if not complete then
            detail = detail..' Incomplete API data or scan limit; includes prior observations. Absence does not prove incomplete.'
            for key, row in pairs(documents[section] and documents[section].rows or {}) do
                if not groups[part][key] then
                    local previous = {}; for i, value in ipairs(row) do previous[i] = value end
                    previous[9] = 'partial'; groups[part][key] = previous
                end
                coroutine.yield()
            end
        end
        generation[section] = epoch
        store(section, groups[part], complete and 'complete' or ready and 'partial' or 'unavailable', detail, epoch)
    end
    if generation.achievements == epoch then dirty.achievements = nil; scanErrors.achievements = nil end
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
    if not GetTradeSkillSubClassFilter then reasons[#reasons+1] = 'category state unavailable'
    elseif not GetTradeSkillSubClassFilter(0) then reasons[#reasons+1] = 'category filter' end
    if not GetTradeSkillInvSlotFilter then reasons[#reasons+1] = 'slot state unavailable'
    elseif not GetTradeSkillInvSlotFilter(0) then reasons[#reasons+1] = 'slot filter' end
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
                or (achievementSection(section) and (dirty.achievements or scanErrors.achievements))
            local freshness = stale and 'stale' or doc.session ~= NS.session and 'cached' or 'current'
            if not stale and isRecipe and recipeIdentity() ~= section then freshness = 'cached' end
            if not stale and section == 'stablepets' and not stableOpen then freshness = 'cached' end
            fields[#fields+1] = {'snapshot', section..'|'..doc.revision..'|'..doc.seen..'|'..freshness}
            bundle.docs[section] = doc
        end
    end
    fields[#fields+1] = {'ctx', 'Gear/bags/pets/mounts are event-cached observations. Unlisted recipe professions are unscanned; open each profession to scan. Bank/mail/keyring are not included. Combat pet is the active pet only; an unopened hunter stable is unscanned.'}
    fields[#fields+1] = {'ctx', 'Snapshot labels: coverage and freshness are separate. Complete + cached means a complete scan from when that profession was last open, NOT a filtered scan. Active recipe filters, if any, are recorded in that snapshot detail. Older files can describe a different filter state.'}
    if not GetNumCompanions or not GetCompanionInfo then
        fields[#fields+1] = {'ctx', 'Mount and companion-pet collection APIs unavailable; do not infer an empty collection.'}
    end
    if not UnitExists then fields[#fields+1] = {'ctx', 'Active combat-pet API unavailable; do not infer no pet.'} end
    if not achievementAPIs() then fields[#fields+1] = {'ctx', 'Achievement APIs unavailable; achievement progress is unknown.'} end
    if dirty.gear or dirty.bags then fields[#fields+1] = {'ctx', 'Inventory scan pending; cached inventory may be stale. Send again after the scan finishes.'} end
    for section in pairs(scanErrors) do fields[#fields+1] = {'ctx', section..' scan failed; do not treat cached data as current.'} end
    return bundle
end
function NS.RememberCharacter(request, bundle) requests[request] = bundle end
function NS.CharacterConfirmed(request)
    local bundle = requests[request]
    if bundle then
        for section, doc in pairs(bundle.docs) do rememberSynced(bundle.owner, section, doc) end
    end
end
function NS.CharacterAck(request) requests[request] = nil end
function NS.CharacterSyncBusy() return manual ~= nil end
local function finishManual(message)
    if manual and manual.request then requests[manual.request] = nil end
    manual = nil
    if NS.SyncButton then NS.SyncButton:SetText('Sync'); NS.SyncButton:Enable() end
    NS.SetStatus(message); NS.Print(message)
end
local function failSync(request, reason)
    if manual and manual.request == request then
        finishManual('Character sync failed: '..reason..' Click Sync to retry.'); return
    end
    requests[request] = nil
    NS.AckPrompt(request); NS.ForgetRequest(request)
    NS.ShowReply(request, 'Character sync failed: '..reason..' Send the prompt again to retry.', 5, true)
    NS.SetStatus('Character sync failed. The agent was not started; send again to retry.')
end
local function uploadBody(job)
    local doc, section = job.doc, job.section
    if job.full then return doc.body end
    local baseline = job.owner == owner and synced[section]
    if baseline and baseline.revision == doc.revision then return doc.body end
    if baseline and baseline.revision ~= doc.revision then
        local hashes, changes, removed = rowHashes(doc), {}, {}
        for _, key in ipairs(keys(doc.rows)) do
            if hashes[key] ~= baseline.hashes[key] then changes[#changes+1] = doc.rows[key] end
            coroutine.yield()
        end
        for _, key in ipairs(keys(baseline.hashes)) do
            if not hashes[key] then removed[#removed+1] = key end
            coroutine.yield()
        end
        local patch = json({2, section, doc.status, doc.detail, baseline.revision, changes, removed})
        if #patch < #doc.body then
            job.usedPatch, job.baseRevision = true, baseline.revision
            return patch
        end
        return doc.body
    end
    -- Older installations may not yet have an acknowledged baseline.
    job.usedPatch = doc.patch ~= nil
    return doc.patch or doc.body
end
function NS.CharacterNeed(request, text)
    if text:sub(1, 8) ~= '\1ABCTX1\n' then return false end
    local bundle = requests[request]
    if not bundle or not NS.S.context then return true end
    bundle.queued = bundle.queued or {}
    local added = false
    for line in text:sub(9):gmatch('[^\n]+') do
        local section, rev = line:match('^([^|]+)|([%x]+%-%d+)$')
        local doc = section and bundle.docs[section]
        if doc and doc.revision == rev and not bundle.queued[section] then
            bundle.queued[section] = true
            uploads[#uploads+1] = {parent = request, section = section, doc = doc, owner = bundle.owner}
            added = true
        end
    end
    -- The companion already has this prompt. Give snapshot pages the strip,
    -- then repeat the prompt after all its uploads so only a fresh send runs it.
    if added then NS.HoldPrompt(request, true) end
    NS.ShowReply(request, 'Syncing character snapshots with the companion...', 0, true)
    return true
end
local function resumeParent(request)
    if not requests[request] then return end
    if active and active.parent == request then return end
    for _, job in ipairs(uploads) do if job.parent == request then return end end
    if manual and manual.request == request then
        local count = manual.count
        finishManual('Character sync complete: '..count..' snapshots uploaded. Send your question to use them.')
        for line in NS.CharacterStatus():gmatch('[^\n]+') do NS.Print('  '..NS.Escape(line)) end
        return
    end
    NS.HoldPrompt(request, false)
end
local function uploadStep()
    if active then
        if active.prepare then
            local job = active
            local done, error = advance(job.prepare)
            if done == false then active = nil; failSync(job.parent, 'Could not prepare snapshot: '..clean(error, 160))
            elseif done then job.prepare = nil; job.start() end
        end
        return
    end
    local job = table.remove(uploads, 1)
    if not job then return end
    if not requests[job.parent] or not NS.S.context then return end
    active = job
    local pages = {}
    local function sendPage(index)
        if not requests[job.parent] then active = nil; return end
        NS.StripTransferProgress(job.parent)
        local seq = NS.NextRequest(); job.request = seq
        local fields = {{'character', job.owner}, {'section', job.section}, {'revision', job.doc.revision},
                        {'page', index}, {'total', #pages}}
        if job.probe then fields[#fields+1] = {'probe', '1'} end
        -- Leave room for several retransmissions when capture misses fragments.
        local ok = NS.BeginRequest(seq, nil, function(reply, state)
            if not requests[job.parent] then active = nil; return end
            NS.StripTransferProgress(job.parent)
            if state ~= 4 or reply ~= 'ABCTX_OK' then
                active = nil
                if reply == 'ABCTX_BASE_MISSING' and job.probe then
                    job.probe = nil; table.insert(uploads, 1, job)
                elseif reply == 'ABCTX_BASE_MISSING' and not job.full then
                    forgetSynced(job.owner, job.section, job.baseRevision)
                    job.full = true; table.insert(uploads, 1, job)
                elseif state == 5 and job.usedPatch and not job.full and reply:find('Character data rejected:', 1, true) == 1 then
                    forgetSynced(job.owner, job.section, job.baseRevision)
                    job.full = true; table.insert(uploads, 1, job)
                else failSync(job.parent, reply) end
            elseif index < #pages then sendPage(index+1)
            else rememberSynced(job.owner, job.section, job.doc); active = nil end
            resumeParent(job.parent)
        end, 240)
        if not ok then active = nil; failSync(job.parent, 'Reply channel unavailable.'); return end
        NS.QueuePrompt(seq, NS.EncodeCharacter(NS.Envelope(fields, pages[index]), NS.session, seq), true)
        NS.SetStatus('Syncing '..job.section..' ('..index..'/'..#pages..')...')
    end
    job.start = function() sendPage(1) end
    job.prepare = coroutine.create(function()
        rowHashes(job.doc)
        job.usedPatch, job.baseRevision = nil, nil
        local payload = job.probe and 'ABCTX_PROBE' or uploadBody(job)
        local pos = 1
        while pos <= #payload do
            local chunk = NS.TrimUTF8(payload:sub(pos, pos+4999))
            pages[#pages+1] = chunk; pos = pos + #chunk
            coroutine.yield()
        end
    end)
end
function NS.CharacterContextOff()
    for request in pairs(requests) do failSync(request, 'Context sharing was turned off.') end
    if manual then finishManual('Character sync cancelled: context sharing was turned off.') end
    if active and active.request then NS.AckPrompt(active.request); NS.ForgetRequest(active.request) end
    active = nil
    uploads, tasks = {}, {}
end
function NS.CharacterStatus()
    local out = {}
    for _, section in ipairs(keys(documents)) do
        local doc = documents[section]
        local isRecipe = section:sub(1, 8) == 'recipes:'
        local stale = dirty[section] or scanErrors[section] or (isRecipe and scanErrors.recipes)
            or (achievementSection(section) and (dirty.achievements or scanErrors.achievements))
        local freshness = stale and 'stale' or doc.session ~= NS.session and 'cached' or 'current'
        if not stale and isRecipe and recipeIdentity() ~= section then freshness = 'cached' end
        if not stale and section == 'stablepets' and not stableOpen then freshness = 'cached' end
        out[#out+1] = section..': '..doc.count..' records, '..doc.status..', '..freshness..', scanned '
            ..math.max(0, time()-doc.seen)..'s ago. '..doc.detail
    end
    for section in pairs(scanErrors) do out[#out+1] = section..': scan failed; cached data may be stale.' end
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
    synced, confirmations, confirming = {}, {}, nil
    local savedSync = type(NS.S.characterSynced) == 'table' and NS.S.characterSynced[owner]
    if type(savedSync) == 'table' then
        local sections = 0
        for section, baseline in pairs(savedSync) do
            sections = sections + 1; if sections > 32 then break end
            local allowed = type(section) == 'string' and #section <= 120 and
                (section == 'gear' or section == 'bags' or section == 'combatpet' or SAVED_COLLECTIONS[section]
                    or achievementSection(section) or section:sub(1, 8) == 'recipes:')
            if allowed and type(baseline) == 'table' and type(baseline.revision) == 'string'
                and #baseline.revision <= 32 and baseline.revision:match('^%x+%-%d+$')
                and type(baseline.hashes) == 'table' then
                local hashes, count, bytes, valid = {}, 0, 0, true
                for key, value in pairs(baseline.hashes) do
                    count = count + 1
                    if count > MAX_ROWS or type(key) ~= 'string' or #key > 512 or type(value) ~= 'string'
                        or #value > 32 or not value:match('^%x+%-%d+$') then valid = false; break end
                    bytes = bytes + #key + #value
                    if bytes > MAX_BYTES then valid = false; break end
                    hashes[key] = value
                end
                if valid then synced[section] = {revision = baseline.revision, hashes = hashes} end
            end
        end
    end
    stableOpen = false
    for _, bucket in ipairs({'characterRecipes', 'characterCollections', 'characterAchievements'}) do
        local saved = type(NS.S[bucket]) == 'table' and NS.S[bucket][owner]
        if type(saved) == 'table' then
            for section, doc in pairs(saved) do
                local allowed = type(section) == 'string' and (bucket == 'characterRecipes' and section:sub(1, 8) == 'recipes:'
                    or bucket == 'characterCollections' and SAVED_COLLECTIONS[section]
                    or bucket == 'characterAchievements' and achievementSection(section))
                if allowed and type(doc) == 'table'
                    and type(doc.body) == 'string' and #doc.body <= MAX_BYTES and type(doc.rows) == 'table'
                    and type(doc.revision) == 'string' and type(doc.seen) == 'number'
                    and type(doc.count) == 'number' and type(doc.status) == 'string' and type(doc.detail) == 'string' then
                    documents[section] = doc
                end
            end
        end
    end
    mark('gear'); mark('bags'); mark('mounts'); mark('pets'); mark('combatpet'); mark('achievements')
end
local driver = CreateFrame('Frame')
for _, event in ipairs({'PLAYER_ENTERING_WORLD', 'PLAYER_EQUIPMENT_CHANGED', 'UNIT_INVENTORY_CHANGED', 'BAG_UPDATE',
    'GET_ITEM_INFO_RECEIVED', 'TRADE_SKILL_SHOW', 'TRADE_SKILL_UPDATE', 'TRADE_SKILL_FILTER_UPDATE',
    'TRADE_SKILL_CLOSE', 'SKILL_LINES_CHANGED', 'COMPANION_LEARNED', 'COMPANION_UNLEARNED', 'COMPANION_UPDATE',
    'UNIT_PET', 'UNIT_NAME_UPDATE', 'UNIT_LEVEL', 'PET_UI_UPDATE', 'PET_UI_CLOSE', 'PET_TALENT_UPDATE',
    'PET_STABLE_SHOW', 'PET_STABLE_UPDATE', 'PET_STABLE_UPDATE_PAPERDOLL', 'PET_STABLE_CLOSED',
    'ACHIEVEMENT_EARNED', 'CRITERIA_UPDATE', 'RECEIVED_ACHIEVEMENT_LIST'}) do pcall(driver.RegisterEvent, driver, event) end
driver:SetScript('OnEvent', function(_, event, unit)
    if event == 'PLAYER_ENTERING_WORLD' then loadOwner() end
    if event == 'ACHIEVEMENT_EARNED' or event == 'CRITERIA_UPDATE' or event == 'RECEIVED_ACHIEVEMENT_LIST' then
        mark('achievements'); return
    end
    if event == 'UNIT_INVENTORY_CHANGED' and unit ~= 'player' then return end
    if event:find('COMPANION_') == 1 then
        if unit == 'MOUNT' then mark('mounts')
        elseif unit == 'CRITTER' then mark('pets')
        else mark('mounts'); mark('pets') end
        return
    end
    if event:find('PET_STABLE_') == 1 then
        if event == 'PET_STABLE_CLOSED' then
            stableOpen = false; generation.stablepets = (generation.stablepets or 0) + 1; tasks.stablepets = nil
        else
            if event == 'PET_STABLE_SHOW' then stableOpen = true end
            if stableOpen then mark('stablepets') end
        end
        return
    end
    if event == 'UNIT_PET' or event == 'UNIT_NAME_UPDATE' or event == 'UNIT_LEVEL' or event:find('PET_') == 1 then
        if event == 'UNIT_PET' and unit ~= 'player' then return end
        if (event == 'UNIT_NAME_UPDATE' or event == 'UNIT_LEVEL') and unit ~= 'pet' then return end
        mark('combatpet')
        if stableOpen then mark('stablepets') end
        return
    end
    if event == 'TRADE_SKILL_CLOSE' then recipeOpen = false; recipeEpoch = recipeEpoch + 1
    elseif event:find('TRADE_SKILL') then
        if event == 'TRADE_SKILL_SHOW' then recipeOpen = true end
        recipeEpoch = recipeEpoch + 1; dirty.recipes = GetTime() + .5
        local section = recipeIdentity()
        if section then dirty[section] = GetTime() end
    elseif event == 'BAG_UPDATE' then mark('bags')
    else
        mark('gear'); mark('bags')
        if event == 'PLAYER_ENTERING_WORLD' then mark('mounts'); mark('pets'); mark('combatpet'); mark('achievements') end
        if event == 'SKILL_LINES_CHANGED' then
            for section in pairs(documents) do if section:sub(1, 8) == 'recipes:' then dirty[section] = GetTime() end end
            recipeEpoch = recipeEpoch + 1; dirty.recipes = GetTime() + .5
        end
    end
end)
local scanners = {gear = gear, bags = bags, recipes = recipes, achievements = achievements,
    mounts = function(epoch) companions('mounts', epoch) end,
    pets = function(epoch) companions('pets', epoch) end, combatpet = combatpet, stablepets = stablepets}
local function scannerAvailable(section)
    return section == 'gear' and GetInventoryItemLink or section == 'bags' and GetContainerNumSlots
        or section == 'recipes' and recipeIdentity()
        or section == 'achievements' and achievementAPIs()
        or COLLECTIONS[section] and GetNumCompanions and GetCompanionInfo
        or section == 'combatpet' and UnitExists
        or section == 'stablepets' and stableOpen and GetNumStableSlots and GetStablePetInfo
end
function NS.SyncCharacter()
    if not NS.S or not NS.S.context then return false, 'Context sharing is off. Use /ab context on before syncing.' end
    if not owner then return false, 'Character is still loading. Try Sync again in a moment.' end
    if manual or active or #uploads > 0 or next(requests) then
        return false, 'Character sync is already pending. Wait for it to finish before syncing again.'
    end
    local scanWait = achievementAPIs() and 120 or 60
    manual = {targets = {}, deadline = GetTime() + scanWait, scanWait = scanWait}
    for section in pairs(scanners) do
        if scannerAvailable(section) then
            local target = section == 'recipes' and recipeIdentity() or section
            manual.targets[section] = target
            tasks[section] = nil
            if section == 'recipes' then
                recipeEpoch = recipeEpoch + 1; dirty.recipes = GetTime() + .5; dirty[target] = GetTime()
            else mark(section) end
        end
    end
    if NS.SyncButton then NS.SyncButton:SetText('Syncing...'); NS.SyncButton:Disable() end
    local message = 'Scanning character data for sync...'
    if InCombatLockdown and InCombatLockdown() then message = 'Character sync queued; scanning will start after combat.' end
    NS.SetStatus(message)
    NS.Print('Sync includes gear, bags, mounts, pets, achievements with criteria, and saved recipes. Keep your own profession open to refresh its recipes; other professions stay cached. Hunter stable pets refresh only while the stable is open.')
    return true, message
end
local function manualStep()
    if not manual or manual.request then return end
    local pending = false
    for section, target in pairs(manual.targets) do
        if not scannerAvailable(section) or (section == 'recipes' and recipeIdentity() ~= target) then
            finishManual('Character sync stopped: '..target..' closed or changed during scanning. Keep it open and click Sync again.'); return
        end
        if scanErrors[section] and not dirty[section] and not tasks[section] then
            finishManual('Character sync stopped: '..section..' scan failed. Click Sync to retry.'); return
        end
        if dirty[section] or dirty[target] or tasks[section] or (section ~= 'achievements' and not documents[target]) then pending = true end
    end
    if pending then
        if GetTime() >= manual.deadline then
            finishManual('Character scan did not settle. Keep the profession open and click Sync again.'); return
        end
        return
    end
    local bundle = NS.CharacterFields({})
    local sections = keys(bundle.docs)
    if #sections == 0 then finishManual('No character snapshots are available to sync yet.'); return end
    -- A local parent groups ordinary character pages; it is never a chat prompt
    -- and never launches an agent. Probe saved/cached revisions first, then use
    -- patches or full bodies when the companion is missing that revision.
    local request = NS.NextRequest()
    manual.request, manual.count = request, #sections
    requests[request] = bundle
    for _, section in ipairs(sections) do
        uploads[#uploads+1] = {parent = request, section = section, doc = bundle.docs[section], owner = bundle.owner, probe = true}
    end
    NS.SetStatus('Uploading '..#sections..' character snapshots...')
end
local function work()
    if not NS.S or not owner then return end
    confirmStep()
    if not NS.S.context then return end
    uploadStep()
    if InCombatLockdown and InCombatLockdown() then
        if manual and not manual.request then manual.deadline = GetTime() + manual.scanWait end
        return
    end
    local now = GetTime()
    for section, fn in pairs(scanners) do
        local available = scannerAvailable(section)
        if available and dirty[section] and now >= dirty[section] and not tasks[section] then
            local epoch = section == 'recipes' and recipeEpoch or generation[section]
            tasks[section] = coroutine.create(function() fn(epoch) end)
            if section == 'recipes' then dirty.recipes = nil end
        end
        local task = tasks[section]
        if task then
            local started = debugprofilestop and debugprofilestop()
            for _ = 1, section == 'achievements' and 32 or 8 do
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
    manualStep()
end
driver:SetScript('OnUpdate', function() NS.Profile('character-context', work) end)
NS.OnLoad(function(S)
    loadOwner()
end)
