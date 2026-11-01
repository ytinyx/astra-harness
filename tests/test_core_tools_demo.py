import json
from pathlib import Path
import tempfile
import unittest

from examples.core_tools_demo import run_demo


class CoreToolsDemoTests(unittest.TestCase):
    def test_edit_process_and_restore_have_independent_artifacts(self):
        with tempfile.TemporaryDirectory(prefix='astra-core-demo-test-') as temporary:
            target=Path(temporary)/'proof'
            summary=run_demo(target)
            self.assertTrue(summary['passed'])
            self.assertEqual((summary['task_model_calls'],summary['task_tool_calls'],summary['restore_tool_calls']),(4,3,1))
            self.assertEqual((target/'workspace/settings.txt').read_bytes(), '# 合成配置\r\nlanguage=zh\r\nretries=2\r\n'.encode('utf-8'))
            runs=json.loads((target/'runs.json').read_text(encoding='utf-8'))
            self.assertEqual(len(runs),2)
            task=next(run for run in runs if run['model_calls'])
            command=next(message['tool_result']['value'] for message in task['messages']
                         if message.get('tool_result') and message['tool_result']['name']=='commands.run')
            self.assertEqual(json.loads(command['stdout']),{'language':'zh','retries':3,'crlf':True})
            self.assertEqual(command['exit_code'],0)
            self.assertEqual(summary['provider_requests'],0)

    def test_denial_preserves_source_and_does_not_start_verifier(self):
        with tempfile.TemporaryDirectory(prefix='astra-core-denied-') as temporary:
            target=Path(temporary)/'proof'
            summary=run_demo(target,approve=False)
            self.assertEqual((summary['task_status'],summary['task_tool_calls'],summary['subprocess_invocations']),('failed',1,0))
            self.assertFalse((target/'state/file-history').exists())
            runs=json.loads((target/'runs.json').read_text(encoding='utf-8'))
            self.assertEqual(len(runs),1)
            self.assertEqual(runs[0]['error'],'approval_denied')

    def test_existing_output_is_never_reused(self):
        with tempfile.TemporaryDirectory(prefix='astra-core-preserve-') as temporary:
            target=Path(temporary);(target/'marker.txt').write_bytes(b'keep')
            with self.assertRaises(FileExistsError):run_demo(target)
            self.assertEqual([path.name for path in target.iterdir()],['marker.txt'])
            self.assertEqual((target/'marker.txt').read_bytes(),b'keep')


if __name__=='__main__':unittest.main()
