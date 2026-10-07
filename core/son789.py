import asyncio
import json
import websockets
import time
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
import hashlib

# Set UTF-8 cho console
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

bp = Blueprint('gameson789', __name__)

# Biến toàn cục để lưu dữ liệu
tx_data = {
    "phien": None,
    "xuc_xac_1": None,
    "xuc_xac_2": None,
    "xuc_xac_3": None,
    "tong": None,
    "ket_qua": None,
    "md5_result": None,
    "raw_rS": None,
    "cBB": None,
    "gBB": None,
    "j": None,
    "lich_su_cuoc": [],
    "last_update": None
}

def tinh_tong_va_ket_qua(d1, d2, d3):
    """Tính tổng và kết quả tài/xỉu"""
    tong = d1 + d2 + d3
    # Tài: 11-18 hoặc 3 mặt đều 3, Xỉu: 4-10
    if tong >= 11 or (d1 == 3 and d2 == 3 and d3 == 3):
        ket_qua = "Tài"
    else:
        ket_qua = "Xỉu"
    return tong, ket_qua

def parse_rS(rS_string):
    """Parse chuỗi rS để lấy thông tin chi tiết"""
    # Ví dụ: "#1533931_HjceML9p8GjcamA1P{2-1-1}NxnQrBa"
    try:
        # Tách lấy phần phiên
        if '_' in rS_string:
            parts = rS_string.split('_')
            phien_part = parts[0].replace('#', '')
            
            # Tìm phần {x-y-z} chứa kết quả xúc xắc
            import re
            dice_pattern = r'\{(\d)-(\d)-(\d)\}'
            dice_match = re.search(dice_pattern, rS_string)
            
            if dice_match:
                d1 = int(dice_match.group(1))
                d2 = int(dice_match.group(2))
                d3 = int(dice_match.group(3))
                return {
                    "phien": phien_part,
                    "xuc_xac": [d1, d2, d3],
                    "co_dice": True
                }
        return {"phien": None, "xuc_xac": None, "co_dice": False}
    except Exception as e:
        print(f"Lỗi parse rS: {e}")
        return {"phien": None, "xuc_xac": None, "co_dice": False}

def parse_tx_data(message):
    """Parse dữ liệu từ message nhận được"""
    try:
        # Parse message JSON
        data = json.loads(message)
        
        # Kiểm tra cấu trúc message
        if len(data) >= 2 and isinstance(data[1], dict):
            msg_data = data[1]
            cmd = msg_data.get('cmd')
            
            # Xử lý cmd 1103 - Dữ liệu tài xỉu mới
            if cmd == 1103:
                print("\n" + "="*70)
                print("=== DỮ LIỆU TÀI XỈU (CMD 1103) ===")
                print("="*70)
                
                # Lấy các trường dữ liệu
                tx_data["cBB"] = msg_data.get('cBB', 0)
                tx_data["gBB"] = msg_data.get('gBB', 0)
                tx_data["j"] = msg_data.get('j', 0)
                
                # Lấy kết quả xúc xắc trực tiếp
                if 'd1' in msg_data:
                    tx_data["xuc_xac_1"] = msg_data['d1']
                if 'd2' in msg_data:
                    tx_data["xuc_xac_2"] = msg_data['d2']
                if 'd3' in msg_data:
                    tx_data["xuc_xac_3"] = msg_data['d3']
                
                # Lấy và parse rS
                rS_string = msg_data.get('rS', '')
                tx_data["raw_rS"] = rS_string
                
                # Parse rS để lấy thông tin
                parsed_rS = parse_rS(rS_string)
                
                # Nếu có phiên từ rS thì dùng, không thì dùng từ data
                if parsed_rS["phien"]:
                    tx_data["phien"] = parsed_rS["phien"]
                else:
                    tx_data["phien"] = msg_data.get('sid', None)
                
                # Nếu không có d1,d2,d3 trực tiếp thì lấy từ rS
                if tx_data["xuc_xac_1"] is None and parsed_rS["xuc_xac"]:
                    tx_data["xuc_xac_1"], tx_data["xuc_xac_2"], tx_data["xuc_xac_3"] = parsed_rS["xuc_xac"]
                
                # Tính tổng và kết quả
                if tx_data["xuc_xac_1"] is not None:
                    tong, ket_qua = tinh_tong_va_ket_qua(
                        tx_data["xuc_xac_1"], 
                        tx_data["xuc_xac_2"], 
                        tx_data["xuc_xac_3"]
                    )
                    tx_data["tong"] = tong
                    tx_data["ket_qua"] = ket_qua
                
                # Tính MD5 của rS để kiểm tra
                if rS_string:
                    md5_hash = hashlib.md5(rS_string.encode()).hexdigest()
                    tx_data["md5_result"] = md5_hash.upper()
                
