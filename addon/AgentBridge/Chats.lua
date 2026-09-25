-- Parallel chats. Each is its own agent conversation: its own transcript here
-- and its own session in the companion, and replies for all of them arrive at
-- once. Saved as AgentBridgeState.chats, newest first (see Core.lua).
local NS = AgentBridge

local function chats() return NS.S.chats end

function NS.FindChat(id)
    for index, chat in ipairs(chats()) do
        if chat.id == id then return chat, index end
    end
end
function NS.CurrentChat() return (NS.FindChat(NS.S.chat)) end
function NS.ChatTitle(id)
    local chat = NS.FindChat(id)
    if not chat then return 'deleted chat' end
    return chat.name or chat.auto or 'New chat'
end

local function clean(name)
    name = tostring(name or ''):gsub('%c', ' '):gsub('^%s+', ''):gsub('%s+$', '')
    if name == '' then return nil end
    return NS.TrimUTF8(name:sub(1, 40))
end

-- Show a chat in the panel, from its newest line.
function NS.SelectChat(id)
    local chat = NS.FindChat(id)
    if not chat then return false end
    if NS.CloseCopy then NS.CloseCopy() end
    NS.S.chat, chat.unread = id, nil
    NS.RefreshTranscript()
    NS.ScrollToEnd()
    if NS.RefreshChats then NS.RefreshChats() end
    return true
end

function NS.NewChat(name)
    local newest = 0
    for _, chat in ipairs(chats()) do newest = math.max(newest, chat.id) end
    -- Always a new id, even for two chats started within the same second.
    local chat = {id = math.max(time(), newest + 1), name = clean(name)}
    table.insert(chats(), 1, chat)
    NS.SelectChat(chat.id)
    NS.SetStatus('New chat. The agent will not see your other chats.')
    return chat
end

function NS.RenameChat(id, name)
    local chat = NS.FindChat(id)
    if not chat then return end
    chat.name = clean(name)
    if NS.RefreshChats then NS.RefreshChats() end
end

