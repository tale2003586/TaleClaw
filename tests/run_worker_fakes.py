"""Spawn-importable, offline targets for the background run supervisor tests."""
import os
from pathlib import Path
import subprocess
import sys
import time


def run_fake(request, emit, cancelled):
    content = request['content']
    if content == 'error':
        raise RuntimeError('fake primary model failure')
    if content.startswith('background:'):
        marker = content.split(':', 1)[1]
        subprocess.Popen([sys.executable, '-c', 'import time,pathlib; time.sleep(0.8); pathlib.Path(' + repr(marker) + ').write_text("orphan")'], start_new_session=True)
        return {'reply': 'done', 'status': 'completed'}
    if content.startswith('crash:'):
        marker = content.split(':', 1)[1]
        child = subprocess.Popen([sys.executable, '-c',
            'import time,pathlib; time.sleep(0.8); pathlib.Path(' + repr(marker) + ').write_text("orphan")'],
            start_new_session=True)
        emit({'type': 'event', 'event': 'child.started', 'pid': child.pid})
        time.sleep(0.12)
        os._exit(17)
    if content.startswith('wait:'):
        release = Path(content.split(':', 1)[1])
        emit({'type': 'delta', 'text': 'started'})
        while not release.exists():
            if cancelled.is_set():
                return {'reply': 'cancelled', 'status': 'stopped'}
            time.sleep(0.01)
    if content == 'ignore-cancel':
        emit({'type': 'delta', 'text': 'started'})
        while True:
            time.sleep(0.02)
    emit({'type': 'delta', 'text': content})
    emit({'type': 'thinking', 'text': 'thinking'})
    return {'reply': 'reply:' + content, 'status': 'completed'}