# Lưu lịch sử
                history = {
                    "timestamp": datetime.now().isoformat(),
                    "phien": tx_data["phien"],
                    "xuc_xac": [tx_data["xuc_xac_1"], tx_data["xuc_xac_2"], tx_data["xuc_xac_3"]],
                    "tong": tx_data["tong"],
                    "ket_qua": tx_data["ket_qua"],
                    "raw_rS": tx_data["raw_rS"],
                    "md5_result": tx_data["md5_result"],
                    "cBB": tx_data["cBB"],
                    "gBB": tx_data["gBB"],
                    "j": tx_data["j"]
                }
                tx_data["lich_su_cuoc"].insert(0, history)
                # Giữ lại 50 lịch sử gần nhất
                tx_data["lich_su_cuoc"] = tx_data["lich_su_cuoc"][:50]
                save('son789-tx', tx_data["lich_su_cuoc"])
                
                tx_data["last_update"] = datetime.now().isoformat()
                print(f"🎲 Phiên {tx_data['phien']} - "
                      f"{tx_data['xuc_xac_1']}-{tx_data['xuc_xac_2']}-{tx_data['xuc_xac_3']} "
                      f"= {tx_data['tong']} ({tx_data['ket_qua']}) "
                      f"| cau={build_cau(tx_data['lich_su_cuoc'])}")
                
            # Xử lý cmd khác
            elif cmd:
                print(f"\n📌 Message cmd {cmd} từ {data[2] if len(data) > 2 else 'unknown'}")
                if cmd == 1105:
                    print("✅ Đã nhận response cho cmd 1105")
                elif cmd == 10001:
                    print("✅ Đã nhận response cho cmd 10001")
                elif cmd == 310:
                    print("✅ Đã nhận response cho cmd 310")
                
        # Xử lý message ping/pong
        elif len(data) >= 1 and data[0] == 8:
            pass

        # Log các message khác
        else:
            if not data:
                return
            print(f"📝 Message khác")
            
    except json.JSONDecodeError as e:
        print(f"❌ Lỗi parse JSON: {e}")
    except Exception as e:
        print(f"❌ Lỗi xử lý dữ liệu: {e}")

