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
import threading
from collections import OrderedDict

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

# 发送粘包所用的全局队列
paint_queue = []
total_size = 0
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
                    logging.info(f"已发送 {len(merged_data)} 字节的绘画数据（粘包）。")
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

    logging.info(f"目标像素数: {len(target)}（非透明且在画布范围内） 已跳过透明: {skipped_transparent} 越界: {skipped_out_of_bounds} 总像素: {total_pixels}")
    return target


def fetch_board_snapshot():
    """通过 HTTP 接口获取当前画板所有像素的快照，返回 dict {(x,y):(r,g,b)}。

    如果请求失败，返回空 dict（上层会把未知像素视为未达标）。
    """
    url = f"{API_BASE_URL}/api/paintboard/getboard"
    try:
        session = requests.Session()
        session.trust_env = False
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
        logging.info("已获取画板快照。")
        return board
    except requests.RequestException as e:
        logging.warning(f"获取画板快照失败: {e}")
        return {}
    except Exception as e:
        logging.exception(f"解析画板快照时出现异常: {e}")
        return {}


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
        cfg.setdefault('draw_mode', 'random')
        cfg.setdefault('image_path', 'image.png')
        cfg.setdefault('start_x', 0)
        cfg.setdefault('start_y', 0)
        cfg.setdefault('users', [])
        return cfg
    except FileNotFoundError:
        logging.warning("找不到 config.json，正在创建默认配置...")
        # 生成默认配置并写入
        default_cfg = {
            # 示例用户：请替换为真实 UID 与 AccessKey，或在 GUI 中添加用户
            "users": [
                {
                    "uid": 114514,
                    "access_key": "AAAAAAA"
                }
            ],
            "paint_interval_ms": 20,
            # 与 config.json.del 保持一致的默认值
            "round_interval_seconds": 3,
            "user_cooldown_seconds": 3,
            "draw_mode": "concentric",
            "log_level": "INFO",
            "image_path": "image.png",
            "start_x": 66,
            "start_y": 64
        }
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(default_cfg, f, ensure_ascii=False, indent=4)
            logging.info("已创建默认 config.json，请根据需要编辑 users 与 image_path。")
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
        logging.info(f"已加载目标图片: {image_path} 大小: {width}x{height}")
        return pixels, width, height
    except Exception:
        logging.exception("加载目标图片失败")
        return None, 0, 0


