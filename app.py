#!/usr/bin/env python
"""Core API - 1 cổng cho HTTP + WebSocket, chạy được trên Render.

Điểm khác bản cũ (các lỗi chậm / không load / null đã fix):
  * HTTP (Flask WSGI) được chạy trong thread pool -> không còn khóa event loop
    của websockets, nên dashboard/API không bị treo khi có nhiều kết nối.
  * Monitor đọc dữ liệu trực tiếp trong process (không tự gọi HTTP vào chính
    mình) -> không còn "unknown"/timeout giả và nhanh hơn nhiều.
  * get_current() gom dữ liệu theo mọi kiểu biến của 13 core + fallback đọc
    lịch sử đã lưu -> giảm hẳn các field null khi worker mới khởi động.
  * Watchdog tự khởi động lại worker của game bị treo (không có dữ liệu mới).
  * Self keep-alive cho Render free plan để service không bị ngủ (chậm phát).
"""
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
from flask import Flask, jsonify, render_template_string
from flask_cors import CORS
from websockets.asyncio.server import serve

PORT = int(os.environ.get('PORT') or os.environ.get('APP_PORT') or 5000)
PUBLIC_URL = (os.environ.get('RENDER_EXTERNAL_URL') or '').rstrip('/')

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

_DATA_DIR = os.environ.get('DATA_DIR') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'data')
MAX_ENTRIES = 100
_storage_lock = threading.Lock()

logging.getLogger('websockets.server').setLevel(logging.CRITICAL)
logging.getLogger('websockets.client').setLevel(logging.ERROR)


class _RateLimit(logging.Filter):
    """Chặn log lặp lại (vòng reconnect của nhà cung cấp die) tối đa 1 lần/30s.

    Log Render bị spam làm app trông như treo và tốn tài nguyên.
    """

    def __init__(self, window=30.0):
        super().__init__()
        self.window = window
        self._seen = {}

    def filter(self, record):
        try:
            key = (record.name, record.levelno, str(record.msg)[:120])
        except Exception:
            return True
        now = time.time()
        last = self._seen.get(key, 0)
        if now - last < self.window:
            return False
        self._seen[key] = now
        if len(self._seen) > 500:
            self._seen = {k: v for k, v in self._seen.items() if now - v < self.window}
        return True


logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(name)s %(message)s')
for _h in logging.getLogger().handlers:
    _h.addFilter(_RateLimit())


def _storage_path(name):
    base = str(name).replace('/', '-').replace('\\', '-').replace('..', '')
    return os.path.join(_DATA_DIR, base + '.json')


def load(name, default=None):
    """Đọc list lịch sử từ {DATA_DIR}/{name}.json (mới->cũ)."""
    if default is None:
        default = []
    try:
        with _storage_lock:
            path = _storage_path(name)
            if not os.path.exists(path):
                return default
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        return data if isinstance(data, list) else default
    except Exception:
        return default


def save(name, entries):
    """Ghi list lịch sử (cap MAX_ENTRIES, atomic)."""
    entries = (entries or [])[:MAX_ENTRIES]
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with _storage_lock:
            path = _storage_path(name)
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(entries, f, ensure_ascii=False)
            os.replace(tmp, path)
    except Exception:
        pass


os.makedirs(_DATA_DIR, exist_ok=True)

_base = os.path.dirname(os.path.abspath(__file__))
for _alias, _fname in _FILES.items():
    try:
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
    except Exception as e:  # 1 core lỗi không được làm sập cả server
        print(f'[app] Không load được {_fname}: {e}')
        globals()[_alias] = None

app = Flask(__name__)
CORS(app)
app.json.sort_keys = False

MODULES = [m for m in (globals().get(a) for a in _FILES) if m is not None]

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
        print(f'[app] Lỗi register {mod.__name__}: {e}')

STATUS = {}
_HISTORY_KEYS = {
    '789': ['789-sicbo', '789-tx'],
    'b52': ['b52-sicbo', 'b52-tx'],
    'betvip': ['betvip-tx', 'betvip-md5'],
    'hitclub': ['hitclub-sicbo', 'hitclub-tx'],
    'lc79': ['lc79-tx', 'lc79-md5'],
    'luck8': ['luck8-txmd5', 'luck8-tx'],
    'max789': ['max789-tx', 'max789-md5'],
    'rikvip': ['rikvip-sicbo', 'rikvip-md5'],
    'sao789': ['sao789-md5', 'sao789-tx'],
    'son789': ['son789-tx'],
    'sumclub': ['sumclub-md5', 'sumclub-tx'],
    'sunwin': ['sunwin-sicbo', 'sunwin-tx'],
    'xocdia88': ['xocdia88-tx', 'xocdia88-md5'],
}


