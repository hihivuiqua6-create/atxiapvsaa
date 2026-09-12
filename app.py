#!/usr/bin/env python
import asyncio
import importlib.util
import io
import json
import logging
import os
import threading
import time
import urllib.parse
from datetime import datetime

import requests
import websockets
from flask import Flask, jsonify, render_template_string
from flask_cors import CORS
from websockets.asyncio.server import serve

PORT = int(os.environ.get('PORT') or os.environ.get('APP_PORT') or 5000)

_FILES = {
    'g789': '789.py',
    'gb52': 'b52.py',
    'gbetvip': 'betvip.py',
    'ghitclub': 'hitclub.py',
    'glc79': 'lc79.py',
    'gluck8': 'luck8.py',
    'gmax789': 'max789.py',
    'grikvip': 'rikvip.py',
    'gsao789': 'sao789.py',
    'gson789': 'son789.py',
    'gsumclub': 'sumclub.py',
    'gsunwin': 'sunwin.py',
    'gxocdia88': 'xocdia88.py',
}

_DATA_DIR = os.environ.get('DATA_DIR') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
MAX_ENTRIES = 100
_storage_lock = threading.Lock()


def _storage_path(name):
    base = name.replace('/', '-').replace('\\', '-').replace('..', '')
    return os.path.join(_DATA_DIR, base + '.json')


def load(name, default=None):
    """Đọc list lịch sử từ file data/{name}.json (mới->cũ)."""
    if default is None:
        default = []
    try:
        with _storage_lock:
            if not os.path.exists(_storage_path(name)):
                return default
            with open(_storage_path(name), 'r', encoding='utf-8') as f:
                data = json.load(f)
        return data if isinstance(data, list) else default
    except Exception:
        return default


def save(name, entries):
    """Ghi list lịch sử lên file data/{name}.json (cap MAX_ENTRIES, atomic)."""
    entries = (entries or [])[:MAX_ENTRIES]
    os.makedirs(_DATA_DIR, exist_ok=True)
    try:
        with _storage_lock:
            tmp = _storage_path(name) + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(entries, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _storage_path(name))
    except Exception:
        pass


_base = os.path.dirname(os.path.abspath(__file__))
for _alias, _fname in _FILES.items():
    _path = os.path.join(_base, 'core', _fname)
    _name = 'core_' + _fname.replace('.py', '').replace(' ', '_')
    _spec = importlib.util.spec_from_file_location(_name, _path)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _mod.load = load
    _mod.save = save
    _mod._sload = load
    _mod._ssave = save
    globals()[_alias] = _mod

app = Flask(__name__)
CORS(app)
app.json.sort_keys = False

MODULES = [
    globals()['g789'], globals()['gb52'], globals()['gbetvip'],
    globals()['ghitclub'], globals()['glc79'], globals()['gluck8'],
    globals()['gmax789'], globals()['grikvip'], globals()['gsao789'],
    globals()['gson789'], globals()['gsumclub'], globals()['gsunwin'],
    globals()['gxocdia88'],
]

GAME_TX = {
    '789': '/789/api/sicbo',
    'b52': '/b52/api/tx',
    'betvip': '/betvip/api/tx',
    'hitclub': '/hitclub/api/sicbo',
    'lc79': '/lc79/api/tx',
    'luck8': '/luck8/api/txmd5',
    'max789': '/max789/api/tx',
    'rikvip': '/rikvip/api/sicbo',
    'sao789': '/sao789/api/md5',
    'son789': '/son789/api/tx',
    'sumclub': '/sumclub/api/md5',
    'sunwin': '/sunwin/api/sicbo',
    'xocdia88': '/xocdia88/api/tx',
}

for mod in MODULES:
    try:
        app.register_blueprint(mod.bp)
    except Exception as e:
        print(f"[app] Lỗi register {mod.__name__}: {e}")

STATUS = {}


