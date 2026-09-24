"""Native hyperlink widgets must receive the item/spell events, not a plain Frame."""
import tempfile
import unittest

from tests.harness import Sim
from tests.test_e2e import agent

ITEM = b'item:19019'
ITEM_LINK = b'|cffff8000|Hitem:19019|h[Thunderfury]|h|r'
SPELL = b'spell:48441'


class GameLinks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sim = Sim(self.tmp.name, agent(lambda p: 'Echo: ' + p))
        self.sim.run(2)
        self.ns, self.g = self.sim.ns, self.sim.g
        self.g.STUB[b'items'][ITEM] = self.sim.lua.table_from([b'Thunderfury', ITEM_LINK])
        self.g.STUB.spells[48441] = b'Rejuvenation'
        self.g.STUB.tooltips[ITEM] = self.sim.lua.table_from([b'Thunderfury', b'Legendary sword'])
        self.g.STUB.tooltips[SPELL] = self.sim.lua.table_from([b'Rejuvenation', b'Heals over time'])
        self.g.AgentBridgePanel.Show(self.g.AgentBridgePanel)

    def tearDown(self):
        self.sim.browser.db.close()
        self.tmp.cleanup()

    def render(self, text=b'Use [Thunderfury](item:19019) and [Rejuvenation](spell:48441).'):
        self.ns.StartExchange(900, self.ns.S.chat, b'which item and spell?')
        self.ns.ShowReply(900, text, 4, True)
        return self.rows()

    def rows(self):
        return [frame for frame in self.g.STUB.frames.values()
                if frame.kind == b'ScrollingMessageFrame' and frame.shown]

    def test_click_and_hover_use_native_chat_widgets_for_both_link_types(self):
        rows = self.render()
        self.assertEqual(len(rows), 1, 'two inline links share the same paragraph widget')
        row = rows[0]
        self.assertIn(b'|Hitem:19019|h', row.lines[1])
        self.assertIn(b'|Hspell:48441|h[Rejuvenation]|h', row.lines[1])
        for data in [ITEM, SPELL]:
            row.scripts[b'OnHyperlinkEnter'](row, data)
            self.assertEqual(self.g.GameTooltip.lastHyperlink, data)
            self.assertTrue(self.g.GameTooltip.shown)
            row.scripts[b'OnHyperlinkLeave'](row)
            self.assertFalse(self.g.GameTooltip.shown)
            row.scripts[b'OnHyperlinkClick'](row, data, b'[label]', b'LeftButton')
            self.assertEqual(self.g.STUB.itemRefs[len(self.g.STUB.itemRefs)][1], data)
        self.assertEqual(self.sim.opened_urls, [], 'game links never launch the browser')
        self.assertEqual(self.sim.jobs, {}, 'tooltips do not invoke an agent')

    def test_shift_click_inserts_the_verified_link_into_focused_prompt(self):
        row = self.render()[0]
        self.g.IsModifiedClick = self.sim.lua.eval("function(kind) return kind == 'CHATLINK' end")
        box = self.g.AgentBridgeInput
        box.SetFocus(box)
        row.scripts[b'OnHyperlinkClick'](row, ITEM, b'untrusted callback label', b'LeftButton')
        self.assertEqual(box.text, ITEM_LINK + b' ')
        row.scripts[b'OnHyperlinkClick'](row, SPELL, b'[spell]', b'LeftButton')
        self.assertIn(b'|Hspell:48441|h[Rejuvenation]|h', box.text)
        self.assertEqual(len(self.g.STUB.itemRefs), 0)

    def test_invalid_and_unknown_links_stay_plain_and_cannot_trigger_itemref(self):
        row = self.render()[0]
        row.scripts[b'OnHyperlinkClick'](row, b'player:Someone', b'anything', b'LeftButton')
        row.scripts[b'OnHyperlinkClick'](row, b'spell:999999', b'anything', b'LeftButton')
        self.assertEqual(len(self.g.STUB.itemRefs), 0)
        _, _, _, rows = self.ns.RenderReply(
            b'[Bad](spell:0) [Bad](spell:48441:evil) [Unknown](spell:999999) '
            b'[Bad](item:0) |Hspell:48441|h[Injected]|h')
        self.assertFalse(any(row[b'interactive'] for row in rows.values()))
        self.assertIn(b'||Hspell:48441', rows[1][b'text'])

    def test_link_widgets_refresh_without_duplicates_and_hide_on_chat_switch(self):
        row = self.render()[0]
        for _ in range(3):
            self.ns.RefreshTranscript()
        self.assertEqual(len(row.lines), 1)
        self.assertIn(b'Thunderfury', self.ns.BodyText(), 'copy/document inspection still includes interactive text')
        self.ns.NewChat()
        self.assertFalse(row.shown)
        self.assertEqual(self.rows(), [])

    def test_uncached_item_becomes_clickable_after_it_arrives(self):
        self.g.STUB[b'items'][ITEM] = None
        self.assertEqual(self.render(b'[Thunderfury](item:19019)'), [])
        self.g.STUB[b'items'][ITEM] = self.sim.lua.table_from([b'Thunderfury', ITEM_LINK])
        self.sim.run(2)
        self.assertEqual(len(self.rows()), 1)
        row = self.rows()[0]
        row.scripts[b'OnHyperlinkClick'](row, ITEM, ITEM_LINK, b'LeftButton')
        self.assertEqual(self.g.STUB.itemRefs[1][1], ITEM)

    def test_link_rows_follow_resize_and_forward_mouse_wheel_to_transcript(self):
        row = self.render()[0]
        scroll = self.g.AgentBridgeScroll
        scroll.SetWidth(scroll, 200)
        self.g.AgentBridgePanel.scripts[b'OnSizeChanged'](self.g.AgentBridgePanel)
        self.sim.run(.1)
        self.assertEqual(row.width, 196)
        self.g.AgentBridgeBody.SetHeight(self.g.AgentBridgeBody, 1800)
        scroll.SetVerticalScroll(scroll, 300)
        row.scripts[b'OnMouseWheel'](row, 1)
        self.assertLess(scroll.GetVerticalScroll(scroll), 300)