# --------------------------------------------------------------------------- #
# Dữ liệu hiện tại (đọc trực tiếp trong process, không gọi HTTP vào chính mình)
# --------------------------------------------------------------------------- #
def _clean(v):
    if isinstance(v, dict):
        return v if any(x not in (None, '', [], {}) for x in v.values()) else None
    if isinstance(v, list):
        return v or None
    return v or None


def get_current(name):
    """Snapshot dữ liệu hiện tại của 1 game, gom mọi kiểu biến của các core."""
    mod = globals().get('g' + name)
    if mod is None:
        return None
    out = {}
    try:
        for key, attr in (
            ('tx', 'tx_data'),
            ('tx', 'current_result'),
            ('tx', 'latest_result'),
            ('md5', 'latest_result_md5'),
            ('sicbo', 'sicbo_data'),
            ('sicbo', 'sicbo_result'),
            ('sicbo', 'latest_sicbo'),
        ):
            if out.get(key) is None:
                out[key] = _clean(getattr(mod, attr, None))
        latest = getattr(mod, 'latest', None)
        if isinstance(latest, dict) and latest:
            for k, v in latest.items():
                kk = 'md5' if 'md5' in str(k).lower() else 'tx'
                if out.get(kk) is None:
                    out[kk] = _clean(v)
    except Exception:
        pass

    # Fallback: chưa có dữ liệu live -> lấy phiên gần nhất đã lưu trên đĩa,
    # nhờ vậy API/dashboard không trả null ngay sau khi deploy.
    if not any(out.get(k) for k in ('tx', 'md5', 'sicbo')):
        for hk in _HISTORY_KEYS.get(name, []):
            hist = load(hk)
            if hist:
                out['tx'] = hist[0]
                out['cached'] = True
                break

    out = {k: v for k, v in out.items() if v}
    return out or None


def _pick_time(snap):
    for v in (snap or {}).values():
        if isinstance(v, dict):
            for k in ('last_update', 'thoi_gian', 'updateTime', 'time'):
                if v.get(k):
                    return v[k]
    return ''


def _pick_phien(snap):
    for v in (snap or {}).values():
        if isinstance(v, dict) and v.get('phien') is not None:
            return v.get('phien')
    return None


def check_game(name):
    st = {'status': 'idle', 'phien': None, 'last_update': '', 'age': None,
          'checked': datetime.now().strftime('%H:%M:%S')}
    try:
        snap = get_current(name)
        if not snap:
            STATUS[name] = st
            return
        st['phien'] = _pick_phien(snap)
        st['cached'] = bool(snap.get('cached'))
        lu = _pick_time(snap)
        if lu:
            st['last_update'] = str(lu)
            try:
                t = datetime.fromisoformat(str(lu).replace('Z', '+00:00'))
                now = datetime.now(t.tzinfo) if t.tzinfo else datetime.now()
                age = max(0, int((now - t).total_seconds()))
                st['age'] = age
                # Nhiều game 1 phiên ~60-90s -> ngưỡng rộng để không báo "chậm" oan
                st['status'] = 'ok' if age < 100 else ('cham' if age < 300 else 'ngung')
            except Exception:
                st['status'] = 'ok'
        else:
            st['status'] = 'ok' if st['phien'] is not None else 'idle'
        if st.get('cached') and st['status'] == 'ok':
            st['status'] = 'cham'
    except Exception as e:
        st['status'] = 'error'
        st['error'] = str(e)
    STATUS[name] = st


_restart_count = {}
_last_restart = {}
MAX_RESTART = 2


def _watchdog(name):
    """Khởi động lại worker của game chưa từng ra dữ liệu / đã ngừng hẳn.

    Các core đều có vòng reconnect riêng, nên watchdog phải rất dè dặt:
    tối đa 2 lần / game và cách nhau 10 phút, tránh tạo thread trùng lặp
    làm dữ liệu bị lặp và tốn CPU.
    """
    st = STATUS.get(name) or {}
    age = st.get('age')
    if st.get('status') not in ('idle', 'error', 'ngung'):
        return
    if age is not None and age < 600:
        return
    if _restart_count.get(name, 0) >= MAX_RESTART:
        return
    if time.time() - _last_restart.get(name, 0) < 600:
        return
    mod = globals().get('g' + name)
    if mod is None or not hasattr(mod, 'start_background'):
        return
    _last_restart[name] = time.time()
    _restart_count[name] = _restart_count.get(name, 0) + 1
    try:
        print(f'[watchdog] restart {name} (lần {_restart_count[name]})')
        mod.start_background()
    except Exception as e:
        print(f'[watchdog] {name} lỗi: {e}')


