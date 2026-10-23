"""Each chat shows the character it belongs to; the list can show only the current character's chats."""
import json
import sqlite3
import tempfile
import unittest

from companion.character import CharacterStore
from tests.harness import Sim
from tests.test_e2e import agent

ME, ALT = 'Testbrew-ChromieCraft', 'Altbrew-ChromieCraft'
OLD = {
    'nextSlot': 1, 'chat': 1700000000,
    'chats': {1: {'id': 1700000000}, 2: {'id': 1700000001}, 3: {'id': 1700000002}},
    'history': {1: {'c': 1700000000, 'p': 'paladin question', 'r': 'paladin answer', 's': 4},
                2: {'c': 1700000001, 'p': 'mystery question', 'r': 'mystery answer', 's': 4}},
}
INBOX = ('CREATE TABLE jobs (id TEXT PRIMARY KEY, prompt TEXT, state TEXT, reply TEXT, backend TEXT, '
         'agent_session TEXT, created REAL, chat TEXT, meta TEXT)')


def context(name, realm='ChromieCraft'):
    return json.dumps({'name': 'x', 'model': '', 'context': f'Client: World of Warcraft 3.3.5 (build 12340), realm {realm}\n'
                                                           f'Character: {name}, level 70 Blood Elf Paladin (Horde)\n'
                                                           'Location: Silvermoon City'})


class ChatCharacters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def boot(self, saved=None):
        self.sim = Sim(self.tmp.name, agent(lambda p: 'Echo: ' + p), saved=saved, slots=160)
        self.addCleanup(self.sim.browser.db.close)
        self.ns, self.g = self.sim.ns, self.sim.g
        self.sim.run(2)

    def tag(self, chat):
        value = self.ns.ChatCharacter(chat)
        return bytes(value).decode() if isinstance(value, bytes) else value

    def prints(self):
        return '\n'.join(bytes(line).decode() for line in self.g.STUB.prints.values())

    def play(self, name):
        self.sim.lua.execute(f"function UnitName() return '{name}' end".encode())

    def show(self):
        panel = self.g.AgentBridgePanel
        panel.scripts[b'OnShow'](panel)

    def test_new_chats_belong_to_the_character_playing_and_say_so(self):
        self.boot()
        first = self.g.AgentBridgeState.chat
        self.assertEqual(self.tag(first), ME, 'the empty chat a first install starts with')
        self.ns.NewChat(b'Raid prep')
        self.assertEqual(self.tag(self.g.AgentBridgeState.chat), ME)
        header = bytes(self.ns.ChatHeader.GetText(self.ns.ChatHeader)).decode()
        self.assertIn('Raid prep', header)
        self.assertIn('Testbrew', header)
        self.assertNotIn('you are playing', header)
        # On an alt, the old chat says whose it is, and a prompt sent there moves it to the alt.
        self.play('Altbrew')
        self.ns.SelectChat(first)
        self.assertIn('you are playing Altbrew', bytes(self.ns.ChatHeader.GetText(self.ns.ChatHeader)).decode())
        self.sim.send('alt question')
        self.assertEqual(self.tag(first), ALT)
        self.assertIn('now belongs to Altbrew (it was Testbrew)', self.prints())
        self.assertTrue(self.sim.run(60, until=lambda: self.sim.last_reply() == 'Echo: alt question'))

    def test_list_can_show_only_this_characters_chats(self):
        self.boot()
        mine = self.g.AgentBridgeState.chat
        self.ns.NewChat(b'Second')
        self.assertEqual(len(self.ns.VisibleChats()), 2)
        self.play('Altbrew')
        self.g.SlashCmdList.AGENTBRIDGE(b'chats mine')
        # No chat of the alt's yet: one is started, so a prompt never lands in another character's chat.
        state = self.g.AgentBridgeState
        self.assertEqual(bytes(state.chatFilter), b'character')
        self.assertEqual(len(state.chats), 3)
        self.assertEqual(self.tag(state.chat), ALT)
        self.assertEqual([chat.id for chat in self.ns.VisibleChats().values()], [state.chat])
        self.assertEqual(bytes(self.ns.FilterButton.GetText(self.ns.FilterButton)), b'Only Altbrew')
        # Back on the main: its newest chat is selected, the alt's is hidden.
        self.play('Testbrew')
        self.sim.reload()
        self.assertEqual(self.sim.g.AgentBridgeState.chat, self.sim.ns.FindChatByName(b'Second'))
        self.assertEqual(len(self.sim.ns.VisibleChats()), 2)
        self.sim.g.SlashCmdList.AGENTBRIDGE(b'chats all')
        self.assertEqual(len(self.sim.ns.VisibleChats()), 3)
        self.assertIsNone(self.sim.g.AgentBridgeState.chatFilter)
        self.sim.g.SlashCmdList.AGENTBRIDGE(b'chats')
        listed = '\n'.join(bytes(line).decode() for line in self.sim.g.STUB.prints.values())
        self.assertIn('[Altbrew]', listed)
        self.assertIn('[Testbrew]', listed)
        self.assertIsNotNone(self.sim.ns.FindChat(mine))

    def test_older_chats_are_placed_by_the_companion_once(self):
        self.boot(OLD)
        with self.sim.browser.db:
            self.sim.browser.db.execute(INBOX)
            self.sim.browser.db.execute('INSERT INTO jobs (id, chat, meta) VALUES (?,?,?)',
                                        ('s:1', f'{1700000000:08x}', context('Gothenheimer')))
        self.assertIsNone(self.tag(1700000000))
        self.assertEqual(self.tag(1700000002), ME, 'an empty chat belongs to whoever is playing')
        self.show()
        self.assertTrue(self.sim.run(120, until=lambda: self.tag(1700000000) is not None))
        self.assertEqual(self.tag(1700000000), 'Gothenheimer-ChromieCraft')
        self.assertIs(self.tag(1700000001), False, 'no stored prompt names a character')
        self.assertIn('unknown character', bytes(self.ns.ChatCharacterText(1700000001)).decode())
        frames = self.sim.character_frames
        self.show()
        self.sim.run(5)
        self.assertEqual(frames, self.sim.character_frames, 'asked once')
        self.assertEqual(self.sim.saved()['chats'][1]['character'], 'Gothenheimer-ChromieCraft', 'kept in saved variables')

    def test_an_older_companion_refuses_and_chats_stay_unplaced(self):
        self.boot(OLD)
        original = self.sim.character._page
        def old(blob):
            if 'probe=chats' in blob:
                raise ValueError('Invalid character page header')
            return original(blob)
        self.sim.character._page = old
        self.show()
        self.sim.run(60)
        self.assertIsNone(self.tag(1700000000))
        self.assertIsNone(self.tag(1700000001))
        self.assertEqual(self.ns.ReceiverInfo().pending, 0)