def check_game(name, path):
    url = 'http://127.0.0.1:%d%s' % (PORT, path)
    st = {'status': 'unknown', 'code': None, 'data': None, 'checked': '', 'last_update': ''}
    try:
        r = requests.get(url, timeout=5)
        st['code'] = r.status_code
        if r.status_code == 200:
            try:
                j = r.json()
            except Exception:
                j = None
            st['data'] = j
            if isinstance(j, dict):
                phien = j.get('phien')
                ket_qua = j.get('ket_qua')
                lu = j.get('last_update') or j.get('thoi_gian') or j.get('updateTime') or j.get('time')
                has_data = bool(phien is not None or ket_qua)
            else:
                has_data = True
            if has_data and lu:
                try:
                    t = datetime.fromisoformat(str(lu).replace('Z', '+00:00'))
                    now = datetime.now(t.tzinfo) if t.tzinfo else datetime.now()
                    age = max(0, (now - t).total_seconds())
                    st['last_update'] = lu
                    st['age'] = round(age)
                    st['status'] = 'ok' if age < 30 else ('cham' if age < 120 else 'ngung')
                except Exception:
                    st['status'] = 'ok'
            elif has_data and not lu:
                st['status'] = 'ok'
            else:
                st['status'] = 'idle'
        else:
            st['status'] = 'error'
    except Exception as e:
        st['status'] = 'error'
        st['data'] = str(e)
    st['checked'] = datetime.now().strftime('%H:%M:%S')
    STATUS[name] = st


def monitor_loop():
    while True:
        for name, path in GAME_TX.items():
            try:
                check_game(name, path)
            except Exception:
                pass
        time.sleep(8)


@app.route('/')
def dashboard():
    def friendly(name):
        return name.replace('game', '')
    rows = []
    for mod in MODULES:
        endpoints = []
        for rule in app.url_map.iter_rules():
            if rule.endpoint.startswith(mod.bp.name + '.'):
                endpoints.append((str(rule), list(rule.methods - {'OPTIONS', 'HEAD'})))
        name = friendly(mod.bp.name)
        st = STATUS.get(name, {})
        rows.append({
            'name': name,
            'bp': mod.bp.name,
            'status': st.get('status', 'chưa cập nhật'),
            'age': st.get('age'),
            'last_update': st.get('last_update', ''),
            'checked': st.get('checked', ''),
            'endpoints': sorted(endpoints),
        })
    html = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8"><title>Core API</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="8">