def monitor_loop():
    time.sleep(10)
    while True:
        for name in GAME_TX:
            try:
                check_game(name)
                _watchdog(name)
            except Exception:
                pass
        time.sleep(6)


def keepalive_loop():
    """Render free plan ngủ sau ~15 phút -> tự ping để luôn phản hồi nhanh."""
    if not PUBLIC_URL:
        return
    while True:
        time.sleep(240)
        try:
            requests.get(PUBLIC_URL + '/healthz', timeout=10)
        except Exception:
            pass


def _stats():
    s = {'ok': 0, 'cham': 0, 'ngung': 0, 'idle': 0, 'error': 0, 'unknown': 0}
    for name in GAME_TX:
        k = (STATUS.get(name) or {}).get('status', 'unknown')
        s[k if k in s else 'unknown'] += 1
    return s


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #
@app.route('/api/status')
def status():
    out = {}
    for name, path in GAME_TX.items():
        st = STATUS.get(name) or {}
        out[name] = {
            'status': st.get('status', 'unknown'),
            'phien': st.get('phien'),
            'age': st.get('age'),
            'cached': st.get('cached', False),
            'last_update': st.get('last_update', ''),
            'checked': st.get('checked', ''),
            'endpoint': path,
            'ws': '/ws/%s' % name,
        }
    return jsonify({'stats': _stats(), 'games': out,
                    'server_time': datetime.now().isoformat()})


@app.route('/api/all')
def api_all():
    return jsonify({name: get_current(name) for name in GAME_TX})


@app.route('/api/<game>/current')
def api_current(game):
    snap = get_current(game.lower())
    if snap is None:
        return jsonify({'error': 'game khong hop le hoac chua co du lieu',
                        'games': list(GAME_TX)}), 404
    return jsonify(snap)


@app.route('/healthz')
def healthz():
    return jsonify({'ok': True, 'time': datetime.now().isoformat(), 'stats': _stats()})


@app.route('/api/ws')
def ws_info():
    return jsonify({
        'protocol': 'ws',
        'endpoints': [{'game': n, 'url': '/ws/%s' % n, 'http': p}
                      for n, p in GAME_TX.items()],
    })


def _endpoints_of(mod):
    eps = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint.startswith(mod.bp.name + '.'):
            eps.append((str(rule), sorted(rule.methods - {'OPTIONS', 'HEAD'})))
    return sorted(eps)


