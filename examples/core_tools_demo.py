"""Inspect → precise edit → process verification → conditional restore.

Only generated files and a short, self-written Python verifier are used. The
scripted model consumes real tool results; it is not a real-model evaluation.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
import tempfile

from astra_harness.application import Application
from astra_harness.builtin.commands import CommandPlugin
from astra_harness.builtin.editing import FileEditPlugin
from astra_harness.builtin.files import FilePlugin
from astra_harness.contracts import ModelResponse, ToolCall
from astra_harness.plugins import PluginManager
from astra_harness.tools import ToolRegistry


ORIGINAL = '# 合成配置\r\nlanguage=zh\r\nretries=2\r\n'.encode('utf-8')
EXPECTED = '# 合成配置\r\nlanguage=zh\r\nretries=3\r\n'.encode('utf-8')
VERIFIER = '''from pathlib import Path
import json
raw = Path("settings.txt").read_bytes()
values = dict(line.split("=", 1) for line in raw.decode("utf-8").splitlines() if "=" in line)
result = {"language": values.get("language"), "retries": int(values["retries"]), "crlf": b"\\r\\n" in raw}
print(json.dumps(result, sort_keys=True))
raise SystemExit(0 if result == {"language": "zh", "retries": 3, "crlf": True} else 2)
'''


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


class EditAndVerifyModel:
    """Fixed plan, with the edit precondition taken from the actual tool hash."""
    def __init__(self, executable):
        self.executable = executable

    def complete(self, request):
        results = {message.tool_result.name: message.tool_result for message in request.messages if message.tool_result}
        if any(result.error_code for result in results.values()):
            return ModelResponse(text='A tool rejected the operation; no automatic retry.')
        if 'files.inspect' not in results:
            return ModelResponse(tool_calls=(ToolCall('inspect-config', 'files.inspect', {'path': 'settings.txt'}),))
        if 'files.edit' not in results:
            digest = results['files.inspect'].value['sha256']
            return ModelResponse(tool_calls=(ToolCall('edit-config', 'files.edit', {
                'path': 'settings.txt', 'old_text': 'retries=2', 'new_text': 'retries=3', 'expected_sha256': digest}),))
        if 'commands.run' not in results:
            return ModelResponse(tool_calls=(ToolCall('verify-config', 'commands.run', {
                'argv': [self.executable, '-I', '-S', 'verify_fixture.py'], 'cwd': '.'}),))
        value = results['commands.run'].value
        return ModelResponse(text=json.dumps({'command_ok': value['ok'], 'exit_code': value['exit_code'],
                                             'verifier_output': value['stdout']}, ensure_ascii=False))


@contextmanager
def open_host(base):
    workspace = base/'workspace'
    executable = str(Path(getattr(sys, '_base_executable', sys.executable)).resolve())
    files = FilePlugin(workspace)
    editing = FileEditPlugin(files, base/'state/file-history')
    commands = CommandPlugin(workspace, allowed_executables=[executable], timeout=5, max_output_bytes=16384, revision='1')
    registry = ToolRegistry()
    plugins = PluginManager(registry)
    try:
        for plugin in (files, editing, commands):
            plugins.activate(plugin)
        registry.freeze()
        with Application(EditAndVerifyModel(executable), registry.tools(), database=base/'state/runs.sqlite3',
                         workspace=workspace, permissions=('files.read', 'files.write', 'commands.execute'),
                         mode='core-tools-scripted', limits={'max_model_calls': 4, 'max_tool_calls': 3, 'run_timeout': 30}) as app:
            yield app
    finally:
        commands.close()
        plugins.close()


def execute(base, approve):
    workspace = base/'workspace'; workspace.mkdir(); (base/'state').mkdir()
    target = workspace/'settings.txt'; target.write_bytes(ORIGINAL)
    (workspace/'verify_fixture.py').write_text(VERIFIER, encoding='utf-8')
    with open_host(base) as app:
        created = app.submit('将合成配置的重试次数从 2 改为 3，保留换行，再运行提供的本地验证程序。')
        waiting = app.wait(created['id'], timeout=15)
        require(waiting['status']=='waiting_approval' and waiting['approval']['name']=='files.edit', 'edit did not wait for approval')
        require(target.read_bytes()==ORIGINAL and not (base/'state/file-history').exists(), 'edit or backup occurred before approval')
        preview = app.preview_write(created['id'])
        require(preview['before'].encode('utf-8')==ORIGINAL and preview['after'].encode('utf-8')==EXPECTED, 'preview mismatch')
        key = waiting['approval']['key']
    with open_host(base) as app:
        restored = app.get_run(created['id'])
        require(restored['status']=='waiting_approval' and restored['approval']['key']==key, 'approval changed during reopen')
        app.approve(created['id'], approved=approve)
        after_edit = app.wait(created['id'], timeout=15)
        if not approve:
            require(after_edit['status']=='failed' and after_edit['error']=='approval_denied', 'denial was not respected')
            require(target.read_bytes()==ORIGINAL and after_edit['tool_calls']==1, 'a denied edit modified the file')
            final = after_edit; undo = None
        else:
            require(target.read_bytes()==EXPECTED, 'precise edit did not preserve the expected raw bytes')
            require(after_edit['status']=='waiting_approval' and after_edit['approval']['name']=='commands.run', 'command skipped approval')
            app.approve(created['id'])
            final = app.wait(created['id'], timeout=15)
            require(final['status']=='completed', 'command task did not finish')
            results = [message['tool_result'] for message in final['messages'] if message.get('tool_result')]
            edit = next(result['value'] for result in results if result['name']=='files.edit')
            command = next(result['value'] for result in results if result['name']=='commands.run')
            require(command['ok'] and command['exit_code']==0, 'a completed invocation was mistaken for command success')
            require(json.loads(command['stdout'])=={'language':'zh','retries':3,'crlf':True}, 'independent verifier output mismatch')
            require(edit['before_sha256']==hashlib.sha256(ORIGINAL).hexdigest() and edit['after_sha256']==hashlib.sha256(EXPECTED).hexdigest(), 'edit hashes mismatch')
            operation = app.submit_tool('files.restore', {'backup_id':edit['backup_id'], 'expected_sha256':edit['after_sha256']})
            require(app.wait(operation['id'])['status']=='waiting_approval' and target.read_bytes()==EXPECTED, 'restore skipped approval')
            app.approve(operation['id']); undo = app.wait(operation['id'])
            require(undo['status']=='completed' and not undo['tool_result']['error_code'] and target.read_bytes()==ORIGINAL, 'conditional restore failed')
        runs = app.list_runs(); trace = {run['id']:app.events(run['id']) for run in runs}
    summary = {'passed':True,'mode':'scripted_offline','approved':approve,'task_status':final['status'],
        'task_model_calls':final['model_calls'],'task_tool_calls':final['tool_calls'],'restore_tool_calls':0 if undo is None else undo['tool_calls'],
        'provider_requests':0,'subprocess_invocations':1 if approve else 0,'approval_survived_restart':True,
        'initial_sha256':hashlib.sha256(ORIGINAL).hexdigest(),'edited_sha256':hashlib.sha256(EXPECTED).hexdigest() if approve else None,
        'final_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'exact_original_restored':target.read_bytes()==ORIGINAL,
        'boundary':'Trusted synthetic local process; no filesystem or network sandbox and no real model quality claim.'}
    save(base/'runs.json', runs); save(base/'trace.json', trace); save(base/'summary.json', summary)
    return summary


def run_demo(output_dir=None, *, approve=True):
    if type(approve) is not bool:
        raise ValueError('approve must be boolean')
    if output_dir is None:
        with tempfile.TemporaryDirectory(prefix='astra-core-tools-') as directory:
            return {**execute(Path(directory), approve), 'retained':False}
    directory = Path(output_dir).absolute()
    directory.mkdir(parents=True, exist_ok=False)
    return {**execute(directory, approve), 'retained':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,help='New directory for synthetic fixtures and retained evidence.')
    parser.add_argument('--deny',action='store_true',help='Deny the edit and verify that no command runs.')
    args=parser.parse_args()
    print(json.dumps(run_demo(args.output_dir,approve=not args.deny),ensure_ascii=False,indent=2))


if __name__=='__main__': main()
