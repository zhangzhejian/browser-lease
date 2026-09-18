import base64
import functools
import http.server
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import urllib.request

import websocket

ROOT = Path.home() / 'browser-lease-trials' / time.strftime('%Y%m%d-%H%M%S')
ROOT.mkdir(parents=True)
CLI = str(Path.home() / '.local/bin/browser-lease')
prefix = 'trial-' + str(time.time_ns())
events = []
started = []
pages = []


def cli(*args, spec=None, expected=0):
    p = subprocess.run([CLI, *args], input=json.dumps(spec) if spec else None,
                       text=True, capture_output=True)
    result = json.loads(p.stdout)
    events.append({'command': list(args), 'exit_code': p.returncode, 'response': result})
    assert p.returncode == expected, result
    return result


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class Page:
    def __init__(self, connection):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(connection['cdp_url'] + '/json/list') as r:
            target = next(p for p in json.load(r) if p['type'] == 'page')
        self.ws = websocket.create_connection(target['webSocketDebuggerUrl'], suppress_origin=True, timeout=10)
        self.seq = 0

    def call(self, method, params=None):
        self.seq += 1
        self.ws.send(json.dumps({'id': self.seq, 'method': method, 'params': params or {}}))
        while True:
            r = json.loads(self.ws.recv())
            if r.get('id') == self.seq:
                assert 'error' not in r, r
                return r.get('result', {})

    def evaluate(self, expression):
        r = self.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True})
        assert 'exceptionDetails' not in r, r
        return r['result'].get('value')


web = ROOT / 'web'
web.mkdir()
(web / 'index.html').write_text('''<!doctype html><meta charset="utf-8"><title>Browser Lease Trial</title>
<style>body{font:24px system-ui;background:#122033;color:#eef;padding:70px}h1{font-size:42px}pre{padding:30px;background:#243650;border-radius:20px}a{color:#9ef}</style>
<h1>Browser Lease · 独立浏览器试用</h1><pre id="state">正在检查隔离…</pre><a id="download" href="sample.txt" download>下载本任务证据</a>''')
(web / 'sample.txt').write_text('browser-lease local trial download\n')
server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(web)))
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f'http://127.0.0.1:{server.server_port}/'
report = {'root': str(ROOT), 'checks': [], 'events': events}
try:
    specs = []
    rows = []
    for label in ('a', 'b'):
        spec = {'task': prefix + '-' + label, 'slot': prefix + '-worker-' + label,
                'agent_pid': os.getpid(), 'environment': prefix, 'account': 'visitor-' + label,
                'course_run': None, 'browser_data_dir': str(ROOT / label / 'profile'),
                'artifacts_dir': str(ROOT / label / 'artifacts'), 'control_mode': 'browser', 'headless': False}
        specs.append(spec)
        (ROOT / ('request-' + label + '.json')).write_text(json.dumps(spec, indent=2))
        row = cli('register', '--file', '-', spec=spec)['data']
        rows.append(row)
        started.append(spec['task'])
        page = Page(row['connection']); pages.append(page)
        page.call('Page.navigate', {'url': url})
        for _ in range(100):
            if page.evaluate('document.title') == 'Browser Lease Trial': break
            time.sleep(.1)
        assert page.evaluate('document.title') == 'Browser Lease Trial'
        page.evaluate(f"document.cookie='worker={label}; Path=/'; localStorage.setItem('worker','{label}'); document.getElementById('state').textContent='Task {label.upper()}\\nCookie: '+document.cookie+'\\nLocal storage: '+localStorage.getItem('worker')")
        shot = page.call('Page.captureScreenshot', {'format': 'png'})
        (Path(spec['artifacts_dir']) / 'browser.png').write_bytes(base64.b64decode(shot['data']))
    assert rows[0]['browser_identity'] != rows[1]['browser_identity']
    assert rows[0]['connection']['cdp_url'] != rows[1]['connection']['cdp_url']
    for label, page in zip(('a','b'), pages):
        assert page.evaluate('document.cookie') == 'worker=' + label
        assert page.evaluate("localStorage.getItem('worker')") == label
    report['checks'].append('两个可见 Chrome：进程、连接端口、Cookie、本地存储独立')
    pages[0].evaluate("document.getElementById('download').click()")
    download = Path(rows[0]['downloads_dir']) / 'sample.txt'
    for _ in range(100):
        if download.exists(): break
        time.sleep(.1)
    assert download.read_text() == 'browser-lease local trial download\n'
    assert not (Path(rows[1]['downloads_dir']) / 'sample.txt').exists()
    report['checks'].append('A 下载仅出现在 A 的证据目录')
    bad = {**specs[1], 'task': prefix + '-conflict', 'slot': prefix + '-conflict',
           'account': specs[0]['account'], 'browser_data_dir': str(ROOT / 'conflict/profile'),
           'artifacts_dir': str(ROOT / 'conflict/artifacts')}
    conflict = cli('register', '--file', '-', spec=bad, expected=3)
    assert conflict['code'] == 'resource_conflict'
    assert not Path(bad['browser_data_dir']).exists()
    report['checks'].append('同环境账号冲突被拒绝，退出码 3，未创建浏览器目录')
    listing = cli('list', '--active', '--probe', '--environment', prefix)['data']
    assert listing['total'] == 2
    assert all(r['health'] == 'reachable' for r in listing['tasks'])
    pages[0].ws.close()
    restored = cli('reconnect', started[0], '--agent-pid', str(os.getpid()))['data']
    reconnected = Page(restored['connection']); pages.append(reconnected)
    assert reconnected.evaluate("localStorage.getItem('worker')") == 'a'
    report['checks'].append('新命令查询与断开后重连成功，原页面状态保留')
    reconnected.ws.close()
    cli('stop', started[0])
    assert pages[1].evaluate("localStorage.getItem('worker')") == 'b'
    assert cli('show', started[1], '--probe')['data']['health'] == 'reachable'
    report['checks'].append('关闭 A 后，B 仍可操作')
    report['ok'] = True
finally:
    for page in pages:
        page.ws.close()
    for task in started:
        cli('stop', task)
        cli('purge-profile', task)
    server.shutdown()
    remaining = cli('list', '--active', '--environment', prefix)['data']['total']
    assert remaining == 0
    report['checks'].append('本次 Chrome 与临时 profile 已回收，截图、下载、任务记录保留')
    (ROOT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'ok': report.get('ok', False), 'report': str(ROOT / 'report.json'), 'checks': report['checks']}, ensure_ascii=False, indent=2))
