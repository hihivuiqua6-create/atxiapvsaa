import asyncio
import json
import websockets
import time
import requests
from datetime import datetime
from flask import Blueprint, jsonify, request


CAU_LEN = 15


def to_tx(ket_qua):
    """Chuyển ket_qua sang ký tự T/X/? (nếu không có tài/xỉu)."""
    if not ket_qua:
        return "?"
    k = str(ket_qua).strip().lower()
    if "tà" in k or "tai" in k:
        return "T"
    if "xỉ" in k or "xiu" in k or "bão" in k or "bao" in k:
        return "X"
    return "?"


def build_cau(history):
    """Trả về chuỗi cau (dài tối đa CAU_LEN, mới nhất ở cuối) từ history mới->cũ."""
    seq = "".join(to_tx(e.get("ket_qua")) for e in (history or [])[:CAU_LEN])
    return seq[::-1]

def _st(v):
    return None if v is None else str(v)


def latest_payload(latest, cau):
    """Tạo payload kết quả mới nhất theo schema dice1/dice2/dice3/dice."""
    xuc = latest.get("xuc_xac") or []
    d1 = xuc[0] if len(xuc) > 0 else None
    d2 = xuc[1] if len(xuc) > 1 else None
    d3 = xuc[2] if len(xuc) > 2 else None
    dice = None if (d1 is None or d2 is None or d3 is None) else f"{d1}-{d2}-{d3}"
    return {
        "phien": latest.get("phien"),
        "dice1": _st(d1),
        "dice2": _st(d2),
        "dice3": _st(d3),
        "dice": dice,
        "tong": _st(latest.get("tong")),
        "ket_qua": latest.get("ket_qua"),
        "cau": cau,
        "last_update": latest.get("last_update"),
    }


def decorate_history(history, limit=None):
    """Trả về bản copy history (mới->cũ) theo schema dice1/dice2/dice3/dice, kèm 'cau'
    là chuỗi T/X cũ->mới của 15 phiên kết thúc ở phiên đó (mới nhất ở cuối)."""
    target = (history or [])[:limit] if limit else (history or [])
    result = []
    for i, e in enumerate(target):
        window = "".join(to_tx(x.get("ket_qua")) for x in (history or [])[i:i + CAU_LEN])
        xuc = e.get("xuc_xac") or []
        d1 = xuc[0] if len(xuc) > 0 else None
        d2 = xuc[1] if len(xuc) > 1 else None
        d3 = xuc[2] if len(xuc) > 2 else None

        def st(v):
            return None if v is None else str(v)

        dice = None if (d1 is None or d2 is None or d3 is None) else f"{d1}-{d2}-{d3}"
        row = {
            "phien": e.get("phien"),
            "dice1": st(d1),
            "dice2": st(d2),
            "dice3": st(d3),
            "dice": dice,
            "tong": st(e.get("tong")),
            "ket_qua": e.get("ket_qua"),
            "cau": window[::-1],
        }
        if e.get("last_update") is not None:
            row["last_update"] = e.get("last_update")
        result.append(row)
    return result


import threading
import sys
import io

# Set UTF-8 cho console
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

bp = Blueprint('game789', __name__)

# Biến toàn cục để lưu dữ liệu tx
tx_data = {
    "phien": None,
    "xuc_xac_1": None,
    "xuc_xac_2": None,
    "xuc_xac_3": None,
    "tong": None,
    "ket_qua": None,
    "so_nguoi_cuoc_tai": None,
    "so_tien_cuoc_tai": None,
    "so_nguoi_cuoc_xiu": None,
    "so_tien_cuoc_xiu": None,
    "lich_su_cuoc": [],  # Lưu lịch sử cược
    "thong_bao": [],     # Lưu thông báo
    "last_update": None
}

