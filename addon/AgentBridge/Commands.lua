-- Sending prompts, completion alerts and slash commands.
local NS = AgentBridge
local edit = NS.Input
local sequence = 0

-- Send a prompt to the chat you are reading. The companion gets an envelope:
-- which chat it belongs to, its name, the agent and model if you chose them for
-- it, and (unless /ab context off) where your character is and what it is
-- doing; then your text with links spelled out and their tooltips. Returns
-- false and a reason when nothing was sent.
function NS.Send(raw)
    if type(raw) ~= 'string' or not raw:find('%S') then return false, 'Type a message first.' end
    local chat = NS.CurrentChat()
    -- An unnamed chat is titled by its first message.
    local title = chat.name or chat.auto or NS.ShortTitle(select(2, NS.MakePromptText(raw)))
    local fields = {{'chat', chat.id}, {'name', title}}
    if chat.agent then fields[#fields+1] = {'agent', chat.agent} end
    if chat.model then fields[#fields+1] = {'model', chat.model} end
    if NS.S.context then
        local ok, context = pcall(NS.GameContext)
        if ok and type(context) == 'string' then
            for line in context:gmatch('[^\n]+') do fields[#fields+1] = {'ctx', line} end
        end
    end
    local body, display = NS.MakePromptText(raw, NS.MAX_PROMPT - #NS.Envelope(fields, ''))
    local text = NS.Envelope(fields, body)
    if #text > NS.MAX_PROMPT then
        return false, 'Message plus link details is too long ('..#text..'/'..NS.MAX_PROMPT..' bytes). Shorten it.'
    end
    sequence = sequence + 1
    NS.QueuePrompt(sequence, NS.EncodePrompt(text, NS.session, sequence))
    NS.BeginRequest(sequence, chat.id)
    NS.SetBadge(nil)
    if not chat.name then chat.auto = chat.auto or title end
    NS.StartExchange(sequence, chat.id, display)
    NS.SetStatus('Prompt #'..sequence..' sent. You can close this panel and keep playing.')
    return true
end

local function submit()
    local ok, why = NS.Send(edit:GetText())
    if not ok then NS.SetStatus(why); return end
    edit:SetText(''); edit:ClearFocus()
end
edit:SetScript('OnEnterPressed', submit)
NS.SendButton:SetScript('OnClick', submit)

function NS.OnReplyFinished(chat, state)
    local title = NS.ChatTitle(chat)
    if chat == NS.S.chat then
        NS.SetStatus(state == 4 and 'Reply ready.' or 'The request finished with a problem; details below.')
    else
        NS.SetStatus((state == 4 and 'Reply ready in "' or 'Problem in "')..NS.Escape(title)..'".')
    end
    if NS.Panel:IsShown() and chat == NS.S.chat then return end
    NS.MarkUnread(chat)
    if not NS.Panel:IsShown() then NS.SetBadge(state) end
    -- With echo on the reply itself is already in the chat frame.
    if (tonumber(NS.S.echo) or 0) <= 0 then
        NS.Print((state == 4 and 'reply ready in "' or 'problem in "')..NS.Escape(title)..'". Type /ab to read it.')
    end
    if NS.S.sound then PlaySound('TellMessage') end
end

local function listChats()
    for index, chat in ipairs(NS.S.chats) do
        local mark = chat.id == NS.S.chat and '|cff66bbff>|r ' or '  '
        local state = NS.IsChatBusy(chat.id) and ' (working)' or chat.unread and ' (unread)' or ''
        local agent, model = NS.ChatAgent(chat.id)
        local who = agent and (' |cff909090- '..NS.AGENTS[agent]..(model and (', '..NS.Escape(model)) or '')..'|r') or ''
        NS.Print(mark..index..'. '..NS.Escape(NS.ChatTitle(chat.id))..state..who)
    end
end

local ECHO = {off = 0, short = 800, full = 100000}

-- Switching agents cannot carry a session over: the new agent starts afresh,
-- given the chat's recent turns as history.
function NS.UseAgent(id, agent)
    local before = NS.ChatAgent(id)
    if not NS.SetChatAgent(id, agent) then
        NS.Print('Unknown agent "'..NS.Escape(agent)..'". Use /ab agent claude, codex or mock.')
        return false
    end
    local title = NS.Escape(NS.ChatTitle(id))
    if before and before ~= agent and NS.HasHistory(id) then
        NS.Print('"'..title..'" now uses '..NS.AGENTS[agent]..'. Its next prompt starts a new '..NS.AGENTS[agent]
            ..' session, given this chat\'s recent turns as history.')
    else
        NS.Print('"'..title..'" uses '..NS.DescribeAgent(id)..'.')
    end
    return true
end

local HELP = {
    '/ab - show or hide the panel (also /agent, /claude, /codex)',
    '/ai <message> - send to the current chat without opening the panel',
    '/ab new [name] - start another chat (the others keep going)',
    '/ab chats - list chats;  /ab chat <number or name> - switch',
    '/ab rename <name> | delete - rename or delete the current chat',
    '/ab agent claude|codex - which agent answers the current chat (no name: show it)',
    '/ab model <name>|default - the model for the current chat (no name: show it)',
    '/ab copy [all] - copy the last reply (or the whole chat) out of the game',
    '/ab echo off|short|full|<n> - copy finished replies you are not reading into chat',
    '/ab context on|off|show - send your character, zone, money, talents and professions with each prompt',
    '/ab pause | resume - stop or restart reply checks',
    '/ab test - run the font self-test and print results',
    '/ab status - show channel state',
    '/ab strip top|topleft|topright|bottom|bottomleft|bottomright - move the pixel strip',
    '/ab alpha 0.5 - strip opacity, so you can see the UI behind it (0.2 to 1)',
    '/ab sound on|off - chime when a reply is ready',
}
SLASH_AGENTBRIDGE1, SLASH_AGENTBRIDGE2, SLASH_AGENTBRIDGE3, SLASH_AGENTBRIDGE4 = '/ab', '/agent', '/claude', '/codex'
SlashCmdList.AGENTBRIDGE = function(arg)
    local cmd, rest = (arg or ''):match('^%s*(%S*)%s*(.-)%s*$')
    cmd = cmd:lower()
    local word = rest:lower()
    if cmd == '' then NS.Toggle()
    elseif cmd == 'show' then NS.Panel:Show()
    elseif cmd == 'hide' then NS.Panel:Hide()
    elseif cmd == 'new' then
        NS.NewChat(rest ~= '' and rest or nil)
        NS.Print('started "'..NS.Escape(NS.ChatTitle(NS.S.chat))..'". /ai sends to it.')
    elseif cmd == 'chats' then listChats()
    elseif cmd == 'chat' and rest ~= '' then
        local id = NS.FindChatByName(rest)
        if id and NS.SelectChat(id) then
            NS.Print('now in "'..NS.Escape(NS.ChatTitle(id))..'".')
        else
            NS.Print('no chat matches "'..NS.Escape(rest)..'". /ab chats lists them.')
        end
    elseif cmd == 'rename' and rest ~= '' then NS.RenameChat(NS.S.chat, rest)
    elseif cmd == 'delete' then NS.AskDelete(NS.S.chat)
    elseif cmd == 'agent' and word ~= '' then NS.UseAgent(NS.S.chat, word)
    elseif cmd == 'model' and rest ~= '' then NS.ApplyModel(NS.S.chat, rest)
    elseif cmd == 'agent' or cmd == 'model' then
        NS.Print('"'..NS.Escape(NS.ChatTitle(NS.S.chat))..'" uses '..NS.DescribeAgent(NS.S.chat)..'.')
    elseif cmd == 'copy' then NS.ShowCopy(word == 'all')
    elseif cmd == 'echo' and (ECHO[word] or tonumber(word)) then
        NS.S.echo = ECHO[word] or math.max(0, math.floor(tonumber(word)))
        NS.Print(NS.S.echo == 0 and 'replies are no longer copied into chat.'
            or ('finished replies you are not reading are copied into chat, up to '..NS.S.echo..' characters.'))
    elseif cmd == 'context' and (word == 'on' or word == 'off') then
        NS.S.context = word == 'on'
        NS.Print('game context '..(NS.S.context and 'is sent with each prompt.' or 'is no longer sent.'))
    elseif cmd == 'context' and word == 'show' then
        local ok, context = pcall(NS.GameContext)
        NS.Print((NS.S.context and 'sent with each prompt:' or 'off (would send):'))
        for line in tostring(ok and context or 'unavailable'):gmatch('[^\n]+') do NS.Print('  '..NS.Escape(line)) end
    elseif cmd == 'pause' then NS.Pause(); NS.SetStatus('Reply checks paused. Resume or Send to continue.')
    elseif cmd == 'resume' then NS.Resume()
    elseif cmd == 'test' then NS.RunSelfTest(true)
    elseif cmd == 'lodtest' and NS.LoDTest then NS.LoDTest()
    elseif cmd == 'status' then
        local r = NS.ReceiverInfo()
        NS.Print(string.format('slot %d/%d, %d request(s) waiting, %s, font size %s, strip %s%s', r.slot,
            NS.BANK_SIZE, r.pending, r.active and 'receiving' or 'idle', tostring(r.calib or 'not calibrated'),
            NS.S.strip, NS.recycledFrom and (', recycled from slot '..NS.recycledFrom) or ''))
    elseif cmd == 'strip' and NS.ANCHORS[word:upper()] then
        NS.S.strip = word:upper(); NS.PlaceStrip(NS.S.strip)
        NS.Print('strip moved to '..NS.S.strip..'. The companion finds it automatically.')
    elseif cmd == 'alpha' and tonumber(word) then
        NS.Print('strip opacity set to '..NS.SetStripAlpha(tonumber(word))..' (0.2 to 1)')
    elseif cmd == 'sound' and (word == 'on' or word == 'off') then
        NS.S.sound = word == 'on'; NS.Print('sound '..word)
    else
        for _, line in ipairs(HELP) do NS.Print(line) end
    end
end

-- /ai <message>: straight from the chat box, links included.
SLASH_AGENTBRIDGEAI1 = '/ai'
SlashCmdList.AGENTBRIDGEAI = function(arg)
    local ok, why = NS.Send(arg or '')
    if ok then
        NS.Print('sent to "'..NS.Escape(NS.ChatTitle(NS.S.chat))..'".')
    else
        NS.Print(why)
    end
end

NS.OnLoad(function(S)
    NS.SetTitle('Agent Bridge '..NS.VERSION)
    NS.RefreshTranscript('last')
    NS.RefreshChats()
    if NS.HasHistory() then
        NS.SetStatus('Conversation restored. Scroll up for earlier replies; send a prompt to continue.')
    end
end)
