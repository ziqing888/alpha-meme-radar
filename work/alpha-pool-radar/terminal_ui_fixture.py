"""Disposable browser-test service. No credentials, RPC, or execution imports."""
import argparse
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from alpha_terminal_server import make_server


class Fixture:
    def __init__(self):
        self.controller = self
        self.running = True
        self.amount = '5'
        self.exit_only = False
        self.state = 'ready'
        self.request_id = None
        self.wallet = '0x' + '1' * 40

    def runtime(self):
        return {'verified': True, 'controls': [{'chain': 'bsc', 'supported': self.running, 'state': self.state, 'request_id': self.request_id}],
                'runtime': [{'chain': 'bsc', 'pid': 123, 'running': True, 'amount_usd': self.amount,
                             'exit_only': self.exit_only}] if self.running else [], 'drafts': []}

    def snapshot(self, root):
        return {'updated_at': datetime.now(timezone.utc).isoformat(), 'wallet': self.wallet,
                'positions': [], 'orders': [], 'candidates': [], 'events': [], 'balances': [],
                'chains': [{'chain': 'bsc', 'state': 'running' if self.running else 'stopped'}]}

    def dispatch(self, route, payload):
        if route != 'strategy/command':
            return 400, {'error': 'fixture_command_only'}
        action = payload.get('action')
        if action == 'apply':
            self.amount = payload['amount_usd']
        elif action in {'pause', 'resume'}:
            self.exit_only = action == 'pause'
        elif action in {'start', 'stop'}:
            self.running = action == 'start'
        else:
            return 400, {'error': 'unknown_action'}
        self.state = 'applied'
        self.request_id = payload['request_id']
        return 202, {'ok': True, 'state': 'submitted', 'request_id': self.request_id}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8773)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='terminal-ui-fixture-') as root:
        server = make_server(args.port, root=Path(root), service=Fixture())
        server.serve_forever()
