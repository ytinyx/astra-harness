import json
from pathlib import Path
import tempfile
import unittest

from examples.embedding_demo import run_demo


class EmbeddingDemoTests(unittest.TestCase):
    def test_local_protocol_persistence_ranking_and_stale_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)/'demo'
            result = run_demo(directory)
            self.assertTrue(result['passed'])
            self.assertEqual(result['real_provider_requests'], 0)
            self.assertEqual(result['task_model_calls'], 0)
            self.assertEqual(result['mock_http_requests'], 4)
            self.assertEqual(result['nearest_path'], 'alpha.md')
            self.assertTrue(result['reopen_only_query'])
            self.assertEqual(result['stale_query_request_count'], 0)
            self.assertEqual(result, json.loads((directory/'summary.json').read_text(encoding='utf-8')))
            self.assertTrue((directory/'lexical.sqlite3.vectors.sqlite3').is_file())

    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            sentinel = directory/'keep.txt'
            sentinel.write_text('keep', encoding='utf-8')
            with self.assertRaises(FileExistsError):
                run_demo(directory)
            self.assertEqual(list(directory.iterdir()), [sentinel])
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'keep')


if __name__ == '__main__':
    unittest.main()