class Lookup(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = CharacterStore(self.db, tmp.name)

    def ask(self, ids, **extra):
        fields = {'probe': ['chats'], 'chats': [ids], **extra}
        return self.store.chat_characters(fields, 'ABCTX_CHATS')

    def test_newest_prompt_names_the_character(self):
        self.assertEqual(self.ask('1700000000'), '\x01ABCHATS1\n1700000000|', 'an inbox without prompts')
        self.db.execute(INBOX)
        rows = [('a:1', f'{1700000000:08x}', context('Oldname')), ('a:2', f'{1700000000:08x}', context('Gothenheimer')),
                ('a:3', f'{1700000001:08x}', json.dumps({'context': 'Location: nowhere'})),
                ('a:4', f'{1700000002:08x}', 'not json')]
        self.db.executemany('INSERT INTO jobs (id, chat, meta) VALUES (?,?,?)', rows)
        self.assertEqual(self.ask('1700000000,1700000001,1700000002'),
                         '\x01ABCHATS1\n1700000000|Gothenheimer-ChromieCraft\n1700000001|\n1700000002|')

    def test_rejects_malformed_lookups(self):
        for ids in ('', '0', '1,1', 'x', '4294967296', ','.join(str(i) for i in range(1, 66))):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.ask(ids)
        with self.assertRaises(ValueError):
            self.ask('1', character=['someone'])
        with self.assertRaises(ValueError):
            self.store.chat_characters({'probe': ['chats'], 'chats': ['1']}, 'something else')


if __name__ == '__main__':
    unittest.main()
