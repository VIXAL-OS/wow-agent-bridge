-- Inbound channel: checked reply packets encoded as glyph advance widths in
-- first-use font files. Lua only calls SetFont/SetText/GetStringWidth; the
-- decoded bytes are validated data that is displayed, never executed.
local NS = AgentBridge
local SIZE, PACKET = NS.BANK_SIZE, NS.REPLY_SIZE
-- Two glyphs per byte: 3.3.5a caps the rasterised em, so a whole byte per
-- glyph would put byte values half a pixel apart. Four bits per glyph puts
-- them about four pixels apart instead.
local GLYPHS, VALUES = PACKET * 2, 16
local FIRST_WINDOW, FRAGMENT_WINDOW, POLL_WINDOW, MAX_POLL = 3, 1.5, 2, 8
local READ_TIMEOUT, WATCH_LIMIT = 15, 3600
-- Measuring is spread over frames by time, not by a fixed count, so a fast
-- machine finishes a packet sooner and a slow one never loses frame rate.
local FRAME_BUDGET, MIN_PER_FRAME, MAX_PER_FRAME = 3, 32, 512
local profile = type(debugprofilestart) == 'function' and type(debugprofilestop) == 'function'
local SELFTEST = NS.PATH..'selftest.ttf'
local TEST_SIZES = {64, 96, 128, 48, 32}

-- Measure on a parentless frame at scale 1 so UI scale never changes results.
local host = CreateFrame('Frame', 'AgentBridgeMeter')
host:SetScale(1); NS.Size(host, 1, 1); host:SetPoint('BOTTOMLEFT', UIParent, 'BOTTOMLEFT', 0, 0); host:SetAlpha(0)
local function newMeter()
    local fs = host:CreateFontString(nil, 'ARTWORK')
    fs:SetPoint('BOTTOMLEFT', host, 'BOTTOMLEFT', 0, 0)
    if fs.SetWordWrap then fs:SetWordWrap(false) end
    if fs.SetNonSpaceWrap then fs:SetNonSpaceWrap(false) end
    return fs
end
local meter, tester = newMeter(), newMeter()
local GLYPH = {}
for i = 0, GLYPHS-1 do
    local c = 0xE000 + i
    GLYPH[i] = string.char(224 + math.floor(c/4096), 128 + math.floor(c/64) % 64, 128 + c % 64)
