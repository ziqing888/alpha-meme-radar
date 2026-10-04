"""Loopback-only terminal; execution controls require explicit operator requests."""
import argparse
from datetime import datetime
import hmac
import json
import mimetypes
import re
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from alpha_live_dashboard import Handler, ROOT, RoundtripControl, allowed_request, valid_post
from alpha_terminal_control import TerminalControl
from alpha_terminal_control import _amount
from alpha_terminal_execution import ExecutionControl
from alpha_terminal_data import terminal_snapshot
from alpha_system_health import build_system_health

DIST = Path(__file__).with_name('terminal-ui') / 'dist'
SESSION_PATH = Path(__file__).resolve().parents[2] / 'outputs' / 'terminal-v2-session.json'


def _session_time(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp if stamp.utcoffset() is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _read_session(path):
    if path is None:
        return None
    try:
        path = Path(path)
        if not path.is_file() or path.stat().st_size > 16384:
            return None
        raw = json.loads(path.read_text(encoding='utf-8'))
        starts = raw.get('started_at') if isinstance(raw, dict) else None
        if (raw.get('schema_version') != 1 or raw.get('strategy_version') != 'chain_v2'
                or not isinstance(starts, dict)):
            return None
        parsed = {chain: _session_time(starts.get(chain)) for chain in ('bsc', 'robinhood')}
        if any(value is None for value in parsed.values()):
            return None
        return {'strategy_version': 'chain_v2',
                'started_at': {chain: parsed[chain].isoformat() for chain in parsed},
                '_parsed': parsed}
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return None


def _apply_session_window(result, session):
    if session is None:
        return result
    starts = session['_parsed']

    def current(row):
        chain = row.get('chain')
        stamp = _session_time(row.get('time'))
        return chain in starts and stamp is not None and stamp >= starts[chain]

    result['orders'] = [row for row in result.get('orders', []) if current(row)]
    result['events'] = [row for row in result.get('events', [])
                        if row.get('type') != 'order' or current(row)]
    result['session'] = {key: value for key, value in session.items() if key != '_parsed'}
    return result


def _apply_session_pnl(result, session):
    if session is None:
        return result
    orders = result.get('orders') if isinstance(result.get('orders'), list) else []
    for chain in result.get('chains', []):
        name = chain.get('chain')
        if name not in session['_parsed'] or chain.get('ledger_identity_status') != 'verified':
            continue
        sells = [row for row in orders if row.get('chain') == name
                 and row.get('side') == 'sell' and row.get('status') == 'filled']
        values = []
        for row in sells:
            value = row.get('pnl_native_atomic')
            if value is None and isinstance(row.get('accounting'), dict):
                value = row['accounting'].get('pnl_native_atomic')
            try:
                values.append(int(value))
            except (ValueError, TypeError):
                pass
        external_pending = int(chain.get('unreconciled_external_sales') or 0)
        external_reconciled = int(chain.get('manual_reconciled_sales') or 0)
        pending = len(sells) - len(values) + external_pending + external_reconciled
        chain['realized_pnl_pending_count'] = pending
        chain['unverified_sell_fills'] = len(sells) - len(values)
        chain['unreconciled_external_sales'] = external_pending
        chain['manual_reconciled_sales'] = external_reconciled
        if pending:
            chain['realized_pnl_native_atomic'] = None
            chain['realized_pnl_native'] = None
            chain['realized_pnl_status'] = 'pending_verification'
            chain['realized_pnl_method'] = None
            chain['manual_reconciliation'] = {
                'status': 'unknown', 'count': None, 'reconciled_count': 0,
            }
        else:
            total = sum(values)
            chain['realized_pnl_native_atomic'] = str(total)
            chain['realized_pnl_native'] = total / 10 ** 18
            chain['realized_pnl_status'] = 'verified'
            chain['realized_pnl_method'] = 'chain_v2_session_receipt_fifo_v1'
            chain['manual_reconciliation'] = {
                'status': 'clear', 'count': 0, 'reconciled_count': 0,
            }
    return result


class TerminalService:
    def __init__(self, controller=None, balances=None, accounting=None, session_path=None):
        self.controller = controller or ExecutionControl()
        self.balances = balances
        self.accounting = accounting
        self.session_path = Path(session_path) if session_path is not None else None
        self.lock = threading.Lock()
        self.runtime_at = 0
        self.runtime_data = None

    def runtime(self):
        with self.lock:
            if self.runtime_data is None or time.monotonic() - self.runtime_at > 4:
                raw = self.controller.runtime()
                self.runtime_data = {'runtime': raw.get('strategies', []),
                                     'verified': raw.get('ok', False),
                                     'verified_at': raw.get('verified_at'),
                                     'controls': raw.get('controls', [])}
                self.runtime_data['unassigned_workers'] = sum(
                    row.get('chain') not in {'bsc', 'robinhood'}
                    for row in self.runtime_data['runtime'])
                if self.runtime_data['unassigned_workers']:
                    self.runtime_data['verified'] = False
                self.runtime_at = time.monotonic()
            result = dict(self.runtime_data)
            drafts = self.drafts()
            configured = {row['chain']: row.get('amount_usd') for row in drafts}
            result['runtime'] = [
                {**row, 'configured_amount_usd': configured.get(
                    row.get('chain'), row.get('configured_amount_usd'))}
                for row in self.runtime_data['runtime']
            ]
            result['drafts'] = drafts
            return result

    def drafts(self):
        path = getattr(self.controller, 'requests_path', None)
        if path is None or not path.is_file():
            return []
        try:
            if path.stat().st_size > 65536:
                return []
            raw = json.loads(path.read_text(encoding='utf-8'))
            strategies = raw.get('strategies', {})
            return [{'chain': chain, 'amount_usd': _amount(item.get('amount_usd')),
                     'requested_at': str(item.get('requested_at', ''))[:60]}
                    for chain, item in strategies.items()
                    if chain in {'bsc', 'robinhood'} and isinstance(item, dict)]
        except (OSError, ValueError, AttributeError):
            return []

    def snapshot(self, root):
        result = terminal_snapshot(root=root)
        result['system_health'] = build_system_health(SESSION_PATH.parent)
        session = _read_session(self.session_path)
        _apply_session_window(result, session)
        effective = {
            row.get('chain'): row.get('effective_strategy')
            for row in self.runtime().get('runtime', [])
            if row.get('chain') in {'bsc', 'robinhood'}
        }
        for chain in result['chains']:
            chain['effective_strategy'] = effective.get(chain.get('chain'))
        for key in ('positions', 'orders', 'candidates'):
            for item in result[key]:
                item['symbol'] = item.get('symbol') or (item.get('token') or 'Unknown')[:10]
        for item in result['candidates']:
            item['first_mcap_usd'] = item.pop('first_mcap', None)
        for event in result['events']:
            event['message'] = event.get('reason') or event.get('status') or event.get('type')
        result['balances'] = self.balances.snapshot(result['wallet']) if self.balances else []
        result = self.accounting.enrich_snapshot(result) if self.accounting else result
        return _apply_session_pnl(result, session)


def parse_payload(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_key')
            result[key] = value
        return result

    def invalid(_):
        raise ValueError('invalid_constant')

    value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError('invalid_payload')
    return value


class TerminalHandler(Handler):
    def respond(self, code, body, content_type):
        self.send_response(code)
        headers = {'Content-Type': content_type, 'Content-Length': str(len(body)),
                   'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                   'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
                   'Cross-Origin-Resource-Policy': 'same-origin',
                   'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"}
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if (len(self.headers.get_all('Host', [])) != 1 or
                not allowed_request(self.headers.get('Host', ''), self.headers.get('Origin'),
                                    self.client_address[0], self.server.server_port) or
                self.headers.get('Sec-Fetch-Site') == 'cross-site'):
            self.json_response(403, {'error': 'request_forbidden'})
            return
        path = urlsplit(self.path).path
        try:
            if path == '/api/terminal/session':
                self.json_response(200, {'csrf_token': self.server.control.csrf_token})
            elif path == '/api/terminal/snapshot':
                self.json_response(200, self.server.terminal.snapshot(self.server.control.root))
            elif path == '/api/terminal/runtime':
                self.json_response(200, self.server.terminal.runtime())
            elif path in {'/status.json', '/roundtrip/status.json'}:
                super().do_GET()
            else:
                relative = unquote(path).lstrip('/') or 'index.html'
                target = (self.server.dist / relative).resolve()
                if not target.is_relative_to(self.server.dist.resolve()) or not target.is_file():
                    self.json_response(404, {'error': 'not_found'})
                    return
                mime = {'.js': 'text/javascript', '.css': 'text/css'}.get(target.suffix)
                self.respond(200, target.read_bytes(), mime or mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
        except (OSError, ValueError, TypeError):
            self.json_response(503, {'error': 'data_unavailable'})

    def do_POST(self):
        actions = {'/api/terminal/wallet/import': 'wallet/import',
                   '/api/terminal/strategy/config': 'strategy/config',
                   '/api/terminal/strategy/command': 'strategy/command'}
        if self.path not in actions:
            self.json_response(404, {'error': 'not_found'})
            return
        csrf = self.headers.get('X-CSRF-Token', '')
        if (any(len(self.headers.get_all(key, [])) != 1 for key in ('Host', 'Origin', 'X-CSRF-Token')) or
                not valid_post(self.headers.get('Host'), self.headers.get('Origin'),
                               self.client_address[0], self.server.server_port,
                               self.headers.get('Sec-Fetch-Site')) or
                not csrf.isascii() or not hmac.compare_digest(csrf, self.server.control.csrf_token)):
            self.json_response(403, {'error': 'request_forbidden'})
            return
        if (self.headers.get('Transfer-Encoding') is not None or
                any(len(self.headers.get_all(key, [])) != 1 for key in ('Content-Length', 'Content-Type')) or
                not re.fullmatch(r'[0-9]{1,6}', self.headers.get('Content-Length', ''))):
            self.json_response(400, {'error': 'request_invalid'})
            return
        length = int(self.headers['Content-Length'])
        if not 0 < length <= 2048:
            self.json_response(413, {'error': 'request_too_large'})
            return
        if self.headers['Content-Type'].split(';')[0].strip().lower() != 'application/json':
            self.json_response(415, {'error': 'json_required'})
            return
        try:
            self.connection.settimeout(3)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError()
            payload = parse_payload(raw)
        except (OSError, ValueError, RecursionError):
            self.json_response(400, {'error': 'request_invalid'})
            return
        code, value = self.server.terminal.controller.dispatch(actions[self.path], payload)
        if hasattr(self.server.terminal, 'runtime_at'):
            self.server.terminal.runtime_at = 0
        self.json_response(code, value)


def make_server(port=8771, root=ROOT, service=None, dist=DIST):
    server = ThreadingHTTPServer(('127.0.0.1', port), TerminalHandler)
    server.control = RoundtripControl(root)
    server.terminal = service or TerminalService(controller=ExecutionControl(config_root=root))
    server.dist = Path(dist)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8771)
    args = parser.parse_args()
    if not (DIST / 'index.html').is_file():
        raise SystemExit('Build terminal-ui first: npm run build')
    from alpha_terminal_balances import Collector
    from alpha_terminal_accounting import Collector as AccountingCollector
    service = TerminalService(balances=Collector(), accounting=AccountingCollector(),
                              session_path=SESSION_PATH)
    server = make_server(args.port, service=service)
    print(f'Terminal ready: http://127.0.0.1:{args.port}/', flush=True)
    try:
        server.serve_forever()
    finally:
        service.balances.close()
        service.accounting.close()
        server.server_close()


if __name__ == '__main__':
    main()
