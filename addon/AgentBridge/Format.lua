-- Turn an agent's Markdown-ish reply into lines that read well in a narrow
-- game panel. Returns entries of {text = <raw text>, style = <name>} so the
-- renderer can escape the text before adding any colour of its own.
local NS = AgentBridge

local function trim(s) return (s:gsub('^%s+', ''):gsub('%s+$', '')) end

-- The 2010 client's fonts do not cover typographic punctuation; agents use it
-- constantly. Swap in ASCII rather than showing boxes.
local ASCII = {
    ['\226\128\148'] = '-', ['\226\128\147'] = '-', ['\226\128\152'] = "'", ['\226\128\153'] = "'",
    ['\226\128\156'] = '"', ['\226\128\157'] = '"', ['\226\128\166'] = '...', ['\226\128\162'] = '-',
    ['\226\134\146'] = '->', ['\226\156\147'] = 'ok', ['\194\160'] = ' ',
}
function NS.Transliterate(s)
    for from, to in pairs(ASCII) do s = s:gsub(from, to) end
    return s
end

-- "**bold**" and "`code`" carry no meaning here; keep the words, drop the marks.
local function marks(s) return (s:gsub('%*%*(.-)%*%*', '%1'):gsub('`([^`]-)`', '%1')) end
local function plain(s) return trim(marks(s)) end

local function isRow(line) return line:match('^%s*|.*|%s*$') ~= nil end
local function isDivider(line) return line:match('^%s*|[%s|:%-]+|%s*$') ~= nil and line:find('%-') ~= nil end

local function cells(line)
    local out = {}
    for cell in (trim(line)..'|'):gmatch('|([^|]*)') do out[#out+1] = plain(cell) end
    out[#out] = nil  -- trailing empty field after the final pipe
    return out
end

-- Column widths are in characters, not bytes: UTF-8 would inflate them.
local function len(s)
    local count = 0
    for i = 1, #s do
        local byte = s:byte(i)
        if byte < 128 or byte >= 192 then count = count + 1 end
    end
    return count
end

local function width(row)
    local total = 0
    for _, cell in ipairs(row) do total = total + len(cell) end
    return total + 3 * math.max(0, #row - 1)
end

local function pad(text, size)
    return text..string.rep(' ', math.max(0, size - len(text)))
end

-- A table that fits is aligned; one that does not becomes a block per row,
-- which is far easier to read than wrapped pipes.
local function renderTable(rows, columns, aligned, out)
    local header, body = rows[1], {}
    for i = 2, #rows do body[#body+1] = rows[i] end
    local widths, fits = {}, true
    for _, row in ipairs(rows) do
        for i, cell in ipairs(row) do widths[i] = math.max(widths[i] or 0, len(cell)) end
    end
    local total = 0
    for _, w in ipairs(widths) do total = total + w end
    fits = aligned and total + 3 * math.max(0, #widths - 1) <= columns
    if fits then
        local line = {}
        for i, cell in ipairs(header) do line[#line+1] = pad(cell, widths[i]) end
        out[#out+1] = {text = table.concat(line, '   '), style = 'heading'}
        out[#out+1] = {text = string.rep('-', math.min(columns, width(header))), style = 'dim'}
        for _, row in ipairs(body) do
            line = {}
            for i = 1, #widths do line[#line+1] = pad(row[i] or '', widths[i]) end
            out[#out+1] = {text = table.concat(line, '   ')}
        end
        return
    end
    for index, row in ipairs(body) do
        if index > 1 then out[#out+1] = {text = ''} end
        out[#out+1] = {text = row[1] ~= '' and row[1] or ('Row '..index), style = 'label'}
        for i = 2, #row do
            if row[i] ~= '' then
                out[#out+1] = {text = '   '..(header[i] or ('Column '..i))..': '..row[i]}
            end
        end
    end
end

-- columns is how many fixed-width characters fit across the panel; 0 means
-- there is no fixed-width font, so tables are never aligned.
function NS.FormatLines(text, columns)
    columns = columns or 70
    local aligned = columns >= 24
    if not aligned then columns = 60 end
    local lines = {}
    for line in (tostring(text)..'\n'):gmatch('(.-)\r?\n') do lines[#lines+1] = NS.Transliterate(line) end
    local out, mono, index = {}, false, 1
    while index <= #lines do
        local line = lines[index]
        if line:match('^%s*```') then
            -- Fenced code: keep every character, including leading spaces.
            mono = true
            index = index + 1
            while index <= #lines and not lines[index]:match('^%s*```') do
                out[#out+1] = {text = lines[index], style = 'code'}
                index = index + 1
            end
        elseif isRow(line) and lines[index+1] and isDivider(lines[index+1]) then
            local rows = {cells(line)}
            index = index + 2
            while index <= #lines and isRow(lines[index]) do
                rows[#rows+1] = cells(lines[index])
                index = index + 1
            end
            mono = aligned
            renderTable(rows, columns, aligned, out)
            index = index - 1
        elseif line:match('^%s*#+%s') then
            out[#out+1] = {text = plain(line:gsub('^%s*#+%s*', '')), style = 'heading'}
        elseif line:match('^%s*[-*+]%s+') then
            out[#out+1] = {text = marks(line:gsub('^(%s*)[-*+]%s+', '%1- '))}
        elseif trim(line):match('^[-=_]+$') and #trim(line) >= 3 then
            out[#out+1] = {text = string.rep('-', math.min(columns, 40)), style = 'dim'}
        else
            out[#out+1] = {text = marks(line)}
        end
        index = index + 1
    end
    while #out > 0 and trim(out[#out].text) == '' do out[#out] = nil end
    return out, mono
end