-- The companion keeps the replies; only this transcript goes.
function NS.DeleteChat(id)
    local chat, index = NS.FindChat(id)
    if not chat then return end
    NS.ForgetChat(id)
    table.remove(chats(), index)
    if #chats() == 0 then
        NS.NewChat()
    elseif NS.S.chat == id then
        NS.SelectChat(chats()[math.min(index, #chats())].id)
    elseif NS.RefreshChats then
        NS.RefreshChats()
    end
end

-- Agents and models. chat.agent and chat.model are your choices for the chat
-- and travel with each prompt; unset, the companion decides (a chat keeps the
-- agent it last used, a new one takes the companion's selection, and the model
-- is the companion's default for that agent). chat.seenAgent and seenModel are
-- what the companion last reported actually answering.
NS.AGENTS = {claude = 'Claude', codex = 'Codex', hermes = 'Hermes', mock = 'Mock'}
local function validModel(model)
    return type(model) == 'string' and #model <= 64 and model:match('^%w[%w%._:/%[%]%-]*$') ~= nil
end

-- Returns the agent, the model, and whether each was chosen for this chat.
function NS.ChatAgent(id)
    local chat = NS.FindChat(id)
    if not chat then return end
    return chat.agent or chat.seenAgent, chat.model or chat.seenModel, chat.agent ~= nil, chat.model ~= nil
end

function NS.SetChatAgent(id, agent)
    local chat = NS.FindChat(id)
    if not chat or (agent and not NS.AGENTS[agent]) then return false end
    -- A model name belongs to one agent, so a new agent starts on its default.
    if agent ~= (chat.agent or chat.seenAgent) then chat.model, chat.seenModel = nil, nil end
    chat.agent = agent
    if NS.RefreshChats then NS.RefreshChats() end
    return true
end

function NS.SetChatModel(id, model)
    local chat = NS.FindChat(id)
    if not chat or (model and not validModel(model)) then return false end
    chat.model = model
    if NS.RefreshChats then NS.RefreshChats() end
    return true
end

-- From the header the companion puts ahead of each reply.
function NS.NoteAgent(id, agent, model)
    local chat = NS.FindChat(id)
    if not chat or not NS.AGENTS[agent] then return end
    local changed = chat.seenAgent ~= agent
    chat.seenAgent, chat.seenModel = agent, validModel(model) and model or nil
    if changed and NS.RefreshChats then NS.RefreshChats() end
end

-- "Codex, model gpt-5.5 (set for this chat)" and the like.
function NS.DescribeAgent(id)
    local agent, model, ownAgent, ownModel = NS.ChatAgent(id)
    if not agent then return "the companion's choice (not known until the first reply)" end
    local text = NS.AGENTS[agent]..(ownAgent and ' (set for this chat)' or '')
    if model then
        text = text..', model '..model..(ownModel and ' (set for this chat)' or " (the companion's default)")
    else
        text = text..", the companion's default model"
    end
    return text
end

-- A chat that finished while you were elsewhere is marked until you open it.
function NS.MarkUnread(id)
    local chat = NS.FindChat(id)
    if chat then chat.unread = true end
    if NS.RefreshChats then NS.RefreshChats() end
end

-- Chats by number (as listed, newest first) or by the start of their title.
function NS.FindChatByName(text)
    local n = tonumber(text)
    if n then
        local chat = chats()[n]
        return chat and chat.id
    end
    text = text:lower()
    for _, chat in ipairs(chats()) do
        if NS.ChatTitle(chat.id):lower():sub(1, #text) == text then return chat.id end
    end
end

-- Dialogs. Only our own keys are added to Blizzard's table (never the global
-- itself, which would taint it). Data is set after showing, as the 3.3.5a
-- popup code does not pass it through before OnShow.
StaticPopupDialogs.AGENTBRIDGE_RENAME = {
    text = 'Rename the chat "%s":', button1 = ACCEPT or 'Accept', button2 = CANCEL or 'Cancel',
    hasEditBox = 1, maxLetters = 40, timeout = 0, whileDead = 1, hideOnEscape = 1,
    OnAccept = function(self)
        local box = _G[self:GetName()..'EditBox']
        NS.RenameChat(self.data, box and box:GetText())
    end,
    EditBoxOnEnterPressed = function(self)
        local dialog = self:GetParent()
        NS.RenameChat(dialog.data, self:GetText())
        dialog:Hide()
    end,
    EditBoxOnEscapePressed = function(self) self:GetParent():Hide() end,
}
StaticPopupDialogs.AGENTBRIDGE_DELETE = {
    text = 'Delete the chat "%s"? Its transcript leaves the game; the companion keeps its replies.',
    button1 = DELETE or 'Delete', button2 = CANCEL or 'Cancel', timeout = 0, whileDead = 1, hideOnEscape = 1,
    OnAccept = function(self) NS.DeleteChat(self.data) end,
}

StaticPopupDialogs.AGENTBRIDGE_MODEL = {
    text = 'Model for "%s" (leave blank for the companion\'s default):', button1 = ACCEPT or 'Accept',
    button2 = CANCEL or 'Cancel', hasEditBox = 1, maxLetters = 64, timeout = 0, whileDead = 1, hideOnEscape = 1,
    OnAccept = function(self)
        local box = _G[self:GetName()..'EditBox']
        NS.ApplyModel(self.data, box and box:GetText())
    end,
    EditBoxOnEnterPressed = function(self)
        local dialog = self:GetParent()
        NS.ApplyModel(dialog.data, self:GetText())
        dialog:Hide()
    end,
    EditBoxOnEscapePressed = function(self) self:GetParent():Hide() end,
}

-- Set a chat's model from typed text: blank goes back to the default.
function NS.ApplyModel(id, text)
    text = tostring(text or ''):gsub('^%s+', ''):gsub('%s+$', '')
    if text == '' or text:lower() == 'default' then
        NS.SetChatModel(id, nil)
    elseif not NS.SetChatModel(id, text) then
        NS.Print('"'..NS.Escape(text)..'" is not a model name (letters, digits and . _ : / - [ ] only).')
        return false
    end
    NS.Print('"'..NS.Escape(NS.ChatTitle(id))..'" uses '..NS.DescribeAgent(id)..'.')
    return true
end

function NS.AskModel(id)
    local dialog = StaticPopup_Show('AGENTBRIDGE_MODEL', NS.Escape(NS.ChatTitle(id)))
    if not dialog then return end
    dialog.data = id
    local box = _G[dialog:GetName()..'EditBox']
    if box then
        local chat = NS.FindChat(id)
        box:SetText(chat and chat.model or ''); box:HighlightText(); box:SetFocus()
    end
end

function NS.AskRename(id)
    local dialog = StaticPopup_Show('AGENTBRIDGE_RENAME', NS.Escape(NS.ChatTitle(id)))
    if not dialog then return end
    dialog.data = id
    local box = _G[dialog:GetName()..'EditBox']
    if box then
        local chat = NS.FindChat(id)
        box:SetText(chat and chat.name or ''); box:HighlightText(); box:SetFocus()
    end
end
function NS.AskDelete(id)
    local dialog = StaticPopup_Show('AGENTBRIDGE_DELETE', NS.Escape(NS.ChatTitle(id)))
    if dialog then dialog.data = id end
end