def parse_tx_data(message):
    """Parse dữ liệu từ message nhận được"""
    try:
        # Parse message JSON
        data = json.loads(message)
        
        # Kiểm tra cấu trúc message
        if len(data) >= 2 and isinstance(data[1], dict):
            msg_data = data[1]
            cmd = msg_data.get('cmd')
            
            # Xử lý cmd 2106 - Dữ liệu tài xỉu
            if cmd == 2106 and 'bs' in msg_data:
                bs_array = msg_data['bs']
                
                # Lấy dữ liệu từ bs array
                tai_data = bs_array[0] if len(bs_array) > 0 else {}
                xiu_data = bs_array[1] if len(bs_array) > 1 else {}
                
                # Cập nhật dữ liệu
                tx_data["so_nguoi_cuoc_tai"] = tai_data.get('bc')
                tx_data["so_tien_cuoc_tai"] = tai_data.get('v')
                tx_data["so_nguoi_cuoc_xiu"] = xiu_data.get('bc')
                tx_data["so_tien_cuoc_xiu"] = xiu_data.get('v')
                
                # Lấy kết quả xúc xắc
                if 'd1' in msg_data:
                    tx_data["xuc_xac_1"] = msg_data['d1']
                if 'd2' in msg_data:
                    tx_data["xuc_xac_2"] = msg_data['d2']
                if 'd3' in msg_data:
                    tx_data["xuc_xac_3"] = msg_data['d3']
                
                # Tính tổng và kết quả
                if tx_data["xuc_xac_1"] is not None and tx_data["xuc_xac_2"] is not None and tx_data["xuc_xac_3"] is not None:
                    tong = tx_data["xuc_xac_1"] + tx_data["xuc_xac_2"] + tx_data["xuc_xac_3"]
                    tx_data["tong"] = tong
                    
                    # Kiểm tra kết quả: Tài (tổng 11-18 hoặc 3 mặt đều 3), Xỉu (tổng 4-10)
                    if tong >= 11 or (tx_data["xuc_xac_1"] == 3 and tx_data["xuc_xac_2"] == 3 and tx_data["xuc_xac_3"] == 3):
                        tx_data["ket_qua"] = "Tài"
                    else:
                        tx_data["ket_qua"] = "Xỉu"
                
                # Lấy phiên
                if 'sid' in msg_data:
                    tx_data["phien"] = msg_data['sid']
                
                # Lưu lịch sử
                history = {
                    "phien": tx_data["phien"],
                    "xuc_xac": [tx_data["xuc_xac_1"], tx_data["xuc_xac_2"], tx_data["xuc_xac_3"]],
                    "tong": tx_data["tong"],
                    "ket_qua": tx_data["ket_qua"],
                    "timestamp": datetime.now().isoformat()
                }
                tx_data["lich_su_cuoc"].insert(0, history)
                # Giữ lại 100 lịch sử gần nhất
                tx_data["lich_su_cuoc"] = tx_data["lich_su_cuoc"][:100]
                save('789-tx', tx_data["lich_su_cuoc"])
                
                tx_data["last_update"] = datetime.now().isoformat()
                
                # Log cập nhật dữ liệu
                print(f"[🎲✅] phiên {tx_data['phien']} - {'-'.join(str(x) if x is not None else '?' for x in [tx_data['xuc_xac_1'], tx_data['xuc_xac_2'], tx_data['xuc_xac_3']])} ➜ tổng: {tx_data['tong']}, kết quả: {tx_data['ket_qua']} | cau={build_cau(tx_data['lich_su_cuoc'])} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                
            # Xử lý cmd 2108 - Thông báo
            elif cmd == 2108:
                mgs = msg_data.get('mgs', '')
                c = msg_data.get('c', 0)
                tst = msg_data.get('tst', 0)
                fu = msg_data.get('fu', '')
                
                # Lưu thông báo
                notification = {
                    "timestamp": datetime.now().isoformat(),
                    "time": tst,
                    "message": mgs,
                    "code": c,
                    "fu": fu
                }
                tx_data["thong_bao"].insert(0, notification)
                tx_data["thong_bao"] = tx_data["thong_bao"][:20]
                
    except json.JSONDecodeError:
        print(f"❌ Lỗi parse JSON: {e}")
    except Exception as e:
        print(f"❌ Lỗi xử lý dữ liệu: {e}")
        import traceback
        traceback.print_exc()

async def send_messages(websocket):
    """Gửi các message định kỳ"""
    try:
        # Message khởi tạo
        init_message = [1, "MiniGame", "quapitjz1", "Hung2010a", {
            "info": json.dumps({
                "ipAddress": "2405:4802:4e51:e130:4caf:e983:fa23:e5ee",
                "wsToken": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJnZW5kZXIiOjAsImNhblZpZXdTdGF0IjpmYWxzZSwiZGlzcGxheU5hbWUiOiJkc2pqdTE0OGMiLCJib3QiOjAsImlzTWVyY2hhbnQiOmZhbHNlLCJ2ZXJpZmllZEJhbmtBY2NvdW50IjpmYWxzZSwicGxheUV2ZW50TG9iYnkiOmZhbHNlLCJjdXN0b21lcklkIjo2ODc2Nzg0OCwiYWZmSWQiOiJzdW4ud2luIiwiYmFubmVkIjpmYWxzZSwiYnJhbmQiOiI3ODkuY2x1YiIsImVtYWlsIjoiIiwidGltZXN0YW1wIjoxNzc4NzU0MTI0OTEwLCJsb2NrR2FtZXMiOltdLCJhbW91bnQiOjAsImxvY2tDaGF0IjpmYWxzZSwicGhvbmVWZXJpZmllZCI6ZmFsc2UsImlwQWRkcmVzcyI6IjI0MDU6NDgwMjo0ZTUxOmUxMzA6NGNhZjplOTgzOmZhMjM6ZTVlZSIsIm11dGUiOmZhbHNlLCJhdmF0YXIiOiJodHRwczovL2FwaS54ZXVpLmlvL2ltYWdlcy9hdmF0YXIvYXZhdGFyXzA2LnBuZyIsInBsYXRmb3JtSWQiOjQsInVzZXJJZCI6ImE3MDY4ZTI1LWVkMjQtNDlhZC1iNGRiLTJhMDdjMTMyZmMzMSIsImVtYWlsVmVyaWZpZWQiOm51bGwsInJlZ1RpbWUiOjE3Nzg3NTQxMDYzMjEsInBob25lIjoiIiwiZGVwb3NpdCI6ZmFsc2UsInVzZXJuYW1lIjoiUzhfcXVhcGl0anoxIn0.2Q3jEHgeR8kSlfpejCcy9ui7HDn8SwcvrcKxNNWjycU",
                "locale": "vi",
                "userId": "a7068e25-ed24-49ad-b4db-2a07c132fc31",
                "username": "S8_quapitjz1",
                "timestamp": 1778754124921,
                "refreshToken": "debf5309d7ea447ba2db47e2a86e7467.4aa5c4314aad4e5baf273eaa14e66207"
            }),
            "signature": "65DBF77A74171E1065B3E6AAD6EDD1F42BD3AB550DF07A9048D95BE5008000869416E3BCB85562ECC68B1517D8690DDC4C1ACEFE652BBAD8DBD933564C757C05A7A1E2B907AB4FC31A09ACBF49B0AEDF467E58E8DF12BAAF91629E31C61CEDD98249DF0E64944C70AA68CD3F6C74E7A23482A793339664C410817157E8186B76"
        }]
        
        await websocket.send(json.dumps(init_message))
        await asyncio.sleep(1)
        
        # Message taixiuCommonPlugin cmd 2100
        tx_cmd = [6, "MiniGame", "taixiuCommonPlugin", {"cmd": 2100}]
        await websocket.send(json.dumps(tx_cmd))
        await asyncio.sleep(1)
        
        # Message lobbyPlugin cmd 10001
        lobby_cmd = [6, "MiniGame", "lobbyPlugin", {"cmd": 10001}]
        await websocket.send(json.dumps(lobby_cmd))
        
        # Vòng lặp gửi message định kỳ mỗi 5 giây
        count = 0
        while True:
            await asyncio.sleep(5)
            count += 1
            
            # Message Simms
            simms_msg = [7, "Simms", 2, 0, {"id": 0}]
            await websocket.send(json.dumps(simms_msg))
            
            # Message taixiuCommonPlugin cmd 2100
            tx_cmd = [6, "MiniGame", "taixiuCommonPlugin", {"cmd": 2100}]
            await websocket.send(json.dumps(tx_cmd))
            
    except Exception as e:
        print(f"❌ Lỗi khi gửi message: {e}")
        import traceback
        traceback.print_exc()

async def websocket_client():
    """Kết nối WebSocket và xử lý message"""
    uri = "wss://websocket.atpman.net/websocket"
    
    while True:
        try:
            print(f"\n{'='*60}")
            print(f"🔌 ĐANG KẾT NỐI ĐẾN {uri}")
            print(f"{'='*60}")
            
            async with websockets.connect(uri) as websocket:
                print(f"✅ Đã kết nối thành công!")
                
                # Tạo task gửi message
                send_task = asyncio.create_task(send_messages(websocket))
                
                # Nhận và xử lý message
                try:
                    async for message in websocket:
                        parse_tx_data(message)
                except websockets.exceptions.ConnectionClosed as e:
                    print(f"\n⚠️ Kết nối đã đóng: {e}")
                    print("🔄 Đang thử kết nối lại sau 5 giây...")
                    send_task.cancel()
                    await asyncio.sleep(5)
                    
        except Exception as e:
            print(f"\n❌ Lỗi kết nối: {e}")
            print("🔄 Đang thử kết nối lại sau 5 giây...")
            await asyncio.sleep(5)

@bp.route('/789/api/tx', methods=['GET'])
def get_tx_data():
    """API endpoint để lấy dữ liệu TX hiện tại"""
    return jsonify(latest_payload({
        "ket_qua": tx_data["ket_qua"],
        "last_update": tx_data["last_update"],
        "phien": tx_data["phien"],
        "tong": tx_data["tong"],
        "xuc_xac": [tx_data["xuc_xac_1"], tx_data["xuc_xac_2"], tx_data["xuc_xac_3"]],
    }, build_cau(tx_data["lich_su_cuoc"])))

@bp.route('/789/api/tx/history', methods=['GET'])
def get_tx_history():
    """API endpoint để lấy lịch sử TX"""
    limit = request.args.get('limit', 100, type=int)
    return jsonify({
        "history": decorate_history(tx_data["lich_su_cuoc"], limit),
        "total": len(tx_data["lich_su_cuoc"])
    })

@bp.route('/789/api/tx/history/<int:n>', methods=['GET'])
def get_tx_history_n(n):
    """API endpoint để lấy n phiên lịch sử gần nhất"""
    if n <= 0:
        n = 1
    return jsonify({
        "history": decorate_history(tx_data["lich_su_cuoc"], n),
        "total": len(tx_data["lich_su_cuoc"])
    })

@bp.route('/789/api/notifications', methods=['GET'])
def get_notifications():
    """API endpoint để lấy thông báo"""
    limit = request.args.get('limit', 10, type=int)
    return jsonify({
        "notifications": tx_data["thong_bao"][:limit],
        "total": len(tx_data["thong_bao"])
    })

# ==================== SICBO (nguồn: api.xeuigogo.info/ktrng_3986) ====================
SICBO_BASE_URL = 'https://api.xeuigogo.info/v2/history/getLastResult'
SICBO_PARAMS = {
    'gameId': 'ktrng_3986',
    'tableId': '39861215743193',
    'size': 100,
    'curPage': 1
}

# Cấu trúc dữ liệu sicbo mặc định (giống format TX của 789)
sicbo_data = {
    "ket_qua": None,
    "last_update": None,
    "phien": None,
    "tong": None,
    "xuc_xac_1": None,
    "xuc_xac_2": None,
    "xuc_xac_3": None
}
sicbo_history = []

def fetch_sicbo():
    """Lấy lịch sử sicbo từ API (mới nhất trước)"""
    try:
        response = requests.get(SICBO_BASE_URL, params=SICBO_PARAMS, timeout=10)
        response.raise_for_status()
        data = response.json()
        result_list = []
        if isinstance(data, dict):
            d = data.get('data')
            if isinstance(d, dict):
                result_list = d.get('resultList', [])
            elif isinstance(d, list):
                result_list = d
        return result_list[:100] if isinstance(result_list, list) else []
    except Exception as e:
        print(f"❌ Lỗi fetch sicbo: {e}")
        return None

def sicbo_poll_loop():
    """Poll sicbo định kỳ"""
    while True:
        try:
            result_list = fetch_sicbo()
            if result_list:
                latest = result_list[0]
                faces = latest.get('facesList') if isinstance(latest.get('facesList'), list) else []
                scores = latest.get('score') or 0
                sicbo_data["ket_qua"] = 'Tài' if scores >= 11 else 'Xỉu'
                sicbo_data["last_update"] = datetime.now().isoformat()
                sicbo_data["phien"] = latest.get('gameNum', '')
                sicbo_data["tong"] = scores
                sicbo_data["xuc_xac_1"] = faces[0] if len(faces) > 0 else None
                sicbo_data["xuc_xac_2"] = faces[1] if len(faces) > 1 else None
                sicbo_data["xuc_xac_3"] = faces[2] if len(faces) > 2 else None

                sicbo_history.clear()
                for r in result_list:
                    fs = r.get('facesList') if isinstance(r.get('facesList'), list) else []
                    sc = r.get('score') or 0
                    sicbo_history.append({
                        "phien": r.get('gameNum', ''),
                        "xuc_xac": fs + [None] * (3 - len(fs)),
                        "tong": sc,
                        "ket_qua": 'Tài' if sc >= 11 else 'Xỉu',
                        "timestamp": datetime.now().isoformat()
                    })
                save('789-sicbo', sicbo_history)
                print(f"[🎲🧨] SICBO phiên {sicbo_data['phien']} - {sicbo_data['xuc_xac_1']}-{sicbo_data['xuc_xac_2']}-{sicbo_data['xuc_xac_3']} "
                      f"➜ tổng: {sicbo_data['tong']}, kết quả: {sicbo_data['ket_qua']} | cau={build_cau(sicbo_history)} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception as e:
            print(f"❌ Lỗi trong sicbo_poll_loop: {e}")
        time.sleep(5)

@bp.route('/789/api/sicbo', methods=['GET'])
def get_sicbo_data():
    """API endpoint để lấy dữ liệu sicbo hiện tại (chuẩn 789 xuc_xac:[3])"""
    return jsonify(latest_payload({
        "ket_qua": sicbo_data["ket_qua"],
        "last_update": sicbo_data["last_update"],
        "phien": sicbo_data["phien"],
        "tong": sicbo_data["tong"],
        "xuc_xac": [sicbo_data["xuc_xac_1"], sicbo_data["xuc_xac_2"], sicbo_data["xuc_xac_3"]],
    }, build_cau(sicbo_history)))

@bp.route('/789/api/sicbo/history', methods=['GET'])
def get_sicbo_history():
    """API endpoint để lấy lịch sử sicbo"""
    limit = request.args.get('limit', 100, type=int)
    return jsonify({
        "history": decorate_history(sicbo_history, limit),
        "total": len(sicbo_history)
    })

@bp.route('/789/api/sicbo/history/<int:n>', methods=['GET'])
def get_sicbo_history_n(n):
    """API endpoint để lấy n phiên lịch sử sicbo gần nhất"""
    if n <= 0:
        n = 1
    return jsonify({
        "history": decorate_history(sicbo_history, n),
        "total": len(sicbo_history)
    })

def start_background():
    """Kích hoạt các worker thread nền (gọi từ app.py router)"""
    global sicbo_history
    tx_data["lich_su_cuoc"] = load('789-tx')
    sicbo_history = load('789-sicbo')
    threading.Thread(target=sicbo_poll_loop, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(websocket_client()), daemon=True).start()