def get_token(uid: int, access_key: str):
    """从服务端获取绘制 token。返回 token 字符串或 None。"""
    try:
        session = requests.Session()
        session.trust_env = False
        # 2025-10-14 文档与仓库说明：获取 Token 的接口为 POST /api/auth/gettoken
        url = f"{API_BASE_URL}/api/auth/gettoken"
        try:
            resp = session.post(url, json={'uid': uid, 'access_key': access_key}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except requests.HTTPError as e:
            logging.warning(f"获取 token 失败 uid={uid}: {e}")
            return None
        except Exception as e:
            logging.warning(f"获取 token 时发生异常 uid={uid}: {e}")
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


async def handle_websocket(config, users_with_tokens, pixels, width, height, debug=False, gui_state=None):
    """主 WebSocket 处理函数

    修复点：
    - 将发送/绘制/接收全部放入 WebSocket 上下文中，确保连接在任务期间保持打开。
    - 并发接收服务端消息，及时应答 0xfc 心跳为 0xfb，避免被断开。
    """
    draw_mode = config.get("draw_mode", "random")
    start_x = config.get("start_x", 0)
    start_y = config.get("start_y", 0)
    paint_interval_ms = config.get("paint_interval_ms", 20)
    round_interval_seconds = config.get("round_interval_seconds", 30)
    user_cooldown_seconds = config.get("user_cooldown_seconds", 30)

    # 目标像素映射（绝对坐标 -> 目标 RGB）
    target_map = build_target_map(pixels, width, height, start_x, start_y)
    
    # 根据模式生成有序的绘画坐标
    ordered_coords = get_draw_order(draw_mode, width, height)
    # 转换为绝对坐标
    target_positions = [(start_x + x, start_y + y) for x, y in ordered_coords if (start_x + x, start_y + y) in target_map]
    current_draw_mode = draw_mode
    current_start_x, current_start_y = start_x, start_y

    # 如果有 GUI 状态对象，初始化一些可视化字段
    if gui_state is not None:
        with gui_state['lock']:
            gui_state['total'] = len(target_positions)
            gui_state['mismatched'] = len(target_positions)
            gui_state['available'] = len(users_with_tokens)
            gui_state['ready_count'] = len(users_with_tokens)
            gui_state['target_bbox'] = (start_x, start_y, width, height)
            gui_state['stop'] = False


    # 临时移除环境中的代理变量，避免 websockets 库自动通过 HTTP_PROXY/HTTPS_PROXY 走代理
    proxy_keys = ['HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy']
    _saved_env = {}
    for _k in proxy_keys:
        if _k in os.environ:
            _saved_env[_k] = os.environ.pop(_k)
    if _saved_env:
        logging.info("检测到环境代理变量，已临时清除以建立直接 WebSocket 连接。")

    try:
        async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
            logging.info("WebSocket 连接已打开。")

            # 启动定时发送任务（粘包）
            sender_task = asyncio.create_task(send_paint_data(ws, paint_interval_ms))

            # 维护当前画板已知颜色状态（初始化为服务端快照，以便重启后立即知道已达成的像素）
            board_state = {}
            try:
                snapshot = fetch_board_snapshot()
                if snapshot:
                    # 使用完整快照，便于 GUI 显示整个画板
                    board_state = snapshot.copy()
                    # 若有 GUI，初始化 GUI 的 board_state（全板快照）
                    if gui_state is not None:
                        with gui_state['lock']:
                            gui_state['board_state'] = board_state.copy()
            except Exception:
                logging.debug("初始化画板快照时出现异常，继续使用空的 board_state。")
            # 画板状态改变事件（用于唤醒调度器进行即时修复）
            state_changed_event = asyncio.Event()

            # 启动接收任务：处理 Ping(0xfc)、绘画结果(0xff)、画板更新(0xfa) 等
            async def receiver():
                try:
                    async for message in ws:
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
                                await ws.send(bytes([0xfb]))
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
                except websockets.exceptions.ConnectionClosed as e:
                    logging.info(f"WebSocket 连接已关闭: {e}")
                except asyncio.CancelledError:
                    # 任务被取消时正常退出
                    pass
                except Exception:
                    logging.exception("WebSocket 接收处理时发生错误")

            receiver_task = asyncio.create_task(receiver())

            # 启动进度显示器（每秒刷新）
            async def progress_printer():
                # 模式前缀，例如: [模式:horizontal] 
                mode_prefix = f"[模式:{draw_mode}] "
                while True:
                    # 计算完成度（不符合的像素数会降低完成度）
                    total = len(target_positions)
                    mismatched = len([pos for pos in target_positions if board_state.get(pos) != target_map[pos]])
                    completed = max(0, total - mismatched)
                    pct = (completed / total * 100) if total > 0 else 100.0
                    # 可用用户数
                    now = time.monotonic()
                    # 逻辑修正：冷却中的用户仍应被视为“可用”（他们拥有有效 token 并未因绘制失败失效）
                    # 因此这里把可用计数设为所有已获取 token 的用户数，同时计算就绪（未冷却）用户数用于分配显示
                    available = len(users_with_tokens)
                    ready_count = len([u for u in users_with_tokens if cooldown_until.get(u['uid'], 0.0) <= now])
                    bar_len = 40
                    filled = int(bar_len * completed / total) if total > 0 else bar_len
                    bar = '#' * filled + '-' * (bar_len - filled)
                    # 显示格式：在进度条左侧加入模式前缀
                    line = f"{mode_prefix}进度: [{bar}] {pct:6.2f}% 可用用户: {available} (就绪:{ready_count})  未达标: {mismatched}"
                    if debug:
                        logging.info(line)
                    else:
                        # 非 debug 模式：只在控制台打印进度条（刷新）
                        sys.stdout.write('\r' + line)
                        sys.stdout.flush()
                    # 计算用户级“被覆盖率”并得出全局抵抗率：
                    # 抵抗率定义（按你的要求）：在所有用户中，统计其上次作画的像素是否被覆盖（若 snapshot 中任意像素与当前画板不同则视为被覆盖），
                    # 抵抗率 = 被覆盖的用户数 / 总用户数 * 100%
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
                                # 若 snapshot 中存在任一像素与当前画板不一致，则认为该用户的上次作画被覆盖
                                for pos, color in snap.items():
                                    cur = board_state.get(pos)
                                    if cur is None or cur != color:
                                        covered_flag = True
                                        break
                            else:
                                # 无 snapshot 的用户视为未被覆盖（按定义只统计已有快照的覆盖情况）
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
                    # 在 CLI 中附加简短的抵抗率展示
                    try:
                        if resistance_pct is None:
                            line = line + '  |  抵抗率: --'
                        else:
                            line = line + f'  |  抵抗率: {resistance_pct:5.1f}%'
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

            # 现在 cooldown_until 已初始化，再启动进度显示器
            progress_task = asyncio.create_task(progress_printer())

            while True:
                # 检查 GUI 是否修改了绘制模式
                if gui_state is not None:
                    with gui_state['lock']:
                        new_mode = gui_state.get('draw_mode', current_draw_mode)
                        new_start_x = gui_state.get('start_x', current_start_x)
                        new_start_y = gui_state.get('start_y', current_start_y)
                else:
                    new_mode = current_draw_mode
                    new_start_x, new_start_y = current_start_x, current_start_y
                if new_mode != current_draw_mode:
                    logging.info(f"检测到模式切换: {current_draw_mode} -> {new_mode}，重新生成绘制顺序。")
                    current_draw_mode = new_mode
                    ordered_coords = get_draw_order(current_draw_mode, width, height)
                    target_positions = [(current_start_x + x, current_start_y + y) for x, y in ordered_coords if (current_start_x + x, current_start_y + y) in target_map]
                    if gui_state is not None:
                        with gui_state['lock']:
                            gui_state['total'] = len(target_positions)
                # 检查起点坐标变化
                if (new_start_x, new_start_y) != (current_start_x, current_start_y):
                    logging.info(f"检测到起点变化: ({current_start_x},{current_start_y}) -> ({new_start_x},{new_start_y})，重建目标映射与绘制顺序。")
                    current_start_x, current_start_y = new_start_x, new_start_y
                    target_map = build_target_map(pixels, width, height, current_start_x, current_start_y)
                    ordered_coords = get_draw_order(current_draw_mode, width, height)
                    target_positions = [(current_start_x + x, current_start_y + y) for x, y in ordered_coords if (current_start_x + x, current_start_y + y) in target_map]
                    if gui_state is not None:
                        with gui_state['lock']:
                            gui_state['total'] = len(target_positions)
                # 若 GUI 模式要求停止，退出循环
                if gui_state is not None and gui_state.get('stop'):
                    logging.info('收到 GUI 退出信号，结束主循环。')
                    break
                now = time.monotonic()
                # 未达目标色（未知状态也视为未完成）
                remaining = [pos for pos in target_positions if board_state.get(pos) != target_map[pos]]

                # 可用用户（不在冷却期）
                available_users = [u for u in users_with_tokens if cooldown_until.get(u['uid'], 0.0) <= now]

                assigned = 0
                if remaining and available_users:
                    # 立即为可用用户分配修复任务（每人 1 个）
                    for user in available_users:
                        if not remaining:
                            break
                        x, y = remaining.pop(0) # 从列表头部取点，以遵循原始顺序
                        r, g, b = target_map[(x, y)]
                        uid = user['uid']
                        token = user['token']
                        paint_id = user_counters[uid]
                        await paint(ws, uid, token, r, g, b, x, y, paint_id)
                        # 记录 pending paint，待画板更新广播到来时归属成功绘制
                        try:
                            pending_paints.append({'uid': uid, 'paint_id': paint_id, 'pos': (x, y), 'color': (r, g, b), 'time': time.monotonic()})
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

            # 取消进度任务
            progress_task.cancel()

            # 等待发送队列清空
            while paint_queue and not getattr(ws, "closed", False):
                await asyncio.sleep(0.5)

            # 取消发送与接收任务
            sender_task.cancel()
            receiver_task.cancel()
            logging.info("所有数据已发送/处理，准备关闭连接。")
    finally:
        # 恢复之前的环境变量（如果有）
        if _saved_env:
            os.environ.update(_saved_env)
            logging.info("已恢复环境代理变量。")


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

    pixels, width, height = load_image_pixels(config)
    if not pixels:
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
            # 若配置中有 access_key，则优先通过接口获取真实 token，失败再回退到配置中的 token
            if ak:
                fetched = None
                try:
                    fetched = get_token(uid, ak)
                except Exception:
                    fetched = None
                if fetched:
                    token = fetched
                elif token_from_config:
                    token = token_from_config
                    logging.warning(f"获取 token 失败，回退使用配置中的 token: uid={uid}")
                else:
                    logging.warning(f"既无法通过 access_key 获取 token，也未在配置中提供 token: uid={uid}，跳过。")
            else:
                # 无 access_key，若配置含 token 则直接使用
                if token_from_config:
                    token = token_from_config
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
                from gui import start_gui as start_gui_func
                gui_available = True
            except Exception as e:
                logging.warning(f"GUI 不可用，回退到 CLI：{e}")

        if cli_only or not gui_available:
            # 保持原有 CLI 行为
            asyncio.run(handle_websocket(config, users_with_tokens, pixels, width, height, debug))
        else:
            # GUI 模式：创建线程安全的 gui_state 并在后台运行 asyncio
            gui_state = {
                'lock': threading.RLock(),
                'stop': False,
                'board_state': {},
                'overlay': False,
                'draw_mode': config.get('draw_mode', 'random'),
                'start_x': int(config.get('start_x', 0)),
                'start_y': int(config.get('start_y', 0)),
            }

            def run_asyncio_loop():
                try:
                    asyncio.run(handle_websocket(config, users_with_tokens, pixels, width, height, debug, gui_state=gui_state))
                except Exception:
                    logging.exception('后台 asyncio 任务异常')

            t = threading.Thread(target=run_asyncio_loop, daemon=True)
            t.start()

            # 启动 Tkinter GUI（主线程）
            try:
                start_gui_func(config, pixels, width, height, users_with_tokens, gui_state)
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
