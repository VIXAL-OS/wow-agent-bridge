-- Opt-in timings; never reset WoW's shared profiling clock.
local NS = AgentBridge
local available = type(debugprofilestop) == 'function'
local enabled, samples, frames, worst, slow = false, {}, 0, 0, 0
function NS.Profile(label, fn, ...)
    if not enabled or not available then return fn(...) end
    local start = debugprofilestop()
    local a, b, c, d = fn(...)
    local ms = debugprofilestop() - start
    if ms >= 0 then
        local s = samples[label] or {n = 0, total = 0, max = 0}
        s.n, s.total, s.max = s.n + 1, s.total + ms, math.max(s.max, ms)
        samples[label] = s
    end
    return a, b, c, d
end
function NS.PerfCommand(mode)
    if mode == 'on' or mode == 'reset' then
        samples, frames, worst, slow, enabled = {}, 0, 0, 0, true
        NS.Print('Performance recording on. Reproduce the dip, then /ab perf; /ab perf off stops it.')
        if not available then NS.Print('Client profiling clock unavailable; only frame intervals can be recorded.') end
        return
    elseif mode == 'off' then enabled = false end
    NS.Print(string.format('Performance %s: %d frames, worst interval %.1f ms, %d over 33 ms.',
        enabled and 'on' or 'off', frames, worst, slow))
    local names = {}
    for label in pairs(samples) do names[#names+1] = label end
    table.sort(names)
    for _, label in ipairs(names) do
        local s = samples[label]
        NS.Print(string.format('  %s: %d calls, average %.2f ms, max %.2f ms', label, s.n, s.total/s.n, s.max))
    end
    NS.Print('Hybrid: '..NS.HybridInfo()..'. Timings cover addon work; frame intervals include the whole game.')
end
local driver = CreateFrame('Frame')
driver:SetScript('OnUpdate', function(_, dt)
    if not enabled then return end
    local ms = dt*1000
    frames, worst = frames + 1, math.max(worst, ms)
    if ms > 33 then slow = slow + 1 end
end)