end
-- The common trailing glyph makes the measured glyph's full advance count.
local function width(fs, text) fs:SetText(text..'~'); return fs:GetStringWidth() end
-- The client may normalise the path it reports, so compare file names. A wrong
-- font still cannot slip through: calibration widths and the slot number inside
-- each packet are both checked.
local function assign(fs, path, size)
    local ok, result = pcall(fs.SetFont, fs, path, size)
    pcall(width, fs, '!')
    local current = fs:GetFont()
    local file = path:match('([^\\/]+)$'):lower()
    local matches = type(current) == 'string' and current:lower():sub(-#file) == file
    return ok and (result or matches), 'assignment='..tostring(ok and result)..', font='..tostring(current)
end

---------------------------------------------------------------------------
-- Self-test: selftest.ttf holds every byte value in known positions. Record
-- the exact width of each value at this renderer's scale, then check that the
-- remaining positions decode by nearest-width lookup. This is independent of
-- how (or whether) the client rounds advances to pixels.
---------------------------------------------------------------------------
local calib, test = nil, nil
-- Mirrors selftest_byte() in companion/native.py.
local function selftestByte(j)
    if j < 8 then return 34*j + 1 end
    return (37*j + 11) % 256
end
local function expected(i)
    local byte = selftestByte(math.floor(i/2))
    if i % 2 == 0 then return math.floor(byte/16) end
    return byte % 16
end
local function lookup(c, w)
    local ws, best, distance = c.widths, nil, c.tol
    for v = 0, VALUES-1 do
        local d = math.abs(ws[v] - w)
        if d <= distance then best, distance = v, d end
    end
    return best
end
local function evaluate(size, measured)
    local widths, gap = {}, math.huge
    for v = 0, VALUES-1 do widths[v] = measured[v] end
    for v = 1, VALUES-1 do gap = math.min(gap, widths[v] - widths[v-1]) end
    local result = {size = size, gap = gap, low = widths[0], high = widths[VALUES-1], errors = 0, residual = 0}
    if not (gap > 0) then result.errors = GLYPHS; return result end
    local c = {size = size, widths = widths, tol = gap/3}
    for i = VALUES, GLYPHS-1 do
        local v = lookup(c, measured[i])
        if v ~= expected(i) then result.errors = result.errors + 1
        else result.residual = math.max(result.residual, math.abs(widths[v] - measured[i])) end
    end
    result.calib = c
    return result
end

function NS.RunSelfTest(verbose)
    if test then test.verbose = test.verbose or verbose; return end
    test = {index = 1, verbose = verbose, results = {}, nextTry = 0, tries = 0}
    if verbose then NS.Print('Running font self-test...') end
end
local function report(t)
    for _, r in ipairs(t.results) do
        if r.failed then
            NS.Print(string.format('  size %d: font did not load (%s)', r.size, r.failed))
        else
            NS.Print(string.format('  size %d: %s  min gap %.3f (~%.2f px), %d/%d mismatches, residual %.4f, widths %.2f..%.2f',
                r.size, r.errors == 0 and '|cff55ff77PASS|r' or '|cffff5555FAIL|r', r.gap, r.gap * NS.PixelsPerUnit(),
                r.errors, GLYPHS-VALUES, r.residual, r.low or 0, r.high or 0))
        end
    end
end
local function finishTest()
    local chosen
    for _, r in ipairs(test.results) do
        if not chosen and r.calib and r.errors == 0 then chosen = r end
    end
    local verbose = test.verbose
    if verbose or not chosen then
        NS.Print(chosen and ('Font channel OK at size '..chosen.size..'.') or
            '|cffff5555Font self-test failed: replies cannot be decoded on this client.|r')
        report(test)
    end
    calib = chosen and chosen.calib or nil
    NS.selfTest = {ok = chosen ~= nil, size = chosen and chosen.size, results = test.results}
    test = nil
end
local function stepTest(now)
    local size = TEST_SIZES[test.index]
    if not size then finishTest(); return end
    if not test.ready then
        if now < test.nextTry then return end
        local ok, why = assign(tester, SELFTEST, size)
        test.tries = test.tries + 1
        if not ok then
            test.nextTry = now + .5
            if test.tries >= 6 then
                test.results[#test.results+1] = {size = size, failed = why}
                test.index, test.tries, test.nextTry = test.index + 1, 0, 0
            end
            return
        end
        test.ready, test.measured, test.i = true, {}, 0
    end
    if profile then debugprofilestart() end
    for measured = 1, MAX_PER_FRAME do
        if measured > MIN_PER_FRAME and profile and debugprofilestop() > FRAME_BUDGET then return end
        test.measured[test.i] = width(tester, GLYPH[test.i])
        test.i = test.i + 1
        if test.i == GLYPHS then
            local r = evaluate(size, test.measured)
            test.results[#test.results+1] = r
            test.index, test.ready, test.tries = test.index + 1, false, 0
            -- Stop at the first passing size unless a full report was asked for.
            if r.errors == 0 and not test.verbose then finishTest() end
            return
        end
    end
end

---------------------------------------------------------------------------
-- Receiver: one unused bank slot per attempt. A slot is reserved in saved
-- settings before its first load and is never loaded twice in one process.
---------------------------------------------------------------------------
local slot, request, session = 1, 0, nil
local active, deadline, watchUntil = false, 0, 0
local reading
local failures, missed, unchanged, lastRevision = 0, 0, 0, nil
local assembly = NS.NewAssembly()

function NS.IsReceiving() return active end
local function exhausted()
    NS.SetStatus('Reply channel used up for this game session. Restart WoW while the companion runs to recycle it.')
end
local function watch()
    if slot > SIZE then exhausted(); return end
    if not active then deadline = GetTime() + FIRST_WINDOW end
    active, watchUntil = true, GetTime() + WATCH_LIMIT
    NS.OnReceiveState(true)
end
local function consume() slot = slot + 1; reading = nil end

function NS.Pause()
    if reading then consume() end
    active = false; NS.OnReceiveState(false)
end
function NS.Resume()
    if request > 0 then failures, missed = 0, 0; watch(); NS.SetStatus('Checking for replies...') end
end
function NS.BeginRequest(sequence)
    if reading then consume() end
    request, session = sequence, NS.session
    assembly = NS.NewAssembly()
    failures, missed, unchanged, lastRevision = 0, 0, 0, nil
    watch()
end
function NS.ControlFrame()
    local remaining = 0
    if active and not reading then
        remaining = math.max(0, math.min(30000, math.floor((deadline - GetTime())*1000)))
    end
    return NS.EncodeControl(session or NS.session, math.min(slot, SIZE + 1), remaining,
        assembly.nextPart, active and slot <= SIZE and request > 0, request)
end
function NS.ReceiverInfo()
    return {slot = slot, request = request, active = active, calib = calib and calib.size, reading = reading ~= nil}
end

local function finish(reason, text, state, complete)
    consume()
    local now = GetTime()
    if reason == 'Empty reply slot' then
        -- Nobody wrote this slot before it loaded (companion stopped, strip hidden).
        missed, failures = missed + 1, 0
        deadline = now + math.min(30, 5 * 2^math.min(missed, 3))
        NS.SetStatus('Waiting for the companion. Is it running and capturing the strip? Retrying...')
        return
    end
    if reason == 'Unchanged' then
        -- Nothing new since the last slot: poll again, a little less eagerly.
        failures, missed = 0, 0
        unchanged = unchanged + 1
        deadline = now + math.min(MAX_POLL, POLL_WINDOW + unchanged * 1.5)
        return
    end
    if reason == 'Stale reply packet' then
        failures = 0; deadline = now + FRAGMENT_WINDOW
        return
    end
    if reason then
        failures = failures + 1
        if failures >= 3 then
            NS.Pause()
            NS.SetStatus('Reply reception paused after repeated errors ('..reason..'). The reply is safe in the companion; Resume to retry.')
            return
        end
        deadline = now + POLL_WINDOW
        NS.SetStatus('Retrying reply reception: '..reason)
        return
    end
    failures, missed = 0, 0
    if not text then deadline = now + FRAGMENT_WINDOW; return end
    if state >= 1 then NS.AckPrompt(request) end
    if state == 3 and not complete then
        -- While the agent is still writing, preview the first part only; the
        -- full text is fetched once it is final. This bounds slot usage.
        assembly.nextPart, assembly.parts = 1, {}
        NS.ShowReply(NS.TrimUTF8(text), state, false)
        deadline = now + 4
        return
    end
    NS.ShowReply(complete and text or NS.TrimUTF8(text), state, complete)
    if not complete then deadline = now + FRAGMENT_WINDOW; return end
    if state >= 4 then NS.Pause(); NS.OnReplyFinished(state); return end
    unchanged = assembly.revision == lastRevision and unchanged + 1 or 0
    lastRevision = assembly.revision
    deadline = now + (state == 3 and 4 or math.min(MAX_POLL, POLL_WINDOW + unchanged * 2.5))
end

local function stepReceiver(now)
    if now >= watchUntil then
        NS.Pause(); NS.SetStatus('Stopped checking after an hour. Resume to check again.')
        return
    end
    if not reading then
        if now < deadline then return end
        if slot > SIZE then NS.Pause(); exhausted(); return end
        -- Reserve before the first load: /reload must never reuse a cached path.
        NS.S.nextSlot = math.max(NS.S.nextSlot or 1, slot + 1)
        reading = {start = now, nextTry = 0, bytes = {}, index = 0}
    end
    if now - reading.start > READ_TIMEOUT then
        finish('Slot '..slot..': '..(reading.reason or 'font did not load'))
        return
    end
    if not reading.ready then
        if now < reading.nextTry then return end
        reading.nextTry = now + 1
        local ok, why = assign(meter, NS.SlotPath(slot), calib.size)
        reading.reason = why
        if not ok then return end
        local low, high = width(meter, '!'), width(meter, '"')
        if math.abs(low - calib.widths[0]) > calib.tol or math.abs(high - calib.widths[VALUES-1]) > calib.tol then
            if reading.recalibrated then finish('Calibration mismatch'); return end
            -- Rendering scale changed since the self-test (window resized?).
            reading.recalibrated, reading.start, reading.nextTry = true, now, 0
            calib = nil; NS.RunSelfTest(false)
            return
        end
        reading.ready = true
    end
    if profile then debugprofilestart() end
    for measured = 1, MAX_PER_FRAME do
        if measured > MIN_PER_FRAME and profile and debugprofilestop() > FRAME_BUDGET then return end
        local v = lookup(calib, width(meter, GLYPH[reading.index]))
        if not v then finish('Font byte measurement'); return end
        if reading.index % 2 == 0 then
            reading.high = v
        else
            reading.bytes[#reading.bytes+1] = string.char(reading.high*16 + v)
        end
        reading.index = reading.index + 1
        -- Peek at the header before paying for the rest of the packet.
        if reading.index == 64 then
            local info, why = NS.PeekReply(table.concat(reading.bytes), session, request, slot)
            if not info then finish(why); return end
            if info.part == 1 and assembly.nextPart == 1 and info.revision == lastRevision then
                finish('Unchanged'); return
            end
        end
        if reading.index == GLYPHS then
            local packet, why = NS.ParseReply(table.concat(reading.bytes), session, request, slot)
            if not packet then finish(why); return end
            local text, state, complete = NS.AcceptFragment(assembly, packet)
            if text == nil then finish(state); return end
            finish(nil, text, state, complete)
            return
        end
    end
end

local driver = CreateFrame('Frame')
driver:RegisterEvent('PLAYER_ENTERING_WORLD')
driver:SetScript('OnEvent', function(self)
    self:UnregisterEvent('PLAYER_ENTERING_WORLD')
    NS.RunSelfTest(false)
end)
driver:SetScript('OnUpdate', function()
    local now = GetTime()
    if test then stepTest(now) end
    if not (active and session and NS.S) then return end
    if calib then stepReceiver(now)
    elseif not test and NS.selfTest and not NS.selfTest.ok then
        NS.Pause()
        NS.SetStatus('Font self-test failed, so replies cannot be decoded here. Type /ab test for details.')
    end
end)
NS.OnLoad(function(S) slot = S.nextSlot end)
