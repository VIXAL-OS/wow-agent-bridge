-- Sending prompts, completion alerts and slash commands.
local NS = AgentBridge
local edit = NS.Input
local sequence = 0

local function submit()
    local text = edit:GetText()
    if not text:find('%S') then return end
    text = NS.MakePromptText(text)
    if #text > NS.MAX_PROMPT then
        NS.SetStatus('Message plus link details is too long ('..#text..'/'..NS.MAX_PROMPT..' bytes). Shorten it.')
        return
    end
    sequence = sequence + 1
    NS.currentPrompt = text
    NS.QueuePrompt(sequence, NS.EncodePrompt(text, NS.session, sequence))
    NS.ClearReply()
    NS.BeginRequest(sequence)
    NS.SetBadge(nil)
    NS.StartExchange(text)
    NS.SetStatus('Prompt #'..sequence..' sent. You can close this panel and keep playing.')
    edit:SetText(''); edit:ClearFocus()
end
edit:SetScript('OnEnterPressed', submit)
NS.SendButton:SetScript('OnClick', submit)

function NS.NewChat()
    NS.Pause(); NS.ClearPrompts(); NS.ClearReply()
    -- Always a new id, even when clicked twice within the same second.
    NS.S.conversation = math.max(time(), (NS.S.conversation or 0) + 1); NS.S.last = nil
    NS.NewSession()
    NS.currentPrompt = nil
    NS.RefreshTranscript('last')
    NS.SetStatus('New conversation. The agent will not see earlier messages.')
end

function NS.OnReplyFinished(state)
    NS.SetStatus(state == 4 and 'Reply ready.' or 'The request finished with a problem; details below.')
    if NS.Panel:IsShown() then return end
    NS.SetBadge(state)
    NS.Print(state == 4 and 'reply ready. Type /ab or click the minimap button to read it.'
        or 'the request finished with a problem. Type /ab for details.')
    if NS.S.sound then PlaySound('TellMessage') end
end

local HELP = {
    '/ab - show or hide the panel (also /agent, /claude, /codex)',
    '/ab new - start a new conversation',
    '/ab pause | resume - stop or restart reply checks',
    '/ab test - run the font self-test and print results',
    '/ab status - show channel state',
    '/ab strip top|topleft|topright|bottom|bottomleft|bottomright - move the pixel strip',
    '/ab alpha 0.5 - strip opacity, so you can see the UI behind it (0.2 to 1)',
    '/ab sound on|off - chime when a reply is ready',
}
SLASH_AGENTBRIDGE1, SLASH_AGENTBRIDGE2, SLASH_AGENTBRIDGE3, SLASH_AGENTBRIDGE4 = '/ab', '/agent', '/claude', '/codex'
SlashCmdList.AGENTBRIDGE = function(arg)
    local cmd, rest = (arg or ''):lower():match('^%s*(%S*)%s*(.-)%s*$')
    if cmd == '' then NS.Toggle()
    elseif cmd == 'show' then NS.Panel:Show()
    elseif cmd == 'hide' then NS.Panel:Hide()
    elseif cmd == 'new' then NS.NewChat()
    elseif cmd == 'pause' then NS.Pause(); NS.SetStatus('Reply checks paused. Resume or Send to continue.')
    elseif cmd == 'resume' then NS.Resume()
    elseif cmd == 'test' then NS.RunSelfTest(true)
    elseif cmd == 'status' then
        local r = NS.ReceiverInfo()
        NS.Print(string.format('slot %d/%d, request %d, %s, font size %s, strip %s%s', r.slot, NS.BANK_SIZE,
            r.request, r.active and 'receiving' or 'idle', tostring(r.calib or 'not calibrated'), NS.S.strip,
            NS.recycledFrom and (', recycled from slot '..NS.recycledFrom) or ''))
    elseif cmd == 'strip' and NS.ANCHORS[rest:upper()] then
        NS.S.strip = rest:upper(); NS.PlaceStrip(NS.S.strip)
        NS.Print('strip moved to '..NS.S.strip..'. The companion finds it automatically.')
    elseif cmd == 'alpha' and tonumber(rest) then
        NS.Print('strip opacity set to '..NS.SetStripAlpha(tonumber(rest))..' (0.2 to 1)')
    elseif cmd == 'sound' and (rest == 'on' or rest == 'off') then
        NS.S.sound = rest == 'on'; NS.Print('sound '..rest)
    else
        for _, line in ipairs(HELP) do NS.Print(line) end
    end
end

NS.OnLoad(function(S)
    NS.SetTitle('Agent Bridge '..NS.VERSION)
    -- Earlier versions kept only the last reply; carry it into the transcript.
    local last = S.last
    if type(S.history) ~= 'table' and type(last) == 'table' and type(last.reply) == 'string' then
        S.history = {{c = S.conversation, p = last.prompt, r = last.reply, s = last.state or 4}}
    end
    S.last = nil
    NS.RefreshTranscript('last')
    if NS.HasHistory() then
        NS.SetStatus('Conversation restored. Scroll up for earlier replies; send a prompt to continue.')
    end
end)