DASHBOARD = """<!doctype html>
<html lang="vi"><head>
<meta charset="utf-8"><title>Core API · Dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=DM+Sans:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#06080d;--panel:#0d1320;--line:#1c2434;--txt:#e7edf6;--dim:#8494ac;--ok:#34d399;--warn:#fbbf24;--bad:#f87171}
*{box-sizing:border-box}
body{font-family:'DM Sans',system-ui,sans-serif;margin:0;min-height:100vh;color:var(--txt);
 padding:34px 22px 70px;background:
 radial-gradient(900px 520px at 8% -12%,#11213c 0,transparent 60%),
 radial-gradient(820px 520px at 100% -6%,#1b1033 0,transparent 55%),var(--bg)}
.container{max-width:1240px;margin:0 auto}
h1{font-family:'Space Grotesk',sans-serif;font-size:29px;letter-spacing:-.025em;margin:0 0 6px}
.sub{color:var(--dim);font-size:13px;margin:0}
.pill{display:inline-flex;align-items:center;gap:7px;margin-top:14px;padding:6px 12px;border-radius:999px;
 background:rgba(52,211,153,.1);border:1px solid rgba(52,211,153,.3);color:var(--ok);font-size:12px}
.pulse{width:7px;height:7px;border-radius:50%;background:currentColor;animation:p 1.6s infinite}
@keyframes p{0%,100%{opacity:1}50%{opacity:.25}}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:12px;margin:26px 0 24px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:15px;padding:15px 17px}
.stat b{font-family:'Space Grotesk',sans-serif;display:block;font-size:26px;line-height:1.1}
.stat span{font-size:12px;color:var(--dim)}
.wrap{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}
.card{background:linear-gradient(180deg,#101827,#0b111d);border:1px solid var(--line);border-radius:17px;padding:18px;transition:.18s}
.card:hover{border-color:#2c3b53;transform:translateY(-2px)}
.card h2{font-family:'Space Grotesk',sans-serif;margin:0 0 6px;font-size:17px;text-transform:capitalize;
 display:flex;justify-content:space-between;align-items:center;gap:8px}
.meta{font-size:11.5px;color:var(--dim);margin:0 0 12px;line-height:1.7}
.meta b{color:var(--txt);font-weight:500}
table{border-collapse:collapse;width:100%}
td{padding:7px 0;border-top:1px solid rgba(255,255,255,.05);font-size:12.5px}
td:last-child{text-align:right;color:var(--dim);font-size:10.5px;letter-spacing:.06em}
a{color:#8fd2ff;text-decoration:none;font-family:ui-monospace,monospace}
a:hover{color:#fff}
.badge{padding:4px 10px;border-radius:999px;font-size:11px;font-weight:600;white-space:nowrap;border:1px solid transparent;display:inline-flex;align-items:center;gap:6px}
.b-ok{background:rgba(52,211,153,.12);color:var(--ok);border-color:rgba(52,211,153,.35)}
.b-cham{background:rgba(251,191,36,.12);color:var(--warn);border-color:rgba(251,191,36,.35)}
.b-ngung,.b-error{background:rgba(248,113,113,.12);color:var(--bad);border-color:rgba(248,113,113,.35)}
.b-idle,.b-unknown{background:rgba(148,163,184,.12);color:#94a3b8;border-color:rgba(148,163,184,.3)}
.dot{width:6px;height:6px;border-radius:50%;background:currentColor}
</style></head>
<body><div class="container">
<header>
<h1>Core API Dashboard</h1>
<p class="sub">13 game · HTTP + WebSocket trên cùng 1 cổng {{ port }}</p>
<div class="pill"><i class="pulse"></i><span id="tick">đang cập nhật trực tiếp…</span></div>
</header>
<div class="stats">
<div class="stat"><b style="color:var(--ok)" id="s-ok">–</b><span>Hoạt động</span></div>
<div class="stat"><b style="color:var(--warn)" id="s-slow">–</b><span>Chậm / ngừng</span></div>
<div class="stat"><b style="color:var(--bad)" id="s-err">–</b><span>Lỗi</span></div>
<div class="stat"><b style="color:#94a3b8" id="s-idle">–</b><span>Chưa có dữ liệu</span></div>
</div>
<div class="wrap">
{% for r in rows %}
<div class="card" data-game="{{ r.name }}">
<h2>{{ r.name }}<span class="badge b-unknown js-badge"><i class="dot"></i>…</span></h2>
<p class="meta">Phiên: <b class="js-phien">—</b> · cập nhật <b class="js-lu">—</b><br>kiểm tra lúc <b class="js-checked">—</b></p>
<table>
{% for url, methods in r.endpoints %}
<tr><td><a href="{{ url }}" target="_blank">{{ url }}</a></td><td>{{ methods|join(', ') }}</td></tr>
{% endfor %}
</table>
</div>
{% endfor %}
</div>
</div>
<script>
const LABEL={ok:'Hoạt động',cham:'Chậm',ngung:'Ngừng',idle:'Chưa có dữ liệu',error:'Lỗi',unknown:'…'};
async function tick(){
  try{
    const r=await fetch('/api/status',{cache:'no-store'});
    const j=await r.json();
    document.getElementById('s-ok').textContent=j.stats.ok;
    document.getElementById('s-slow').textContent=j.stats.cham+j.stats.ngung;
    document.getElementById('s-err').textContent=j.stats.error;
    document.getElementById('s-idle').textContent=j.stats.idle+j.stats.unknown;
    for(const [name,g] of Object.entries(j.games)){
      const c=document.querySelector(`.card[data-game="${name}"]`); if(!c) continue;
      const b=c.querySelector('.js-badge');
      b.className='badge js-badge b-'+(g.status||'unknown');
      let t=LABEL[g.status]||g.status;
      if(g.age!=null&&g.status!=='ok') t+=' '+g.age+'s';
      if(g.cached) t+=' (cache)';
      b.innerHTML='<i class="dot"></i>'+t;
      c.querySelector('.js-phien').textContent=g.phien??'—';
      c.querySelector('.js-lu').textContent=g.last_update||'—';
      c.querySelector('.js-checked').textContent=g.checked||'—';
    }
    document.getElementById('tick').textContent='cập nhật trực tiếp · '+new Date().toLocaleTimeString('vi-VN');
  }catch(e){ document.getElementById('tick').textContent='mất kết nối, đang thử lại…'; }
}
tick(); setInterval(tick,5000);
</script>
</body></html>"""


@app.route('/')
def dashboard():
    rows = [{'name': mod.bp.name.replace('game', ''), 'endpoints': _endpoints_of(mod)}
            for mod in MODULES]
    return render_template_string(DASHBOARD, rows=rows, port=PORT)


