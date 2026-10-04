import base64
import json
from types import SimpleNamespace

import pytest

from server import alpha_remote_sync as client
from server import alpha_sync_bridge as bridge


def b64(value):
    return base64.b64encode(json.dumps(value).encode()).decode()


def test_remote_bridge_validates_and_atomically_publishes_input(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, 'ROOT', tmp_path)
    monkeypatch.setattr(bridge.os, 'chown', lambda *args: None, raising=False)
    payload = {'signals': [], 'quotes': [], 'updated_at': '2026-09-09T00:00:00Z'}

    bridge.write_input('robinhood', b64(payload))

    assert json.loads((tmp_path / 'robinhood-execution-input.json').read_text()) == payload
    with pytest.raises(ValueError, match='invalid_input_schema'):
        bridge.write_input('robinhood', b64({'signals': []}))


def test_persistent_client_cycle_publishes_verified_remote_state(tmp_path, monkeypatch):
    monkeypatch.setattr(client, 'OUTPUTS', tmp_path)
    (tmp_path / 'robinhood-execution-input.json').write_text(json.dumps({
        'signals': [], 'quotes': [], 'updated_at': '2026-09-09T00:00:00Z'}))
    state = {'chain': 'robinhood', 'positions': {}, 'terminal_pending': 0}
    status = {'status': 'waiting_for_strategy_candidate', 'terminal_control': {'pid': 99}}
    response = json.dumps({'ok': True, 'chain': 'robinhood',
                           'state_b64': b64(state), 'status_b64': b64(status)}) + '\n'

    class Input:
        value = ''
        def write(self, value):
            self.value += value
        def flush(self):
            pass

    class Output:
        def readline(self):
            return response

    process = SimpleNamespace(stdin=Input(), stdout=Output(), poll=lambda: None)
    latency = client.cycle(process, SimpleNamespace(chain='robinhood'))

    request = json.loads(process.stdin.value)
    assert request['chain'] == 'robinhood'
    assert json.loads(base64.b64decode(request['input_b64']))['signals'] == []
    assert json.loads((tmp_path / 'okx-dex-sdk-robinhood-live-state.json').read_text()) == state
    assert json.loads((tmp_path / 'okx-dex-sdk-robinhood-live-status.json').read_text()) == status
    assert latency >= 0
