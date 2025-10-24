import argparse
import asyncio
import websockets
import requests
import json
import os
import logging
import time
import random
import sys
from uuid import UUID
from PIL import Image
import tool
import threading
from collections import OrderedDict, deque

# --- 全局配置 ---
API_BASE_URL = "https://paintboard.luogu.me"
WS_URL = "wss://paintboard.luogu.me/api/paintboard/ws"
CONFIG_FILE = "config.json"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("paint.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# pending paints waiting for confirmation via board update: list of dicts {uid, paint_id, pos, color, time}
pending_paints = []
# per-user snapshot of their most recent successful painted pixels: uid -> OrderedDict[(x,y) -> (r,g,b)]
user_last_snapshot = {}
# configuration for snapshot size and pending timeout (seconds)
SNAPSHOT_SIZE = 100
PENDING_TIMEOUT = 10.0

def save_config(config: dict):
    """保存配置到本地 config.json"""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
        logging.info("配置已保存到 config.json")
    except Exception:
        logging.exception("保存配置失败")

def append_to_queue(paint_data):
    """将绘画数据添加到粘包队列"""
    global paint_queue, total_size
    paint_queue.append(paint_data)
    total_size += len(paint_data)

def get_merged_data():
    """合并队列中的所有数据块"""
    global paint_queue, total_size
    if not paint_queue:
        return None
    
    merged = bytearray(total_size)
    offset = 0
    for chunk in paint_queue:
        merged[offset:offset + len(chunk)] = chunk
        offset += len(chunk)
    
    paint_queue = []
    total_size = 0
    return merged

async def paint(ws, uid, token, r, g, b, x, y, paint_id):
    """准备绘画数据并加入队列"""
    try:
        # token 可能带或不带短横线，UUID 构造器对不同格式略敏感，做兼容处理
        try:
            token_bytes = UUID(token).bytes
        except Exception:
            # 试着移除短横线后以 hex 形式构造
            try:
                token_bytes = UUID(hex=token.replace('-', '')).bytes
            except Exception as e:
                logging.error(f"无效的 Token 格式: {token}，创建 UUID 失败: {e}")
                return
        # 构造绘画数据包
        # 操作码 (1) + x (2) + y (2) + rgb (3) + uid (3) + token (16) + id (4) = 31 字节
        paint_data = bytearray(31)
        paint_data[0] = 0xfe  # 操作码
        paint_data[1:3] = x.to_bytes(2, 'little')
        paint_data[3:5] = y.to_bytes(2, 'little')
        paint_data[5:8] = [r, g, b]
        paint_data[8:11] = uid.to_bytes(3, 'little')
        paint_data[11:27] = token_bytes
        paint_data[27:31] = paint_id.to_bytes(4, 'little')
        
        append_to_queue(paint_data)
    except Exception as e:
        logging.error(f"创建绘画数据时出错: {e}")

async def send_paint_data(ws, interval_ms):
    """定时发送粘合后的绘画数据包"""
    while True:
        await asyncio.sleep(interval_ms / 1000.0)
        # websockets 新旧 API 兼容：优先使用 ws.open，否则使用 not ws.closed
        try:
            is_open = getattr(ws, "open", None)
            if is_open is None:
                is_open = not getattr(ws, "closed", False)
        except Exception:
            is_open = False

        if is_open and paint_queue:
            merged_data = get_merged_data()
            if merged_data:
                try:
                    await ws.send(merged_data)
                    logging.debug(f"已发送 {len(merged_data)} 字节的绘画数据（粘包）。")
                except websockets.ConnectionClosed:
                    logging.warning("发送数据时 WebSocket 连接已关闭。")
                    break
                except Exception as e:
                    logging.error(f"发送数据时出错: {e}")

def build_target_map(pixels, width, height, start_x, start_y, config=None):
    """构建目标像素颜色映射：{(abs_x,abs_y): (r,g,b)}，跳过透明与越界。

    更健壮地支持像素格式：RGBA、RGB、灰度等。若像素带 alpha 通道且 alpha==0 则视为透明并跳过。
    若配置中设置 `ignore_semitransparent` 为 True，则 alpha<255 的像素也会被视为透明并跳过。
    """
    target = {}
    skipped_transparent = 0
    skipped_out_of_bounds = 0
    ignore_semi = False
    if isinstance(config, dict):
        ignore_semi = bool(config.get('ignore_semitransparent', False))

    total_pixels = width * height
    for py in range(height):
        for px in range(width):
            idx = py * width + px
            if idx < 0 or idx >= len(pixels):
                continue
            p = pixels[idx]
            try:
                # 支持多种像素表示
                if isinstance(p, (list, tuple)):
                    if len(p) >= 4:
                        r, g, b, a = p[0], p[1], p[2], p[3]
                    elif len(p) == 3:
                        r, g, b = p
                        a = 255
                    elif len(p) == 1:
                        r = g = b = p[0]
                        a = 255
                    else:
                        # 未识别格式，视为透明
                        skipped_transparent += 1
                        continue
                else:
                    # 纯整数等情况，视为灰度
                    r = g = b = int(p)
                    a = 255
            except Exception:
                skipped_transparent += 1
                continue

            # 跳过完全透明像素
            try:
                if int(a) == 0:
                    skipped_transparent += 1
                    continue
            except Exception:
                # 若无法解析 alpha，则保守处理为可见
                pass

            # 可选：跳过半透明像素
            if ignore_semi:
                try:
                    if 0 < int(a) < 255:
                        skipped_transparent += 1
                        continue
                except Exception:
                    pass

            abs_x, abs_y = start_x + px, start_y + py
            if 0 <= abs_x < 1000 and 0 <= abs_y < 600:
                target[(abs_x, abs_y)] = (int(r), int(g), int(b))
            else:
                skipped_out_of_bounds += 1

    logging.debug(f"目标像素数: {len(target)}（非透明且在画布范围内） 已跳过透明: {skipped_transparent} 越界: {skipped_out_of_bounds} 总像素: {total_pixels}")
    return target


def fetch_board_snapshot():
    """通过 HTTP 接口获取当前画板所有像素的快照，返回 dict {(x,y):(r,g,b)}。

    如果请求失败，返回空 dict（上层会把未知像素视为未达标）。
    """
    # 临时移除代理环境变量
    proxy_keys = ['HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy']
    _saved_env = {}
    for _k in proxy_keys:
        if _k in os.environ:
            _saved_env[_k] = os.environ.pop(_k)

    url = f"{API_BASE_URL}/api/paintboard/getboard"
    try:
        session = requests.Session()
        resp = session.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.content
        expected = 1000 * 600 * 3
        if len(data) < expected:
            logging.warning(f"获取画板快照数据长度不够: {len(data)} < {expected}")
        board = {}
        # 解析为 RGB 每像素 3 字节，小端/大端与顺序在文档中已指明为 r,g,b
        # 仅填充需要关注的坐标时更高效，上层传入 target_positions 后再筛选
        # 这里返回完整 map，调用方可根据需要筛选
        max_len = len(data) - 2
        for y in range(600):
            row_base = y * 1000 * 3
            for x in range(1000):
                idx = row_base + x * 3
                if idx + 2 > max_len:
                    break
                r = data[idx]
                g = data[idx + 1]
                b = data[idx + 2]
                board[(x, y)] = (r, g, b)
        logging.debug("已获取画板快照。")
        return board
    except requests.RequestException as e:
        logging.warning(f"获取画板快照失败: {e}")
        return {}
    except Exception as e:
        logging.exception(f"解析画板快照时出现异常: {e}")
        return {}
    finally:
        # 恢复环境变量
        if _saved_env:
            os.environ.update(_saved_env)


def get_draw_order(mode: str, width: int, height: int):
    """根据模式返回绘制顺序坐标列表（相对坐标）。

    支持模式：
    - horizontal: 从上到下、从左到右
    - concentric: 以图像中心为基准，按 Chebyshev 距离从小到大（近似同心扩散）
    - random: 随机顺序（使用固定种子确保可重现）
    """
    coords = [(x, y) for y in range(height) for x in range(width)]
    m = (mode or '').lower()
    if m == 'horizontal':
        return coords
    if m == 'concentric':
        cx = (width - 1) / 2.0
        cy = (height - 1) / 2.0
        return sorted(coords, key=lambda p: max(abs(p[0]-cx), abs(p[1]-cy)))
    if m == 'random':
        rnd = random.Random(width * 10007 + height * 97)
        rnd.shuffle(coords)
        return coords
    # 默认
    return coords


def load_config():
    """加载配置文件，返回 dict；若失败则记录错误并返回 None。"""
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        # 填充默认值
        cfg.setdefault('paint_interval_ms', 20)
        cfg.setdefault('round_interval_seconds', 30)
        cfg.setdefault('user_cooldown_seconds', 30)
        cfg.setdefault('users', [])
        
        # 兼容旧配置格式：如果有 image_path 但没有 images，则创建 images 列表
        if 'image_path' in cfg and 'images' not in cfg:
            cfg['images'] = [{
                'image_path': cfg.get('image_path', 'image.png'),
                'start_x': cfg.get('start_x', 0),
                'start_y': cfg.get('start_y', 0),
                'draw_mode': cfg.get('draw_mode', 'random'),
                'weight': 1.0,
                'enabled': True
            }]
        
        # 如果没有 images 配置，创建默认配置
        if 'images' not in cfg or not cfg['images']:
            cfg['images'] = [{
                'image_path': 'image.png',
                'start_x': 0,
                'start_y': 0,
                'draw_mode': 'random',
                'weight': 1.0,
                'enabled': True
            }]
        
        return cfg
    except FileNotFoundError:
        logging.warning("找不到 config.json，正在创建默认配置...")
        # 生成默认配置并写入
        default_cfg = {
            "users": [
                {
                    "uid": 114514,
                    "access_key": "AAAAAAA"
                }
            ],
            "paint_interval_ms": 20,
            "round_interval_seconds": 3,
            "user_cooldown_seconds": 3,
            "log_level": "INFO",
            "images": [
                {
                    "image_path": "image.png",
                    "start_x": 66,
                    "start_y": 64,
                    "draw_mode": "concentric",
                    "weight": 1.0,
                    "enabled": True
                }
            ]
        }
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(default_cfg, f, ensure_ascii=False, indent=4)
            logging.info("已创建默认 config.json，请根据需要编辑 users 与 images。")
            return default_cfg
        except Exception:
            logging.exception("创建默认 config.json 失败")
            return None
    except Exception:
        logging.exception("加载配置失败")
        return None


def load_image_pixels(config):
    """根据配置加载目标图片，返回 (pixels,width,height)

    pixels 为 RGBA 四元组列表。
    """
    image_path = config.get('image_path')
    if not image_path or not os.path.exists(image_path):
        logging.error(f"找不到目标图片: {image_path}")
        return None, 0, 0
    try:
        img = Image.open(image_path).convert('RGBA')
        width, height = img.size
        pixels = list(img.getdata())
        logging.debug(f"已加载目标图片: {image_path} 大小: {width}x{height}")
        return pixels, width, height
    except Exception:
        logging.exception("加载目标图片失败")
        return None, 0, 0


def get_token(uid: int, access_key: str):
    """从服务端获取绘制 token。返回 token 字符串或 None。"""
    # 临时移除代理环境变量
    proxy_keys = ['HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy']
    _saved_env = {}
    for _k in proxy_keys:
        if _k in os.environ:
            _saved_env[_k] = os.environ.pop(_k)
    
    try:
        session = requests.Session()
        # 禁用环境代理，避免被系统代理干扰
        try:
            session.trust_env = False
        except Exception:
            pass
        # 2025-10-14 文档与仓库说明：获取 Token 的接口为 POST /api/auth/gettoken
        url = f"{API_BASE_URL}/api/auth/gettoken"
        # 带指数回退的简易重试
        data = None
        delay = 1.0
        for attempt in range(5):
            try:
                resp = session.post(url, json={'uid': uid, 'access_key': access_key}, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                break
            except requests.HTTPError as e:
                logging.warning(f"获取 token 失败 uid={uid} (HTTP): {e}")
                break
            except Exception as e:
                logging.warning(f"获取 token 尝试 {attempt+1}/5 失败 uid={uid}: {e}")
                time.sleep(delay)
                delay = min(delay * 2, 10)
        if data is None:
            return None

        # 解析多种可能的响应包装，优先寻找 token 字段
        # 常见结构：{ "data": { "token": "..." }, "code":0 }
        token = None
        if isinstance(data, dict):
            # 直接 token
            token = data.get('token')
            if not token:
                # data 里可能嵌套
                inner = data.get('data') or data.get('result')
                if isinstance(inner, dict):
                    token = inner.get('token') or inner.get('paintToken')
            # 兼容旧字段
            if not token:
                token = data.get('paintToken')

        if not token:
            logging.warning(f"未从接口获取到 token，响应: {data}")
            return None
        return token
    except Exception as e:
        logging.warning(f"获取 token 失败 uid={uid}: {e}")
        return None
    finally:
        # 恢复环境变量
        if _saved_env:
            os.environ.update(_saved_env)


async def handle_websocket(config, users_with_tokens, images_data, debug=False, gui_state=None):
    """主 WebSocket 处理函数

    修复点：
    - 将发送/绘制/接收全部放入 WebSocket 上下文中，确保连接在任务期间保持打开。
    - 并发接收服务端消息，及时应答 0xfc 心跳为 0xfb，避免被断开。
    - 支持多图片绘制，合并目标映射并按权重处理重叠
    """
    paint_interval_ms = config.get("paint_interval_ms", 20)
    round_interval_seconds = config.get("round_interval_seconds", 30)
    user_cooldown_seconds = config.get("user_cooldown_seconds", 30)

    # 合并所有图片的目标像素映射（处理重叠，高权重优先）
    # tool.merge_target_maps 现在也返回每个绝对坐标对应的图片索引映射 pos_to_image_idx
    result = tool.merge_target_maps(images_data)
    # 兼容旧接口：如果返回两个值则填充空映射
    if isinstance(result, tuple) and len(result) == 3:
        target_map, positions_by_mode, pos_to_image_idx = result
    else:
        target_map, positions_by_mode = result
        pos_to_image_idx = {}
    
    # 合并所有绘制模式的坐标列表（保持原有顺序）
    target_positions = []
    for mode in ['horizontal', 'concentric', 'random']:  # 按优先级合并
        if mode in positions_by_mode:
            target_positions.extend(positions_by_mode[mode])
    # 构建按图片分组的坐标（用于公平派发）
    from collections import defaultdict, deque as _deque
    positions_by_image = defaultdict(list)
    for pos in target_positions:
        idx = pos_to_image_idx.get(pos)
        if idx is not None:
            positions_by_image[idx].append(pos)
    positions_by_image = {k: _deque(v) for k, v in positions_by_image.items()}
    
    logging.info(f"已加载 {len(images_data)} 个图片，合并后共 {len(target_map)} 个目标像素")

    # 如果有 GUI 状态对象，初始化一些可视化字段
    if gui_state is not None:
        with gui_state['lock']:
            gui_state['total'] = len(target_positions)
            gui_state['mismatched'] = len(target_positions)
            gui_state['available'] = len(users_with_tokens)
            gui_state['ready_count'] = len(users_with_tokens)
            gui_state['images_data'] = images_data  # 存储图片数据供 GUI 使用
            gui_state['stop'] = False


    # 临时移除环境中的代理变量，避免 websockets 库自动通过 HTTP_PROXY/HTTPS_PROXY 走代理
    proxy_keys = ['HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy']
    _saved_env = {}
    for _k in proxy_keys:
        if _k in os.environ:
            _saved_env[_k] = os.environ.pop(_k)
    if _saved_env:
        logging.debug("检测到环境代理变量，已临时清除以建立直接 WebSocket 连接。")

    # 记录本次连接开始时间，供上层决定是否重置退避
    conn_started_at = time.monotonic()
    try:
        # 限制握手与关闭超时，避免卡住；部分环境下可减轻“opening handshake 超时”的长期滞留
        async with websockets.connect(
            WS_URL,
            ping_interval=15,      # 每15秒发送一次 ping（更频繁，避免服务器超时断开）
            ping_timeout=45,       # 45秒超时（更宽松，适应网络波动）
            open_timeout=20,       # 增加握手超时到20秒
            close_timeout=10,
            max_size=10 * 1024 * 1024,  # 10MB 消息大小限制
        ) as ws:
            logging.info("WebSocket 连接已建立（心跳优化: ping间隔=15s）")
            # 新连接建立时清空待发送队列，避免残留数据
            try:
                tool.paint_queue.clear()
                if hasattr(tool, 'total_size'):
                    tool.total_size = 0
            except Exception:
                pass

            # 启动定时发送任务（粘包）
            sender_task = asyncio.create_task(tool.send_paint_data(ws, paint_interval_ms))

            # 维护当前画板已知颜色状态（初始化为服务端快照，以便重启后立即知道已达成的像素）
            board_state = {}
            try:
                snapshot = tool.fetch_board_snapshot()
                if snapshot:
                    # 使用完整快照，便于 GUI 显示整个画板
                    board_state = snapshot.copy()
                    # 若有 GUI，初始化 GUI 的 board_state（全板快照）
                    if gui_state is not None:
                        with gui_state['lock']:
                            gui_state['board_state'] = board_state.copy()
                            # expose pos->image mapping (initial)
                            gui_state['pos_to_image_idx'] = pos_to_image_idx
            except Exception:
                logging.debug("初始化画板快照时出现异常，继续使用空的 board_state。")
            # 画板状态改变事件（用于唤醒调度器进行即时修复）
            state_changed_event = asyncio.Event()

            # 启动接收任务：处理 Ping(0xfc)、绘画结果(0xff)、画板更新(0xfa) 等
            async def receiver():
                """接收并处理服务器消息，增强异常处理和心跳响应
                
                改进：
                - 更完善的异常捕获和分类处理
                - 连续错误计数和自动退出
                - 详细的错误日志
                """
                consecutive_errors = 0
                max_consecutive_errors = 10
                message_count = 0
                
                try:
                    async for message in ws:
                        message_count += 1
                        try:
                            if isinstance(message, str):
                                data = bytearray(message.encode())
                            else:
                                data = bytearray(message)
                            offset = 0
                            while offset < len(data):
                                opcode = data[offset]
                                offset += 1
                                if opcode == 0xfc:  # Heartbeat Ping
                                    logging.debug("收到 Ping，发送 Pong。")
                                    try:
                                        await ws.send(bytes([0xfb]))
                                        consecutive_errors = 0  # 成功响应心跳后重置错误计数
                                    except (websockets.exceptions.ConnectionClosed,
                                            websockets.exceptions.ConnectionClosedError,
                                            websockets.exceptions.ConnectionClosedOK) as e:
                                        err_msg = str(e) if str(e) else e.__class__.__name__
                                        logging.warning(f"响应 Pong 时连接已关闭: {err_msg}，接收任务退出。")
                                        return
                                    except Exception as e:
                                        err_msg = str(e) if str(e) else e.__class__.__name__
                                        logging.warning(f"响应 Pong 失败: {err_msg}")
                                        consecutive_errors += 1
                                        if consecutive_errors >= max_consecutive_errors:
                                            logging.error("心跳响应连续失败，接收任务退出。")
                                            return
                                elif opcode == 0xff:  # 绘画结果
                                    if offset + 5 > len(data):
                                        break
                                    paint_id = int.from_bytes(data[offset:offset+4], 'little')
                                    status_code = data[offset+4]
                                    logging.debug(f"收到绘画结果: ID={paint_id}, 状态=0x{status_code:x}")
                                    offset += 5
                                elif opcode == 0xfa:  # 画板像素更新广播 x(2) y(2) rgb(3)
                                    if offset + 7 > len(data):
                                        break
                                    try:
                                        x = int.from_bytes(data[offset:offset+2], 'little'); offset += 2
                                        y = int.from_bytes(data[offset:offset+2], 'little'); offset += 2
                                        r, g, b = data[offset], data[offset+1], data[offset+2]; offset += 3
                                        board_state[(x, y)] = (r, g, b)
                                        logging.debug(f"画板更新: ({x},{y}) -> ({r},{g},{b})")
                                        # attribute this board update to any pending paint that tried to set same pos/color recently
                                        try:
                                            now = time.monotonic()
                                            matched = None
                                            # find a pending paint that matches (x,y) and color within timeout
                                            for p in list(pending_paints):
                                                if p['pos'] == (x, y) and p['color'] == (r, g, b) and now - p['time'] <= PENDING_TIMEOUT:
                                                    matched = p
                                                    break
                                            if matched is not None:
                                                uid_matched = matched['uid']
                                                # record into user's last snapshot (ordered dict to keep recent entries)
                                                snap = user_last_snapshot.get(uid_matched)
                                                if snap is None:
                                                    snap = OrderedDict()
                                                    user_last_snapshot[uid_matched] = snap
                                                snap[matched['pos']] = matched['color']
                                                # keep only latest SNAPSHOT_SIZE items
                                                while len(snap) > SNAPSHOT_SIZE:
                                                    snap.popitem(last=False)
                                                # remove matched pending paint
                                                try:
                                                    pending_paints.remove(matched)
                                                except Exception:
                                                    pass
                                        except Exception:
                                            pass
                                        # 若更新触及目标区域，唤醒调度器以便即时修复
                                        if (x, y) in target_map:
                                            state_changed_event.set()
                                        # 同步到 GUI（实时画板预览）
                                        if gui_state is not None:
                                            try:
                                                with gui_state['lock']:
                                                    gui_state['board_state'][(x, y)] = (r, g, b)
                                            except Exception:
                                                pass
                                        # 清理过期的 pending_paints
                                        try:
                                            now = time.monotonic()
                                            pending_paints[:] = [p for p in pending_paints if now - p['time'] <= PENDING_TIMEOUT]
                                        except Exception:
                                            pass
                                    except Exception:
                                        # 出错则跳过此条
                                        pass
                                else:
                                    logging.warning(f"收到未知操作码: 0x{opcode:x}")
                            
                            # 成功处理消息后重置错误计数
                            consecutive_errors = 0
                            
                        except Exception as e:
                            err_msg = str(e) if str(e) else e.__class__.__name__
                            err_type = e.__class__.__name__
                            logging.warning(f"处理消息时出错 ({err_type}): {err_msg}")
                            consecutive_errors += 1
                            if consecutive_errors >= max_consecutive_errors:
                                logging.error(f"消息处理连续失败 {consecutive_errors} 次，接收任务退出。")
                                break
                    
                    # 正常退出循环（连接关闭）
                    logging.info(f"WebSocket 消息流结束（共接收 {message_count} 条消息），接收任务退出。")
                    
                except (websockets.exceptions.ConnectionClosed,
                        websockets.exceptions.ConnectionClosedError,
                        websockets.exceptions.ConnectionClosedOK) as e:
                    err_msg = str(e) if str(e) else e.__class__.__name__
                    logging.info(f"WebSocket 连接已关闭: {err_msg} (接收了 {message_count} 条消息)")
                except asyncio.CancelledError:
                    # 任务被取消时正常退出
                    logging.debug(f"接收任务被取消 (已接收 {message_count} 条消息)")
                    raise
                except Exception as e:
                    err_msg = str(e) if str(e) else e.__class__.__name__
                    err_type = e.__class__.__name__
                    logging.exception(f"WebSocket 接收处理时发生未预期异常 ({err_type}): {err_msg}")
                finally:
                    logging.info("接收任务已退出。")

            receiver_task = asyncio.create_task(receiver())

            # 启动进度显示器（每秒刷新）
            async def progress_printer():
                # 模式前缀：显示多图片信息
                mode_prefix = f"[{len(images_data)}图] "
                # history of (time, pct) for averaging over window_seconds
                history = deque()
                window_seconds = 60.0
                while True:
                    # 计算完成度（不符合的像素数会降低完成度）
                    total = len(target_positions)
                    mismatched = len([pos for pos in target_positions if board_state.get(pos) != target_map[pos]])
                    completed = max(0, total - mismatched)
                    pct = (completed / total * 100) if total > 0 else 100.0

                    # 可用用户数与就绪数
                    now = time.monotonic()
                    available = len(users_with_tokens)
                    ready_count = len([u for u in users_with_tokens if cooldown_until.get(u['uid'], 0.0) <= now])

                    # 计算用户级“被覆盖率”并得出全局抵抗率
                    resistance_pct = None
                    user_covered = {}
                    try:
                        total_users = len(users_with_tokens)
                        covered = 0
                        for u in users_with_tokens:
                            uid = u['uid']
                            snap = user_last_snapshot.get(uid)
                            covered_flag = False
                            if snap and len(snap) > 0:
                                for pos, color in snap.items():
                                    cur = board_state.get(pos)
                                    if cur is None or cur != color:
                                        covered_flag = True
                                        break
                            else:
                                covered_flag = False
                            user_covered[uid] = covered_flag
                            if covered_flag:
                                covered += 1
                        if total_users > 0:
                            resistance_pct = (covered / total_users) * 100.0
                        else:
                            resistance_pct = None
                    except Exception:
                        resistance_pct = None
                        user_covered = {}

                    # 更新 GUI 状态
                    if gui_state is not None:
                        with gui_state['lock']:
                            gui_state['total'] = total
                            gui_state['mismatched'] = mismatched
                            gui_state['available'] = len(users_with_tokens)
                            gui_state['ready_count'] = ready_count
                            gui_state['resistance_pct'] = resistance_pct
                            gui_state['user_covered'] = user_covered
                            # 显示累计派发次数（配置索引 -> 次数）
                            try:
                                gui_state['assigned_per_image'] = dict(assigned_counter_per_image)
                            except Exception:
                                gui_state['assigned_per_image'] = {}

                    # 进度条文本与条形
                    bar_len = 40
                    filled = int(bar_len * completed / total) if total > 0 else bar_len
                    bar = '#' * filled + '-' * (bar_len - filled)


                    # maintain history and compute average growth over last window_seconds
                    try:
                        history.append((now, pct))
                        # drop older than window
                        while history and (now - history[0][0] > window_seconds):
                            history.popleft()
                        growth = None
                        growth_str = ''
                        eta_str = ''
                        if len(history) >= 2:
                            t0, p0 = history[0]
                            t1, p1 = history[-1]
                            dt = max(1e-6, t1 - t0)
                            growth = (p1 - p0) / dt
                        if growth is None:
                            growth_str = '  增长: --'
                        else:
                            growth_str = f'  增长: {growth:+.2f}%/s'
                            if growth > 1e-6:
                                remain_pct = max(0.0, 100.0 - pct)
                                eta_s = remain_pct / growth
                                if eta_s >= 3600:
                                    eta_str = f'  估计剩余: {int(eta_s//3600)}h{int((eta_s%3600)//60)}m'
                                elif eta_s >= 60:
                                    eta_str = f'  估计剩余: {int(eta_s//60)}m{int(eta_s%60)}s'
                                else:
                                    eta_str = f'  估计剩余: {int(eta_s)}s'
                            else:
                                if pct < 95.0:
                                    eta_str = '  估计剩余: 我们正在被攻击，无法抵抗'
                                else:
                                    eta_str = '  估计剩余: 即将完成'
                    except Exception:
                        growth_str = '  增长: --'
                        eta_str = ''

                    # 判断危险状态：增长为负且进度小于95%
                    danger = False
                    try:
                        if growth is not None and growth < 0 and pct < 95.0:
                            danger = True
                    except Exception:
                        danger = False

                    # 构造最终输出行，优先包含抵抗率与增长/ETA
                    try:
                        if resistance_pct is None:
                            res_part = '  |  抵抗率: --'
                        else:
                            res_part = f'  |  抵抗率: {resistance_pct:5.1f}%'
                    except Exception:
                        res_part = ''

                    # 将输出行写到控制台；在危险时把进度条染为红色（ANSI）
                    try:
                        if danger:
                            red = '\x1b[31m'
                            reset = '\x1b[0m'
                            colored_bar = red + '[' + bar + ']' + reset
                            out_line = f"{mode_prefix}进度: {colored_bar} {pct:6.2f}% 可用用户: {available} (就绪:{ready_count})  未达标: {mismatched}{res_part}{growth_str}{eta_str}"
                        else:
                            out_line = f"{mode_prefix}进度: [{bar}] {pct:6.2f}% 可用用户: {available} (就绪:{ready_count})  未达标: {mismatched}{res_part}{growth_str}{eta_str}"
                        if debug:
                            logging.info(out_line)
                        else:
                            sys.stdout.write('\r' + out_line)
                            sys.stdout.flush()
                    except Exception:
                        pass

                    # 若 GUI 请求停止则退出循环
                    if gui_state is not None and gui_state.get('stop'):
                        logging.info('收到 GUI 退出信号，结束调度循环。')
                        return
                    await asyncio.sleep(1)

            # 调度：支持冷却与持续监视
            user_counters = {u['uid']: 0 for u in users_with_tokens}
            cooldown_until = {u['uid']: 0.0 for u in users_with_tokens}  # monotonic 时间戳
            in_watch_mode = False
            round_idx = 0
            # 累计派发计数（按配置索引聚合）
            from collections import defaultdict as _dd
            assigned_counter_per_image = _dd(int)

            # 现在 cooldown_until 已初始化，再启动进度显示器
            progress_task = asyncio.create_task(progress_printer())

            # 轮转图片列表与游标（避免某一模式/图片饿死）
            try:
                rr_images_raw = sorted(set(pos_to_image_idx.values()))
            except Exception:
                rr_images_raw = []
            # 根据 images_data 中记录的 config_index -> weight 构建加权轮转列表
            weight_map = {}
            try:
                for img in images_data:
                    cfg_idx = img.get('config_index')
                    if cfg_idx is not None:
                        weight_map[cfg_idx] = float(img.get('weight', 1.0))
            except Exception:
                weight_map = {}

            rr_images = []
            # 缩放因子：每个权重单位大约重复的基数（1.0 -> 10 次），可调
            scale = 10
            for cfg_idx in rr_images_raw:
                w = max(0.0, weight_map.get(cfg_idx, 1.0))
                repeat = max(1, int(round(w * scale)))
                rr_images.extend([cfg_idx] * repeat)
            # 若所有 weight 都异常导致 rr_images 为空，则回退为 raw 列表
            if not rr_images:
                rr_images = rr_images_raw
            # 调试信息：记录每个配置索引的权重与在轮转列表中的出现次数
            try:
                from collections import Counter
                dist = Counter(rr_images)
                logging.debug(f"轮转队列权重映射: {weight_map} -> 轮转分布: {dict(dist)} (scale={scale})")
            except Exception:
                pass
            rr_cursor = 0

            # 健康检查：定期验证连接和任务状态
            last_health_check = time.monotonic()
            health_check_interval = 10  # 每10秒进行一次完整健康检查
            last_task_check = time.monotonic()
            task_check_interval = 2  # 每2秒快速检查任务状态
            
            while True:
                now = time.monotonic()
                
                # 快速任务状态检查（每2秒）- 优先检测任务退出
                if now - last_task_check >= task_check_interval:
                    last_task_check = now
                    connection_issue = False
                    
                    # 检查发送任务状态
                    try:
                        if sender_task.done():
                            try:
                                exc = sender_task.exception() if not sender_task.cancelled() else None
                                if exc:
                                    logging.error(f"发送任务异常退出: {exc}")
                                else:
                                    logging.warning("发送任务已正常退出，可能由于连接问题")
                            except Exception as e:
                                logging.warning(f"发送任务已退出: {e}")
                            connection_issue = True
                    except Exception as e:
                        logging.debug(f"检查发送任务状态时出错: {e}")
                    
                    # 检查接收任务状态
                    try:
                        if receiver_task.done():
                            try:
                                exc = receiver_task.exception() if not receiver_task.cancelled() else None
                                if exc:
                                    logging.error(f"接收任务异常退出: {exc}")
                                else:
                                    logging.warning("接收任务已正常退出，可能由于连接问题")
                            except Exception as e:
                                logging.warning(f"接收任务已退出: {e}")
                            connection_issue = True
                    except Exception as e:
                        logging.debug(f"检查接收任务状态时出错: {e}")
                    
                    # 检查连接状态
                    try:
                        is_open = getattr(ws, "open", None)
                        if is_open is None:
                            is_open = not getattr(ws, "closed", False)
                        if not is_open:
                            logging.warning("检测到 WebSocket 已关闭")
                            connection_issue = True
                    except Exception as e:
                        logging.debug(f"检查连接状态时出错: {e}")
                        connection_issue = True
                    
                    # 如果检测到连接问题，立即清理并退出
                    if connection_issue:
                        logging.warning("检测到连接异常，退出当前循环以便重连。")
                        try:
                            progress_task.cancel()
                            sender_task.cancel()
                            receiver_task.cancel()
                            # 等待任务取消完成
                            await asyncio.wait_for(
                                asyncio.gather(progress_task, sender_task, receiver_task, return_exceptions=True),
                                timeout=2.0
                            )
                        except asyncio.TimeoutError:
                            logging.debug("等待任务取消超时")
                        except Exception as e:
                            logging.debug(f"取消任务时出错: {e}")
                        break
                
                # 完整健康检查（每10秒）- 包含详细日志
                if now - last_health_check >= health_check_interval:
                    last_health_check = now
                    
                    # 检查连接状态
                    try:
                        is_open = getattr(ws, "open", None)
                        if is_open is None:
                            is_open = not getattr(ws, "closed", False)
                    except Exception:
                        is_open = False
                    
                    if not is_open:
                        logging.warning("健康检查：检测到 WebSocket 已关闭，退出以便重连。")
                        try:
                            progress_task.cancel()
                            sender_task.cancel()
                            receiver_task.cancel()
                            await asyncio.wait_for(
                                asyncio.gather(progress_task, sender_task, receiver_task, return_exceptions=True),
                                timeout=2.0
                            )
                        except:
                            pass
                        break
                    
                    # 检查任务健康状态
                    sender_ok = not sender_task.done()
                    receiver_ok = not receiver_task.done()
                    progress_ok = not progress_task.done()
                    
                    if not sender_ok or not receiver_ok or not progress_ok:
                        logging.warning(f"健康检查：任务状态异常 [发送:{sender_ok} 接收:{receiver_ok} 进度:{progress_ok}]，退出以便重连。")
                        try:
                            progress_task.cancel()
                            sender_task.cancel()
                            receiver_task.cancel()
                            await asyncio.wait_for(
                                asyncio.gather(progress_task, sender_task, receiver_task, return_exceptions=True),
                                timeout=2.0
                            )
                        except:
                            pass
                        break
                    
                    logging.debug(f"健康检查通过：连接正常，所有任务运行中")
                # 优先处理 GUI 请求的配置刷新（如图片路径、起点或模式被修改并调用 refresh_config）
                if gui_state is not None and gui_state.get('reload_pixels'):
                    try:
                        with gui_state['lock']:
                            gui_state['reload_pixels'] = False
                            updated_images_data = gui_state.get('images_data', images_data)
                        # 重新构建目标映射与绘制顺序
                        _res = tool.merge_target_maps(updated_images_data)
                        if isinstance(_res, tuple) and len(_res) == 3:
                            target_map, positions_by_mode, pos_to_image_idx = _res
                        else:
                            target_map, positions_by_mode = _res
                            pos_to_image_idx = {}
                        target_positions = []
                        for mode in ['horizontal', 'concentric', 'random']:
                            if mode in positions_by_mode:
                                target_positions.extend(positions_by_mode[mode])
                        # 重建图片轮转队列
                        try:
                            rr_images = sorted(set(pos_to_image_idx.values()))
                        except Exception:
                            rr_images = []
                        rr_cursor = 0
                        # 新配置后重置累计派发计数
                        try:
                            assigned_counter_per_image.clear()
                        except Exception:
                            pass
                        if gui_state is not None:
                            with gui_state['lock']:
                                gui_state['total'] = len(target_positions)
                                gui_state['mismatched'] = len([pos for pos in target_positions if board_state.get(pos) != target_map[pos]])
                                gui_state['pos_to_image_idx'] = dict(pos_to_image_idx)
                        logging.info('已根据 GUI 请求刷新目标像素与绘制顺序。')
                    except Exception:
                        logging.exception('处理 GUI 刷新请求时出错')

                # 若 GUI 模式要求停止，退出循环
                if gui_state is not None and gui_state.get('stop'):
                    logging.info('收到 GUI 退出信号，结束主循环。')
                    # 立即取消所有后台任务
                    progress_task.cancel()
                    sender_task.cancel()
                    receiver_task.cancel()
                    break
                now = time.monotonic()
                # 未达目标色（未知状态也视为未完成）
                remaining = [pos for pos in target_positions if board_state.get(pos) != target_map[pos]]

                # 可用用户（不在冷却期）
                available_users = [u for u in users_with_tokens if cooldown_until.get(u['uid'], 0.0) <= now]

                assigned = 0
                if remaining and available_users:
                    # 公平派发：按照图片轮转，从各图片的队列取下一个未达标像素
                    # 若无法建立图片队列，则回退到全局顺序
                    # 先构建本轮的按图片剩余队列
                    rem_by_img = {}
                    if remaining:
                        tmp = {}
                        for pos in remaining:
                            i2 = pos_to_image_idx.get(pos)
                            if i2 is None:
                                continue
                            lst = tmp.setdefault(i2, [])
                            lst.append(pos)
                        from collections import deque as __dq
                        rem_by_img = {k: __dq(v) for k, v in tmp.items()}

                    for user in available_users:
                        if not remaining:
                            break
                        pick = None
                        picked_img_idx = None
                        if rr_images and rem_by_img:
                            tried = 0
                            while tried < len(rr_images):
                                img_idx = rr_images[rr_cursor % len(rr_images)]
                                q = rem_by_img.get(img_idx)
                                if q and len(q) > 0:
                                    pick = q.popleft()
                                    picked_img_idx = img_idx
                                    rr_cursor = (rr_cursor + 1) % max(1, len(rr_images))
                                    # 从全局 remaining 中移除此点
                                    try:
                                        remaining.remove(pick)
                                    except ValueError:
                                        pass
                                    break
                                else:
                                    rr_cursor = (rr_cursor + 1) % max(1, len(rr_images))
                                    tried += 1
                        if pick is None:
                            pick = remaining.pop(0)
                            picked_img_idx = pos_to_image_idx.get(pick)
                        x, y = pick
                        r, g, b = target_map[(x, y)]
                        uid = user['uid']
                        token = user['token']
                        paint_id = user_counters[uid]
                        await tool.paint(ws, uid, token, r, g, b, x, y, paint_id)
                        # 记录 pending paint，待画板更新广播到来时归属成功绘制
                        try:
                            image_idx = picked_img_idx if picked_img_idx is not None else pos_to_image_idx.get((x, y)) if 'pos_to_image_idx' in locals() else None
                            pending_paints.append({
                                'uid': uid,
                                'paint_id': paint_id,
                                'pos': (x, y),
                                'color': (r, g, b),
                                'time': time.monotonic(),
                                'image_idx': image_idx
                            })
                            # 累计派发计数（按配置索引）
                            if image_idx is not None:
                                assigned_counter_per_image[image_idx] += 1
                        except Exception:
                            pass
                        logging.info(f"UID {uid} 播报绘制像素: ({x},{y}) color=({r},{g},{b}) id={paint_id}")
                        user_counters[uid] = paint_id + 1
                        cooldown_until[uid] = now + user_cooldown_seconds
                        assigned += 1

                    # 输出轮次/进度日志
                    round_idx += 1
                    left = len([pos for pos in target_positions if board_state.get(pos) != target_map[pos]])
                    logging.info(f"第 {round_idx} 次调度：分配 {assigned} 个修复任务，剩余未达标 {left}。")

                # 计算等待策略
                if not remaining:
                    # 已全部达标，进入/保持监视模式
                    if not in_watch_mode:
                        logging.info("所有目标像素已达到目标颜色，进入监视模式：将持续监听画板偏移并即时修复。")
                        in_watch_mode = True
                    # 等待事件或超时，避免繁忙循环
                    try:
                        await asyncio.wait_for(state_changed_event.wait(), timeout=round_interval_seconds)
                    except asyncio.TimeoutError:
                        pass
                    finally:
                        state_changed_event.clear()
                    continue

                # 有未完成像素但本轮未能分配（可能因为都在冷却）
                if remaining and assigned == 0:
                    # 统计冷却剩余时间
                    cool_list = []
                    for user in users_with_tokens:
                        uid = user['uid']
                        rem = max(0.0, cooldown_until.get(uid, 0.0) - now)
                        cool_list.append(f"{uid}:{int(rem)}s")
                    logging.info("暂无可用用户，本轮跳过。冷却剩余：" + ", ".join(cool_list))

                    # 计算下一个冷却到期或等待上限
                    future_times = [cooldown_until[u['uid']] for u in users_with_tokens if cooldown_until[u['uid']] > now]
                    next_ready_in = min([(t - now) for t in future_times], default=round_interval_seconds)
                    timeout = max(0.5, min(round_interval_seconds, next_ready_in))
                    try:
                        await asyncio.wait_for(state_changed_event.wait(), timeout=timeout)
                    except asyncio.TimeoutError:
                        pass
                    finally:
                        state_changed_event.clear()
                    continue

                # 有分配则短暂让出控制权（粘包发送会在后台周期触发）
                await asyncio.sleep(0.1)

            # 不再退出，理论上保持在 while True；若意外跳出则继续走到发送清空
            logging.info("调度循环结束（意外），等待剩余数据发送...")

            # 确保所有后台任务都被取消（如果还没被取消）
            if not progress_task.cancelled():
                progress_task.cancel()
            if not sender_task.cancelled():
                sender_task.cancel()
            if not receiver_task.cancelled():
                receiver_task.cancel()

            # 等待任务完全退出（最多等待1秒）
            try:
                await asyncio.wait_for(
                    asyncio.gather(progress_task, sender_task, receiver_task, return_exceptions=True),
                    timeout=1.0
                )
            except asyncio.TimeoutError:
                logging.warning("等待后台任务退出超时，但继续清理")
            except Exception:
                pass

            # 等待发送队列清空
            # paint_queue 已经被移到 tool.paint_queue 实现为模块全局
            while getattr(tool, 'paint_queue', []) and not getattr(ws, "closed", False):
                await asyncio.sleep(0.5)

            logging.info("所有数据已发送/处理，准备关闭连接。")
    finally:
        # 恢复之前的环境变量（如果有）
        if _saved_env:
            os.environ.update(_saved_env)
            logging.debug("已恢复环境代理变量。")
    # 将连接持续时长通过返回值暴露给上层（用于退避重置判定）
    try:
        return max(0.0, time.monotonic() - conn_started_at)
    except Exception:
        return 0.0


async def run_forever(config, users_with_tokens, images_data, debug=False, gui_state=None):
    """带自动重连的持久运行包装器。

    当网络断开、服务器重启或偶发异常导致内部循环退出时，按指数回退重连，
    直到收到 GUI 停止信号或进程被终止。
    
    优化：
    - 更智能的退避策略
    - 连接成功后重置退避
    - 详细的重连日志和诊断信息
    - 统计连接质量指标
    """
    backoff = 1.0
    backoff_max = 60.0
    reconnect_count = 0
    last_successful_duration = 0
    total_connected_time = 0.0
    successful_connections = 0
    
    while True:
        # GUI 请求停止则退出
        if gui_state is not None and gui_state.get('stop'):
            logging.info('检测到停止标记，结束 run_forever 循环。')
            break
        reconnect_count += 1
        connection_start = time.monotonic()
        exit_reason = "未知原因"
        
        try:
            if reconnect_count == 1:
                logging.info(f"初始连接 WebSocket...")
            else:
                logging.info(f"尝试重新连接 WebSocket (第 {reconnect_count} 次尝试，累计成功连接: {successful_connections} 次，总连接时长: {total_connected_time:.1f}s)...")
            
            duration = await handle_websocket(config, users_with_tokens, images_data, debug, gui_state=gui_state)
            last_successful_duration = duration if isinstance(duration, (int, float)) else 0
            
            # 统计成功连接
            if last_successful_duration > 0:
                successful_connections += 1
                total_connected_time += last_successful_duration
            
            # 若自然返回且未设置停止，视为异常退出，进入重连
            if gui_state is not None and gui_state.get('stop'):
                exit_reason = "用户停止"
                break
            
            exit_reason = "主循环正常退出"
            
            # 分析连接质量并调整重连策略
            if isinstance(duration, (int, float)):
                if duration >= 60.0:
                    # 连接稳定超过1分钟，认为网络质量良好
                    backoff = 1.0
                    reconnect_count = 0
                    avg_duration = total_connected_time / successful_connections if successful_connections > 0 else 0
                    logging.info(f'连接持续时间较长({duration:.1f}s)，网络质量良好。平均连接时长: {avg_duration:.1f}s，已重置重连退避。')
                elif duration >= 30.0:
                    # 连接持续30-60秒，部分稳定
                    backoff = max(1.0, backoff / 2)
                    logging.info(f'连接持续时间中等({duration:.1f}s)，已降低重连退避至 {backoff:.1f}s。')
                elif duration >= 10.0:
                    # 连接持续10-30秒，网络不稳定
                    backoff = min(backoff * 1.5, backoff_max)
                    logging.warning(f'连接持续时间较短({duration:.1f}s)，网络可能不稳定，退避增加至 {backoff:.1f}s。')
                else:
                    # 连接持续不到10秒，网络很不稳定或服务器限制
                    backoff = min(backoff * 2.0, backoff_max)
                    logging.warning(f'连接持续时间很短({duration:.1f}s)，可能遇到连接限制或严重网络问题，退避增加至 {backoff:.1f}s。')
                
                # 计算连接稳定性评分 (0-100)
                stability_score = min(100, (duration / 300.0) * 100) if duration > 0 else 0
                logging.info(f'本次连接稳定性评分: {stability_score:.1f}/100')
            
            logging.warning(f'主循环意外结束（运行时长: {last_successful_duration:.1f}s，原因: {exit_reason}），将在短暂等待后尝试重连。')
            
        except (websockets.exceptions.ConnectionClosedError,
                websockets.exceptions.ConnectionClosedOK,
                websockets.exceptions.InvalidStatusCode,
                OSError,
                asyncio.TimeoutError) as e:
            # 某些异常的 str 可能为空，补充类型名便于诊断
            err_text = str(e) or e.__class__.__name__
            err_type = e.__class__.__name__
            connection_duration = time.monotonic() - connection_start
            
            # 详细分类错误类型
            if isinstance(e, websockets.exceptions.InvalidStatusCode):
                exit_reason = f"服务器返回异常状态码: {err_text}"
                logging.warning(f'{exit_reason} (第 {reconnect_count} 次尝试，已连接 {connection_duration:.1f}s)')
                # 状态码错误可能是服务器限制，使用较大退避
                backoff = min(backoff * 2.5, backoff_max)
            elif isinstance(e, asyncio.TimeoutError):
                exit_reason = "连接超时"
                logging.warning(f'{exit_reason} (第 {reconnect_count} 次尝试，已连接 {connection_duration:.1f}s)')
                # 超时可能是网络问题，适度退避
                backoff = min(backoff * 1.8, backoff_max)
            elif isinstance(e, OSError):
                exit_reason = f"操作系统网络错误: {err_text}"
                logging.warning(f'{exit_reason} (第 {reconnect_count} 次尝试，已连接 {connection_duration:.1f}s)')
                # OS错误通常是严重网络问题，使用较大退避
                backoff = min(backoff * 2.0, backoff_max)
            else:
                exit_reason = f"连接异常关闭: {err_text}"
                logging.warning(f'{exit_reason} (第 {reconnect_count} 次尝试，已连接 {connection_duration:.1f}s)')
                # 网络相关异常，根据之前的连接时长调整
                if last_successful_duration >= 10.0:
                    backoff = min(backoff * 1.5, backoff_max)
                else:
                    backoff = min(backoff * 2.0, backoff_max)
                    
        except Exception as e:
            err_text = str(e) or e.__class__.__name__
            err_type = e.__class__.__name__
            connection_duration = time.monotonic() - connection_start
            exit_reason = f"未预期异常 ({err_type}): {err_text}"
            logging.exception(f'运行过程中出现未预期异常 (第 {reconnect_count} 次尝试，已连接 {connection_duration:.1f}s)，准备重连。')
            # 未预期异常，使用较大的退避
            backoff = min(backoff * 2.5, backoff_max)

        # 指数回退的等待；等待期间可响应停止
        wait_left = backoff
        if reconnect_count == 1:
            logging.info(f'将在 {wait_left:.1f}s 后进行首次重连。退出原因: {exit_reason}')
        else:
            # 计算预计下次连接的稳定性
            avg_duration = total_connected_time / successful_connections if successful_connections > 0 else 0
            logging.info(f'将在 {wait_left:.1f}s 后进行第 {reconnect_count + 1} 次重连尝试。')
            logging.info(f'连接统计 - 成功: {successful_connections} 次，平均时长: {avg_duration:.1f}s，退出原因: {exit_reason}')
        
        while wait_left > 0:
            if gui_state is not None and gui_state.get('stop'):
                logging.info('检测到停止标记，放弃重连等待。')
                return
            await asyncio.sleep(min(1.0, wait_left))
            wait_left -= 1.0
    
    # 输出最终统计
    if successful_connections > 0:
        avg_duration = total_connected_time / successful_connections
        logging.info(f'run_forever 退出。总连接次数: {successful_connections}，总时长: {total_connected_time:.1f}s，平均时长: {avg_duration:.1f}s')
    else:
        logging.info('run_forever 正常退出（无成功连接）。')


def main():
    """主函数"""
    # 解析命令行参数（支持 -debug 和 -cli）
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('-debug', action='store_true', help='启用详细日志（DEBUG）并显示完整日志）')
    parser.add_argument('-cli', action='store_true', help='仅命令行模式，禁用 GUI')
    args, _ = parser.parse_known_args()
    debug = bool(args.debug)
    cli_only = bool(args.cli)

    config = load_config()
    if not config:
        return

    # 加载所有图片
    images_data = tool.load_all_images(config)
    if not images_data:
        logging.error("没有可用的图片配置，程序退出。")
        return

    # 获取 token 阶段：在 GUI 模式下显示进度条避免无响应感
    def get_tokens_with_progress(users_list, allow_gui=True):
        results = []
        total = len(users_list)
        # 尝试在主线程创建一个临时 Tk 窗口（仅用于进度显示）
        use_tk = False
        progress = None
        root = None
        try:
            if not cli_only and allow_gui:
                import tkinter as tk
                from tkinter import ttk
                root = tk.Tk()
                root.title('获取 Token 中...')
                root.geometry('400x80')
                ttk.Label(root, text='正在获取用户 Token，请稍候...').pack(pady=(8,0))
                progress = ttk.Progressbar(root, orient='horizontal', length=360, mode='determinate', maximum=total)
                progress.pack(pady=(6,8))
                root.update()
                use_tk = True
        except Exception:
            # 无法创建 GUI，回退到控制台
            use_tk = False

        idx = 0
        for user in users_list:
            uid = user.get('uid')
            ak = user.get('access_key')
            token = None
            token_from_config = user.get('token')
            token_time = user.get('token_time', 0)
            now_ts = int(time.time())
            # 若配置中有 access_key，则优先通过接口获取真实 token，失败再回退到配置中的 token
            if ak:
                # 如果配置中已有 token 且 token_time 在 1 day 内，则复用并跳过请求
                if token_from_config and token_time and (now_ts - int(token_time) < 86400):
                    token = token_from_config
                else:
                    fetched = None
                    try:
                        fetched = get_token(uid, ak)
                    except Exception:
                        fetched = None
                    if fetched:
                        token = fetched
                        # 将新获取的 token 及时间写回配置并持久化
                        try:
                            user['token'] = token
                            user['token_time'] = now_ts
                            save_config(config)
                        except Exception:
                            logging.exception('写回 token 到 config 失败')
                    elif token_from_config:
                        # 获取失败，回退使用配置中的 token（可能无 token_time 或已过期），记录警告并保存当前时间以避免频繁重试
                        token = token_from_config
                        logging.warning(f"获取 token 失败，回退使用配置中的 token: uid={uid}")
                        try:
                            user['token_time'] = now_ts
                            save_config(config)
                        except Exception:
                            pass
                    else:
                        logging.warning(f"既无法通过 access_key 获取 token，也未在配置中提供 token: uid={uid}，跳过。")
            else:
                # 无 access_key，若配置含 token 则直接使用
                if token_from_config:
                    # 若存在 token_time 并且未过期则直接使用；否则标记当前时间并保存
                    if token_time and (now_ts - int(token_time) < 86400):
                        token = token_from_config
                    else:
                        token = token_from_config
                        try:
                            user['token_time'] = now_ts
                            save_config(config)
                        except Exception:
                            pass
                else:
                    logging.warning(f"用户条目缺少 access_key 且未提供 token: uid={uid}，跳过。")

            if token:
                results.append({'uid': uid, 'token': token})
            idx += 1
            if use_tk and progress is not None:
                try:
                    progress['value'] = idx
                    root.update()
                except Exception:
                    pass
            else:
                sys.stdout.write(f'获取 token 进度: {idx}/{total}\r')
                sys.stdout.flush()

        if use_tk and root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        else:
            print('')
        return results

    users_with_tokens = get_tokens_with_progress(config.get('users', []), allow_gui=True)
    
    if not users_with_tokens:
        if cli_only:
            logging.error("没有可用的用户 Token，程序退出（CLI 模式）。")
            return
        else:
            logging.warning("当前没有可用的用户 Token，进入 GUI 模式以便手动添加/填写 Token。继续启动 GUI...")

    # 根据 -debug 决定控制台日志行为：非 debug 模式下把 StreamHandler 设为 WARNING，以便只显示进度条
    if not debug:
        root = logging.getLogger()
        for h in root.handlers:
            if isinstance(h, logging.StreamHandler):
                h.setLevel(logging.WARNING)
    else:
        # 若启用 debug，则把全局日志级别调到 DEBUG
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        gui_available = False
        start_gui_func = None
        if not cli_only:
            try:
                # 动态导入 GUI 前端
                import gui
                start_gui_func = gui.start_gui
                gui_available = True
            except Exception as e:
                logging.warning(f"GUI 不可用，回退到 CLI：{e}")

        # 定义刷新配置函数（仅刷新图片配置，不重新获取 token）
        def refresh_config(new_cfg: dict):
            """在不重新获取 token 的情况下刷新运行期配置并更新 GUI 状态。"""
            try:
                # 保存到文件
                save_config(new_cfg)
            except Exception:
                logging.exception('保存配置失败')
            # 更新内存中的 config 引用
            nonlocal config, images_data
            config.update(new_cfg)
            # 重新加载所有图片
            try:
                # Always update images_data with the latest result (may be empty list)
                new_images_data = tool.load_all_images(config) or []
                images_data = new_images_data
                if images_data:
                    logging.info('已刷新所有图片数据（未重新获取 Token）。')
                else:
                    logging.warning('刷新配置时未能加载到任何图片（可能都被禁用或路径无效）。')
            except Exception:
                logging.exception('刷新配置时加载图片失败')
            # 如果 GUI 可用，更新 gui_state 的相关字段
            try:
                if gui_available:
                    with gui_state['lock']:
                        # Update gui_state even if images_data is empty so backend rebuilds its maps
                        gui_state['images_data'] = images_data
                        gui_state['reload_pixels'] = True
            except Exception:
                logging.exception('刷新配置时更新 GUI 状态失败')

        # 将刷新回调注册到 gui 模块（GUI 将调用此回调以替代重启）
        try:
            if gui_available:
                gui.REFRESH_CALLBACK = refresh_config
        except Exception:
            pass

        if cli_only or not gui_available:
            # CLI/无 GUI：使用带重连的持久运行
            asyncio.run(run_forever(config, users_with_tokens, images_data, debug))
        else:
            # GUI 模式：创建线程安全的 gui_state 并在后台运行 asyncio
            gui_state = {
                'lock': threading.RLock(),
                'stop': False,
                'board_state': {},
                'overlay': False,
                'images_data': images_data,
            }

            def run_asyncio_loop():
                try:
                    asyncio.run(run_forever(config, users_with_tokens, images_data, debug, gui_state=gui_state))
                except Exception:
                    logging.exception('后台 asyncio 任务异常')

            t = threading.Thread(target=run_asyncio_loop, daemon=True)
            t.start()

            # 启动 Tkinter GUI（主线程）
            try:
                start_gui_func(config, images_data, users_with_tokens, gui_state)
            finally:
                # GUI 关闭时请求后台停止
                with gui_state['lock']:
                    gui_state['stop'] = True
                t.join(timeout=5)
                logging.info('GUI 退出，后台任务已请求停止。')
    except KeyboardInterrupt:
        logging.info("检测到手动中断，程序退出。")
    except Exception:
        logging.exception("程序运行时发生未捕获的异常")

if __name__ == "__main__":
    main()