# --------------------------------------------------------------------------- #
# Bridge: 1 cổng phục vụ cả HTTP (Flask WSGI) và WebSocket
# --------------------------------------------------------------------------- #
def _wsgi_call(environ):
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured['status'] = status
        captured['headers'] = headers

    body = b''
    result = None
    try:
        result = app.wsgi_app(environ, start_response)
        for chunk in result:
            if chunk:
                body += chunk
    finally:
        try:
            if result is not None:
                result.close()
        except Exception:
            pass
    return captured.get('status', '200 OK'), captured.get('headers', []), body


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
        'wsgi.url_scheme': 'https' if PUBLIC_URL.startswith('https') else 'http',
        'wsgi.input': io.BytesIO(b''),
        'wsgi.errors': io.StringIO(),
        'wsgi.multithread': True,
        'wsgi.multiprocess': False,
        'wsgi.run_once': False,
        'HTTP_HOST': headers_map.get('host', 'localhost:%d' % PORT),
        'REMOTE_ADDR': '127.0.0.1',
        'REMOTE_PORT': '0',
    }
    for k, v in headers_map.items():
        kk = k.replace('-', '_').upper()
        if kk not in ('HOST', 'CONNECTION', 'UPGRADE'):
            env['HTTP_' + kk] = v
    return env


def _serve_http(path, query, headers_map):
    from websockets.http11 import Response
    from websockets.datastructures import Headers as WSHeaders
    try:
        status, hdrs, body = _wsgi_call(_build_environ(path, query, headers_map))
    except Exception as e:
        body = json.dumps({'error': 'internal', 'detail': str(e)}).encode()
        status, hdrs = '500 Internal Server Error', [('Content-Type', 'application/json')]
    code = int(status.split(' ')[0])
    reason = ' '.join(status.split(' ')[1:]) or 'OK'
    wh = WSHeaders()
    for hn, hv in hdrs:
        if hn.lower() in ('content-length', 'transfer-encoding'):
            continue
        wh[hn] = hv
    wh['Content-Length'] = str(len(body))
    return Response(code, reason, wh, body)


async def process_request(conn, request):
    """/ws/* -> websocket; còn lại -> Flask (chạy trong thread, không block loop)."""
    full = request.path
    path, _, qs = full.partition('?')
    path_u = urllib.parse.unquote(path)
    if path_u.startswith('/ws/') or path_u == '/ws':
        return None
    headers_map = {k.lower(): v for k, v in request.headers.items()}
    try:
        return await asyncio.to_thread(_serve_http, path_u, qs, headers_map)
    except Exception as e:
        from websockets.http11 import Response
        from websockets.datastructures import Headers as WSHeaders
        body = json.dumps({'error': str(e)}).encode()
        wh = WSHeaders()
        wh['Content-Type'] = 'application/json'
        wh['Content-Length'] = str(len(body))
        return Response(500, 'Internal Server Error', wh, body)


async def ws_handler(conn):
    req = getattr(conn, 'request', None)
    path = req.path if req else '/ws/'
    game = urllib.parse.unquote(path).partition('/ws/')[2].split('/')[0].split('?')[0].lower()
    if not game or globals().get('g' + game) is None:
        await conn.send(json.dumps({'error': 'game khong hop le',
                                   'games': list(GAME_TX)}, ensure_ascii=False))
        await conn.close()
        return
    last_sig = None
    last_beat = 0.0
    while True:
        try:
            snap = get_current(game)
            sig = json.dumps(snap, ensure_ascii=False, sort_keys=True, default=str)
            now = time.time()
            if sig != last_sig or now - last_beat > 15:
                last_sig, last_beat = sig, now
                await conn.send(json.dumps(
                    {'game': game, 'data': snap, 'server_time': datetime.now().isoformat()},
                    ensure_ascii=False, default=str))
        except Exception:
            break
        await asyncio.sleep(1)


def main():
    for mod in MODULES:
        try:
            mod.start_background()
        except Exception as e:
            print(f'[app] start_background {mod.__name__} lỗi: {e}')
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=keepalive_loop, daemon=True).start()

    async def _run():
        async with serve(
            ws_handler,
            host='0.0.0.0',
            port=PORT,
            process_request=process_request,
            ping_interval=20,
            ping_timeout=25,
            max_size=8 * 1024 * 1024,
        ) as server:
            print('=' * 52)
            print(f'🚀 Core API chạy: http://0.0.0.0:{PORT}  |  ws://…/ws/<game>')
            print(f'   games: {", ".join(GAME_TX)}')
            print('=' * 52)
            await server.serve_forever()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print('\n👋 Stopped')


if __name__ == '__main__':
    main()
