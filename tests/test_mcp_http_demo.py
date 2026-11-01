"""The HTTP example is judged by server receipts, including ambiguous effects."""
import json
from pathlib import Path
import tempfile
import unittest

from examples.mcp_http_demo import run_demo


class HTTPMCPDemoTests(unittest.TestCase):
    def test_real_loopback_cases_leave_independently_checkable_receipts(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'demo'
            result = run_demo(directory)
            self.assertTrue(result['passed'])
            self.assertEqual(result['provider_requests'], 0)
            cases = {case['name']: case for case in result['cases']}
            self.assertEqual({key: value['status'] for key, value in cases.items()},
                             {'json': 'completed', 'sse': 'completed', 'denied': 'failed', 'uncertain': 'unknown'})
            for name, expected in (('json', [27]), ('sse', [31]), ('denied', []), ('uncertain', [43])):
                receipts = json.loads((directory/name/'receipts.json').read_text(encoding='utf-8'))
                self.assertEqual([row['value'] for row in receipts], expected)
                self.assertEqual(cases[name]['model_calls'], 0)
                self.assertEqual(cases[name]['automatic_replays'], 0)
                self.assertTrue((directory/name/'trace.json').is_file())
            self.assertTrue(cases['uncertain']['unknown_gate_checked'])
            self.assertEqual(json.loads((directory/'summary.json').read_text(encoding='utf-8')), result)

    def test_existing_output_is_preserved_before_any_network_setup(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            sentinel = directory/'sentinel.txt'
            sentinel.write_text('keep', encoding='utf-8')
            with self.assertRaises(FileExistsError):
                run_demo(directory)
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'keep')
            self.assertEqual(list(directory.iterdir()), [sentinel])


if __name__ == '__main__':
    unittest.main()
