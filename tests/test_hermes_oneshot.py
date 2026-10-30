import unittest
from companion.hermes_oneshot import result_event


class FiniteResults(unittest.TestCase):
    def test_partial_or_failed_text_is_not_a_success(self):
        for flags in ({}, {'completed':False}, {'completed':True,'partial':True},
                      {'completed':True,'interrupted':True}, {'completed':True,'error':'failed'}):
            with self.subTest(flags=flags):
                self.assertEqual(result_event('Some text', flags)['exit_code'], 1)
        self.assertEqual(result_event(' ', {'completed':True})['exit_code'], 1)

    def test_completed_native_session_survives_bridge_transport(self):
        event = result_event('Done', {'completed':True,'session_id':'test-session'})
        self.assertEqual(event['exit_code'], 0)
        self.assertEqual(event['session_id'], 'test-session')
        self.assertIsNone(event['error'])