async def send_messages(websocket):
    """Gửi các message định kỳ"""
    try:
        # Message khởi tạo
        init_message = [1, "MiniGame", "Quapit", "Hung2010a", {
            "signature": "4B5C8265FA10515EBAF652DF75AA193C6196EA215453E83863D74E465AF238233810FAFB999C07A8755CBEBD00CB17A31A265AF96F18CCF5FB8AFB4F65B35DF5D38E7565E6BB2181A54EC0E6508ADBC9DF86CE3E0E81CD172B203C56505189D314F456F15F228E9B56ABCE841E14C5E4196715CF2AD961A124A2D913134EE845",
            "info": {
                "cs": "4dc5955332de74761ebc17d160770529",
                "phone": "",
                "ipAddress": "2405:4802:4e51:e130:4caf:e983:fa23:e5ee",
                "isMerchant": False,
                "userId": "24d37a7c-3ac3-4b47-8c38-60e8cf6352d1",
                "deviceId": "2501856051151851514860415102476832",
                "isMktAccount": False,
                "username": "quapit",
                "timestamp": 1778755758270
            },
            "pid": 4
        }]
        
        print("🔗 Đang gửi init message...")
        await websocket.send(json.dumps(init_message))
        await asyncio.sleep(1)
        
        # Message taixiuMd5Plugin cmd 1105
        tx_cmd = [6, "MiniGame", "taixiuMd5Plugin", {"cmd": 1105}]
        await websocket.send(json.dumps(tx_cmd))
        await asyncio.sleep(1)
        
        # Message lobbyPlugin cmd 10001
        lobby_cmd = [6, "MiniGame", "lobbyPlugin", {"cmd": 10001}]
        await websocket.send(json.dumps(lobby_cmd))
        await asyncio.sleep(1)
        
        # Message channelPlugin cmd 310
        channel_cmd = [6, "MiniGame", "channelPlugin", {"cmd": 310}]
        await websocket.send(json.dumps(channel_cmd))
        
        print("✅ ĐÃ GỬI INIT + CMD 1105 + LOBBY + CHANNEL. Bắt đầu vòng ping (30s).")
        
        # Vòng lặp gửi ping định kỳ mỗi 30 giây
        count = 0
        while True:
            await asyncio.sleep(30)
            count += 1
            
            # Message ping
            ping_msg = [7, "MiniGame", 3, int(time.time() * 1000)]
            await websocket.send(json.dumps(ping_msg))
            
            print(f"💓 PING lần {count} [{datetime.now().strftime('%H:%M:%S')}]")
            
    except Exception as e:
        print(f"❌ Lỗi khi gửi message: {e}")
        import traceback
        traceback.print_exc()

async def websocket_client():
    """Kết nối WebSocket và xử lý message"""
    uri = "wss://api.jiusyss.me/websocket?d=YUcxaWJXbGliVzg9fDE0NnwxNzc4NzU1NzU3NzM4fGRkYmE5OWIxYWEyM2U0ZDVjOGY1YmU4YmRlNjM0YzFkfDgyOGE4YzJiOTNiNDYzNTQxOTI2MWRhNTcxOTE1MDY1fEU3Qjk2MjU="
    
    while True:
        try:
            print(f"\n{'='*70}")
            print(f"🔌 ĐANG KẾT NỐI ĐẾN {uri[:100]}...")
            print(f"{'='*70}")
            
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

@bp.route('/son789/api/tx', methods=['GET'])
def get_tx_data():
    """API endpoint để lấy dữ liệu TX hiện tại (chuẩn 789)"""
    return jsonify(latest_payload({
        "ket_qua": tx_data["ket_qua"],
        "last_update": tx_data["last_update"],
        "phien": tx_data["phien"],
        "tong": tx_data["tong"],
        "xuc_xac": [tx_data["xuc_xac_1"], tx_data["xuc_xac_2"], tx_data["xuc_xac_3"]],
    }, build_cau(tx_data["lich_su_cuoc"])))

@bp.route('/son789/api/tx/history', methods=['GET'])
def get_tx_history():
    """API endpoint để lấy lịch sử TX"""
    limit = request.args.get('limit', 20, type=int)
    return jsonify({
        "history": decorate_history(tx_data["lich_su_cuoc"], limit),
        "total": len(tx_data["lich_su_cuoc"])
    })

@bp.route('/son789/api/tx/verify', methods=['POST'])
def verify_md5():
    """API endpoint để verify MD5 của rS"""
    data = request.get_json()
    rS_string = data.get('rS', '')
    if rS_string:
        md5_calc = hashlib.md5(rS_string.encode()).hexdigest().upper()
        return jsonify({
            "rS": rS_string,
            "md5_calculated": md5_calc,
            "md5_received": data.get('md5', '')
        })
    return jsonify({"error": "No rS provided"}), 400

def start_background():
    """Kích hoạt các worker thread nền (gọi từ app.py router)"""
    tx_data["lich_su_cuoc"] = load('son789-tx')
    threading.Thread(target=lambda: asyncio.run(websocket_client()), daemon=True).start()