<style>
body{font-family:Segoe UI,Arial,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:24px}
h1{color:#38bdf8}
.wrap{display:flex;flex-wrap:wrap;gap:16px}
.card{background:#1e293b;border-radius:10px;padding:16px;min-width:320px;flex:1}
.card h2{margin:0 0 10px;color:#f8fafc;font-size:16px;display:flex;justify-content:space-between;align-items:center}
table{border-collapse:collapse;width:100%}
td,th{padding:6px 8px;border-bottom:1px solid #334155;text-align:left;font-size:13px}
a{color:#7dd3fc;text-decoration:none}a:hover{text-decoration:underline}
.code{font-family:monospace;font-size:12px}
.small{font-size:11px;color:#94a3b8}
.badge{padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.ok{background:#16a34a;color:#fff}
.cham{background:#d97706;color:#fff}
.ngung{background:#dc2626;color:#fff}
.error{background:#b91c1c;color:#fff}
.idle{background:#475569;color:#fff}
.unknown{background:#64748b;color:#fff}
.summary{display:flex;gap:20px;margin-bottom:16px;font-size:14px}
</style></head>
<body>
<h1>🎰 Core API Dashboard — <span class="small">1 port :{{ port }} · auto-refresh 8s</span></h1>
<div class="summary">
<span>✅ Hoạt động: <b style="color:#4ade80">{{ stats.ok }}</b></span>
<span>⚠️ Chậm/Ngừng: <b style="color:#facc15">{{ stats.cham + stats.ngung }}</b></span>
<span>🔴 Bị lỗi: <b style="color:#f87171">{{ stats.error }}</b></span>
<span>⚪ Chưa có dữ liệu: <b>{{ stats.idle }}</b></span>
</div>
<div class="wrap">
{% for r in rows %}
<div class="card">
<h2>🎲 {{ r.name }}
{% if r.status == 'ok' %}<span class="badge ok">● Đang hoạt động</span>
{% elif r.status == 'cham' %}<span class="badge cham">● Chậm ({{ r.age }}s)</span>
{% elif r.status == 'ngung' %}<span class="badge ngung">● Ngừng ({{ r.age }}s)</span>
{% elif r.status == 'idle' %}<span class="badge idle">● Chưa có dữ liệu</span>
{% elif r.status == 'error' %}<span class="badge error">● Bị lỗi</span>
{% else %}<span class="badge unknown">● {{ r.status }}</span>{% endif %}
</h2>
<p class="small">Cập nhật: {{ r.last_update }} · kiểm tra lúc {{ r.checked }}</p>
<table>
{% for url, methods in r.endpoints %}
<tr><td class="code"><a href="{{ url }}" target="_blank">{{ url }}</a></td><td class="small">{{ methods|join(', ') }}</td></tr>
{% endfor %}
</table>
</div>
{% endfor %}
</div>
</body></html>"""
    return render_template_string(html, rows=rows, port=PORT, stats=_stats())


def _stats():
    s = {'ok': 0, 'cham': 0, 'ngung': 0, 'idle': 0, 'error': 0, 'unknown': 0}
    for name, path in GAME_TX.items():
        st = STATUS.get(name, {})
        k = st.get('status', 'unknown')
        if k in s:
            s[k] += 1
        else:
            s['unknown'] += 1
    return s


@app.route('/api/status')
def status():
    out = {}
    for name, path in GAME_TX.items():
        out[name] = {
            'status': STATUS.get(name, {}).get('status', 'unknown'),
            'code': STATUS.get(name, {}).get('code'),
            'age': STATUS.get(name, {}).get('age'),
            'last_update': STATUS.get(name, {}).get('last_update', ''),
            'checked': STATUS.get(name, {}).get('checked', ''),
            'endpoint': path,
            'ws': 'ws://localhost:%d/ws/%s' % (PORT, name),
        }
    return jsonify(out)


@app.route('/healthz')
def healthz():
    return jsonify({'ok': True, 'time': datetime.now().isoformat(), 'stats': _stats()})


@app.route('/api/ws')
def ws_info():
    return jsonify({
        'protocol': 'ws',
        'port': PORT,
        'base': 'ws://localhost:%d' % PORT,
        'endpoints': [
            {'game': name, 'url': 'ws://localhost:%d/ws/%s' % (PORT, name), 'http': path}
            for name, path in GAME_TX.items()
        ],
    })


def get_current(name):
    """Trả về snapshot dữ liệu hiện tại của một game (từ biến module)."""
    try:
        mod = globals().get('g' + name)
        if mod is None:
            return None
        if name == 'luck8':
            return {'tx': mod.latest.get('TaixiuMd5', {})}
        if name == 'sunwin':
            return mod.__dict__.get('current_result', {}) and (
                {'tx': getattr(mod, 'current_result', {}), 'sicbo': getattr(mod, 'sicbo_result', {})})
        if name == '789':
            return {'tx': getattr(mod, 'tx_data', {}), 'sicbo': getattr(mod, 'sicbo_data', {})}
        d = {}
        for key in ('latest_result', 'latest_result_md5'):
            v = getattr(mod, key, None)
            if v is not None:
                d[key] = v
        sicbo = getattr(mod, 'latest_sicbo', None)
        if sicbo is not None:
            d['sicbo'] = sicbo
        if name == 'rikvip':
            d = {'hu': getattr(mod, 'latest_result', {}), 'md5': getattr(mod, 'latest_result_md5', {}),
                 'sicbo': getattr(mod, 'latest_sicbo', {})}
        if d:
            return d
        if hasattr(mod, 'tx_data'):
            return {'tx': mod.tx_data}
        return None
    except Exception:
        return None


def _wsgi_call(environ):
    """Gọi Flask app (WSGI) đồng bộ, trả (status, headers, body)."""
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured['status'] = status
        captured['headers'] = headers

    body = b''
    status = '200 OK'
    headers = []
    try:
        result = app.wsgi_app(environ, start_response)
        for chunk in result:
            if chunk:
                body += chunk
        status = captured.get('status', '200 OK')
        headers = captured.get('headers', [])
    finally:
        try:
            result.close()
        except Exception:
            pass
    return status, headers, body


def _build_environ(path, query, headers_map):
    env = {
        'REQUEST_METHOD': 'GET',
        'SCRIPT_NAME': '',
        'PATH_INFO': urllib.parse.unquote(path),
        'QUERY_STRING': query or '',
        'SERVER_NAME': 'localhost',
        'SERVER_PORT': str(PORT),
        'SERVER_PROTOCOL': 'HTTP/1.1',
        'wsgi.version': (1, 0),
        'wsgi.url_scheme': 'http',
        'wsgi.input': io.BytesIO(b''),
        'wsgi.errors': io.StringIO(),
        'wsgi.multithread': True,
        'wsgi.multiprocess': False,
        'wsgi.run_once': False,
        'HTTP_HOST': 'localhost:%d' % PORT,
        'HTTP_ACCEPT': headers_map.get('accept', '*/*'),
        'HTTP_USER_AGENT': headers_map.get('user-agent', 'CoreBridge'),
        'REMOTE_ADDR': '127.0.0.1',
        'REMOTE_PORT': '0',
    }
    for k, v in headers_map.items():
        kk = k.replace('-', '_').upper()
        if kk not in ('HOST', 'CONNECTION', 'UPGRADE'):
            env['HTTP_' + kk] = v
    return env


def _serve_http(path, query, headers_map):
    """Serve yêu cầu HTTP thường qua Flask WSGI -> Response."""
    from websockets.http11 import Response
    from websockets.datastructures import Headers as WSHeaders
    env = _build_environ(path, query, headers_map)
    status, hdrs, body = _wsgi_call(env)
    code = int(status.split(' ')[0])
    reason = ' '.join(status.split(' ')[1:]) or 'OK'
    wh = WSHeaders()
    for hn, hv in hdrs:
        hn_l = hn.lower()
        if hn_l in ('content-length', 'transfer-encoding'):
            continue
        wh[hn] = hv
    wh['Content-Length'] = str(len(body))
    return Response(code, reason, wh, body)


async def process_request(conn, request):
    """Xử lý trước: nếu KHÔNG phải /ws/ thì trả HTTP response qua Flask."""
    full = request.path
    qs = ''
    path = full
    if '?' in full:
        path, qs = full.split('?', 1)
    path_u = urllib.parse.unquote(path)
    if path_u.startswith('/ws/'):
        return None  # tiếp tục mở websocket
    headers_map = {k.lower(): v for k, v in request.headers.items()}
    resp = _serve_http(path_u, qs, headers_map)
    return resp


async def ws_handler(conn):
    path = getattr(conn, 'request', None).path if getattr(conn, 'request', None) else '/ws/'
    try:
        seg = path.partition('/ws/')[2].split('/')[0].lower()
        seg = seg.split('?')[0]
    except Exception:
        seg = ''
    data_name = seg
    if not data_name or not globals().get('g' + data_name):
        await conn.send(json.dumps({"error": "game khong hop le", "games": list(GAME_TX.keys())}))
        await conn.close()
        return
    last_sig = {}
    while True:
        try:
            snap = get_current(data_name)
            sig = repr(snap)
            new = sig != last_sig.get(data_name)
            last_sig[data_name] = sig
            if new or True:
                await conn.send(json.dumps({"game": data_name, "data": snap}))
        except Exception as e:
            try:
                await conn.send(json.dumps({"error": str(e)}))
            except Exception:
                break
        await asyncio.sleep(2)


# Giảm log nhiễu: mọi request HTTP thường đều bị websockets ghi là "handshake failed"
logging.getLogger('websockets.server').setLevel(logging.CRITICAL)


def main():
    threading.Thread(target=monitor_loop, daemon=True).start()
    for mod in MODULES:
        try:
            mod.start_background()
        except Exception as e:
            print(f"[app] start_background {mod.__name__} lỗi: {e}")
    print('=' * 50)

    async def _run():
        async with serve(
            ws_handler,
            host='0.0.0.0',
            port=PORT,
            process_request=process_request,
            ping_interval=20,
            ping_timeout=20,
            max_size=8 * 1024 * 1024,
        ) as server:
            print(f'🚀 Core API (WS + HTTP) chạy tại:')
            print(f'   http://127.0.0.1:{PORT}')
            print(f'   ws://127.0.0.1:{PORT}/ws/<game>')
            await server.serve_forever()

    print('=' * 50)
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print('\n👋 Stopped')


if __name__ == '__main__':
    main()