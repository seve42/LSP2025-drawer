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
import ping
import seq_player
import threading
import re
from collections import OrderedDict, deque
import multiprocessing
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn

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


def process_worker(idx, cfg, users_sub, images_sub, dbg):
    """顶层函数：在子进程中启动独立的 asyncio 事件循环并运行 run_forever。

    该函数需要位于模块顶层以便 multiprocessing 在 Windows 上能够正确导入并调用。
    注意：images_sub 可能包含大量像素数据，会在进程间被序列化/复制。
    """
    import asyncio as _asyncio
    try:
        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        loop.run_until_complete(run_forever(cfg, users_sub, images_sub, dbg))
    except Exception:
        import logging as _logging
        _logging.exception(f"进程 worker #{idx} 出现未处理异常")
    finally:
        try:
            loop.close()
        except Exception:
            pass

# pending paints waiting for confirmation via board update: list of dicts {uid, paint_id, pos, color, time}
pending_paints = []
# per-user snapshot of their most recent successful painted pixels: uid -> OrderedDict[(x,y) -> (r,g,b)]
user_last_snapshot = {}
# configuration for snapshot size and pending timeout (seconds)
SNAPSHOT_SIZE = 100
PENDING_TIMEOUT = 10.0

# WebUI 日志记录器
def log_to_web_if_available(gui_state, message):
    """如果 WebUI 可用，则向其发送日志"""
    if gui_state and 'log_to_web' in gui_state:
        try:
            gui_state['log_to_web'](message)
        except Exception:
            pass

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
        # 程序自重启间隔（分钟），如果为 0 则禁用自动重启
        cfg.setdefault('auto_restart_minutes', 30)
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
        
        # 清理用户配置中可能残留的 token 字段，避免将 token persisted 在 config.json
        try:
            users = cfg.get('users', [])
            cleaned = False
            for u in users:
                if 'token' in u or 'token_time' in u:
                    u.pop('token', None)
                    u.pop('token_time', None)
                    cleaned = True
            if cleaned:
                # 保存清理后的配置
                try:
                    save_config(cfg)
                    logging.info("已从 config.json 中移除 token 字段并保存清理后的配置。")
                except Exception:
                    logging.exception("保存清理后的 config 失败")
        except Exception:
            logging.exception('清理 token 字段时出错')

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
            # 程序自重启时间，单位分钟（默认 30）；设置为 0 可禁用自动重启
            "auto_restart_minutes": 30,
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
    # 额外尝试：确保 NO_PROXY 包含目标域名与本地回环，方便在某些代理实现下绕过代理
    try:
        no_proxy = os.environ.get('NO_PROXY') or os.environ.get('no_proxy') or ''
        add_hosts = ['paintboard.luogu.me', 'localhost', '127.0.0.1']
        for h in add_hosts:
            if h not in no_proxy:
                no_proxy = (no_proxy + ',' + h) if no_proxy else h
        os.environ['NO_PROXY'] = no_proxy
        os.environ['no_proxy'] = no_proxy
        logging.debug(f"已设置 NO_PROXY 为: {no_proxy}")
    except Exception:
        pass

    # 诊断信息：在 Windows 下，Clash 等代理可能通过 WinHTTP 设置拦截请求，记录其状态以便排查
    try:
        import subprocess
        # 指定编码并在解码错误时使用替换策略，避免在某些 Windows 环境下
        # 因系统默认编码(GBK)无法解码某些字节导致 subprocess 内部 reader 线程抛出
        # UnicodeDecodeError 而中断。encoding='utf-8' 更能兼容常见输出，errors='replace'
        # 会用替换字符替代无法解码的字节。
        try:
            proc = subprocess.run(
                ['netsh', 'winhttp', 'show', 'proxy'],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace'
            )
        except TypeError:
            # 兼容性回退：某些 Python 版本或环境可能不支持 encoding/errors 参数
            proc = subprocess.run(['netsh', 'winhttp', 'show', 'proxy'], capture_output=True, text=True)

        if proc.returncode == 0:
            winhttp_out = (proc.stdout or '').strip()
            if winhttp_out:
                logging.debug(f"WinHTTP 代理状态:\n{winhttp_out}")
        else:
            logging.debug('无法获取 WinHTTP 代理状态')
    except Exception:
        # 在非 Windows 平台或无 netsh 可用时忽略
        pass

    # 记录本次连接开始时间，供上层决定是否重置退避
    conn_started_at = time.monotonic()
    try:
        # 限制握手与关闭超时，避免卡住；部分环境下可减轻“opening handshake 超时”的长期滞留
        async with websockets.connect(
            WS_URL,
            ping_interval=None,    # 禁用 websockets 库的自动 ping（完全依赖应用层心跳）
            ping_timeout=None,     # 禁用 websockets 库的 ping 超时检测
            open_timeout=30,       # 增加握手超时到30秒
            close_timeout=10,
            max_size=10 * 1024 * 1024,  # 10MB 消息大小限制
        ) as ws:
            logging.info("WebSocket 连接已建立")
            # 更新 GUI 状态：已连接
            try:
                if gui_state is not None:
                    with gui_state['lock']:
                        gui_state['conn_status'] = 'connected'
                        # use epoch seconds so frontend can compute wall-clock duration
                        gui_state['conn_since'] = int(time.time())
                        gui_state['conn_reason'] = ''
                        gui_state['server_offline'] = False
            except Exception:
                pass
            # 新连接建立时清空待发送队列，避免残留数据
            try:
                tool.paint_queue.clear()
                if hasattr(tool, 'total_size'):
                    tool.total_size = 0
            except Exception:
                pass

            # 启动定时发送任务（粘包）
            # 使用事件驱动：当有新的 paint 被 append 到队列时，调度器会 set 该事件以唤醒发送任务，
            # 避免频繁轮询 sleep，从而减少不必要的等待（同时 send_paint_data 内仍会遵守 interval_ms 最小间隔以满足速率限制）。
            paint_queue_event = asyncio.Event()
            sender_task = asyncio.create_task(tool.send_paint_data(ws, paint_interval_ms, paint_queue_event))

            # 根据配置启动额外的写连接以提高发送吞吐量（最多 16 线程/连接）
            writeonly_connections = 1
            try:
                writeonly_connections = int(config.get('writeonly_connections', 1)) if isinstance(config, dict) else 1
            except Exception:
                writeonly_connections = 1
            # 限制最大值以避免滥用资源
            writeonly_connections = max(1, min(writeonly_connections, 16))

            extra_writers = max(0, writeonly_connections - 1)
            write_worker_tasks = []

            async def write_only_worker(idx: int):
                """单独的写连接任务：建立独立 WebSocket 连接并运行 send_paint_data 循环。

                设计目标：分摊发送压力到多个连接，提高并发发送能力。遇到异常时记录并退出（上层会重连）。
                """
                try:
                    async with websockets.connect(
                        WS_URL,
                        ping_interval=None,
                        ping_timeout=None,
                        open_timeout=30,
                        close_timeout=10,
                        max_size=10 * 1024 * 1024,
                    ) as write_ws:
                        logging.info(f"写连接 #{idx} 已建立")
                        await tool.send_paint_data(write_ws, paint_interval_ms, paint_queue_event)
                except Exception as e:
                    logging.warning(f"写连接 #{idx} 异常退出: {e}")

            for i in range(extra_writers):
                try:
                    t = asyncio.create_task(write_only_worker(i))
                    write_worker_tasks.append(t)
                except Exception:
                    logging.exception(f"启动写连接任务 #{i} 失败")

            # 维护当前画板已知颜色状态（初始化为服务端快照，以便重启后立即知道已达成的像素）
            board_state = {}
            try:
                # fetch_board_snapshot 使用 requests，会阻塞线程；改为放到线程池中执行，避免阻塞 asyncio 事件循环
                loop = asyncio.get_running_loop()
                snapshot = await loop.run_in_executor(None, tool.fetch_board_snapshot)
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
            # in-flight 集合：记录已分配但尚未被画板更新确认的位置，避免重复分配
            inflight = set()

            # 启动独立的 Ping 处理进程（通过两个 multiprocessing.Queue 与之通信）
            try:
                ping_in_q = multiprocessing.Queue()
                ping_out_q = multiprocessing.Queue()
                ping_proc = ping.PingProcess(ping_in_q, ping_out_q, timeout=30.0, max_consec_fail=10)
                ping_proc.start()
                logging.info(f"已启动独立 Ping 进程 (pid={getattr(ping_proc, 'pid', None)})")
            except Exception as e:
                logging.exception(f"启动 Ping 进程失败: {e}")
                ping_in_q = None
                ping_out_q = None
                ping_proc = None

            # 为兼容后续清理逻辑，创建一个已完成的占位任务（无需实际监控任务）
            heartbeat_monitor_task = asyncio.create_task(asyncio.sleep(0))
            
            # 启动接收任务：处理 Ping(0xfc)、绘画结果(0xff)、画板更新(0xfa) 等
            async def receiver():
                """接收并处理服务器消息，使用独立的 ping.py 模块处理心跳
                
                改进：
                - 使用专门的 HeartbeatManager 处理心跳
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
                                    # 将 ping 事件转发到独立的 Ping 进程（若可用）
                                    resp = None
                                    if 'ping_in_q' in locals() and ping_in_q is not None and ping_out_q is not None:
                                        try:
                                            ping_in_q.put({'type': 'ping', 'ts': time.monotonic()})
                                        except Exception as e:
                                            logging.warning(f"向 ping 进程发送 ping 事件失败: {e}")
                                            # 标记不可用，回退到直接发送
                                            ping_in_q = None

                                        # 尝试短暂等待来自 ping 进程的指令（非阻塞主事件循环）
                                        try:
                                            loop = asyncio.get_running_loop()
                                            resp = await loop.run_in_executor(None, ping_out_q.get, True, 0.05)
                                        except Exception:
                                            resp = None

                                    # 处理 ping 进程的响应
                                    if isinstance(resp, dict) and resp.get('cmd') == 'send_pong':
                                            try:
                                                # 记录收到 ping 的时间并在发送后记录响应时延，便于诊断
                                                t_recv = time.monotonic()
                                                await ws.send(bytes([0xfb]))
                                                t_sent = time.monotonic()
                                                logging.debug(f"心跳: 收到 ping -> 发送 pong, 延迟: {(t_sent - t_recv)*1000:.2f}ms")
                                                consecutive_errors = 0
                                            except Exception as e:
                                                err_msg = str(e) if str(e) else e.__class__.__name__
                                                logging.warning(f"发送 Pong 时出错: {err_msg}")
                                                consecutive_errors += 1
                                                if consecutive_errors >= max_consecutive_errors:
                                                    logging.error("心跳处理连续失败，接收任务退出。")
                                                    if 'ping_proc' in locals() and ping_proc is not None:
                                                        logging.info(f"Ping 进程 pid={getattr(ping_proc,'pid',None)}")
                                                    return
                                    elif isinstance(resp, dict) and resp.get('cmd') == 'restart':
                                        logging.warning("Ping 进程请求重启主进程")
                                        try:
                                            tool.restart_script()
                                        except Exception:
                                            pass
                                    else:
                                        # 回退：立即在当前连接上发送 Pong（保证实时性）
                                        try:
                                            t_recv = time.monotonic()
                                            await ws.send(bytes([0xfb]))
                                            t_sent = time.monotonic()
                                            logging.debug(f"心跳(fallback): 收到 ping -> 发送 pong, 延迟: {(t_sent - t_recv)*1000:.2f}ms")
                                            consecutive_errors = 0
                                        except Exception as e:
                                            err_msg = str(e) if str(e) else e.__class__.__name__
                                            logging.warning(f"发送 Pong (fallback) 时出错: {err_msg}")
                                            consecutive_errors += 1
                                            if consecutive_errors >= max_consecutive_errors:
                                                logging.error("心跳处理连续失败，接收任务退出。")
                                                if 'ping_proc' in locals() and ping_proc is not None:
                                                    logging.info(f"Ping 进程 pid={getattr(ping_proc,'pid',None)}")
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
                                                log_to_web_if_available(gui_state, f"上一轮成功绘画的坐标: ({x}, {y}) by UID {uid_matched}")
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
                                            # 若该位置在 in-flight，则移除（已确认或被覆盖）
                                            try:
                                                inflight.discard((x, y))
                                            except Exception:
                                                pass
                                        # 同步到 GUI（实时画板预览）
                                        if gui_state is not None:
                                            try:
                                                with gui_state['lock']:
                                                    # 仅同步画板像素到 GUI，热力图功能已移除，故不再记录 pixel_hits
                                                    gui_state['board_state'][(x, y)] = (r, g, b)
                                            except Exception:
                                                pass
                                        # 清理过期的 pending_paints，并同步 inflight 集合以允许重试
                                        try:
                                            now = time.monotonic()
                                            pending_paints[:] = [p for p in pending_paints if now - p['time'] <= PENDING_TIMEOUT]
                                            try:
                                                # 将 inflight 与仍在 pending_paints 中的位置做交集，
                                                # 这样超时未确认的 pos 会从 inflight 中移除并在后续调度中被重试。
                                                pending_positions = set(p['pos'] for p in pending_paints)
                                                inflight.intersection_update(pending_positions)
                                            except Exception:
                                                # 如果同步失败也不要阻塞主流程
                                                pass
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
                    if 'ping_proc' in locals() and ping_proc is not None:
                        logging.info(f"Ping 进程 pid={getattr(ping_proc,'pid',None)} 状态: {'alive' if ping_proc.is_alive() else 'stopped'}")
                    else:
                        logging.info("Ping 进程信息不可用（使用内置心跳或未创建）")
                    
                except (websockets.exceptions.ConnectionClosed,
                        websockets.exceptions.ConnectionClosedError,
                        websockets.exceptions.ConnectionClosedOK) as e:
                    err_msg = str(e) if str(e) else e.__class__.__name__
                    logging.info(f"WebSocket 连接已关闭: {err_msg} (接收了 {message_count} 条消息)")
                    if 'ping_proc' in locals() and ping_proc is not None:
                        logging.info(f"Ping 进程 pid={getattr(ping_proc,'pid',None)} 状态: {'alive' if ping_proc.is_alive() else 'stopped'}")
                    else:
                        logging.info("Ping 进程信息不可用（使用内置心跳或未创建）")
                except asyncio.CancelledError:
                    # 任务被取消时正常退出
                    logging.debug(f"接收任务被取消 (已接收 {message_count} 条消息)")
                    if 'ping_proc' in locals() and ping_proc is not None:
                        logging.debug(f"Ping 进程 pid={getattr(ping_proc,'pid',None)} 状态: {'alive' if ping_proc.is_alive() else 'stopped'}")
                    else:
                        logging.debug("Ping 进程信息不可用（使用内置心跳或未创建）")
                    raise
                except Exception as e:
                    err_msg = str(e) if str(e) else e.__class__.__name__
                    err_type = e.__class__.__name__
                    logging.exception(f"WebSocket 接收处理时发生未预期异常 ({err_type}): {err_msg}")
                    if 'ping_proc' in locals() and ping_proc is not None:
                        logging.info(f"Ping 进程 pid={getattr(ping_proc,'pid',None)} 状态: {'alive' if ping_proc.is_alive() else 'stopped'}")
                    else:
                        logging.info("Ping 进程信息不可用（使用内置心跳或未创建）")
                finally:
                    logging.info("接收任务已退出。")

            receiver_task = asyncio.create_task(receiver())

            # 启动进度显示器（每秒刷新）
            async def progress_printer():
                # 使用 rich 渲染更美观的进度条
                mode_prefix = f"[{len(images_data)}图] "
                history = deque()
                window_seconds = 60.0

                console = Console()
                progress = Progress(
                    SpinnerColumn(),
                    TextColumn("{task.description}"),
                    BarColumn(bar_width=40),
                    TextColumn("{task.percentage:>3.0f}%"),
                    TimeRemainingColumn(),
                    console=console,
                    transient=False,
                )
                task_id = progress.add_task(f"{mode_prefix}进度", total=100)
                progress.start()
                try:
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

                        # 计算抵抗率
                        resistance_pct = None
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
                                user_covered = covered_flag
                                if covered_flag:
                                    covered += 1
                            if total_users > 0:
                                resistance_pct = (covered / total_users) * 100.0
                        except Exception:
                            resistance_pct = None

                        # 更新 GUI 状态
                        if gui_state is not None:
                            with gui_state['lock']:
                                gui_state['total'] = total
                                gui_state['mismatched'] = mismatched
                                gui_state['available'] = len(users_with_tokens)
                                gui_state['ready_count'] = ready_count
                                gui_state['resistance_pct'] = resistance_pct

                        # maintain history and compute average growth
                        try:
                            history.append((now, pct))
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

                        danger = False
                        try:
                            if growth is not None and growth < 0 and pct < 95.0:
                                danger = True
                        except Exception:
                            danger = False

                        try:
                            if resistance_pct is None:
                                res_part = '抵抗率: --'
                            else:
                                res_part = f'抵抗率: {resistance_pct:5.1f}%'
                        except Exception:
                            res_part = ''

                        # 构造描述并更新 progress
                        danger_mark = ' ⚠️' if danger else ''
                        desc = f"{mode_prefix}可用:{available} 就绪:{ready_count} 未达:{mismatched} | {res_part}{growth_str}{eta_str}{danger_mark}"
                        try:
                            progress.update(task_id, completed=pct, description=desc)
                        except Exception:
                            # 回退为直接输出
                            out_line = f"{mode_prefix}进度: [{int(pct):3d}%] 可用:{available} (就绪:{ready_count}) 未达:{mismatched} {res_part}{growth_str}{eta_str}"
                            if debug:
                                logging.info(out_line)
                            else:
                                sys.stdout.write('\r' + out_line)
                                sys.stdout.flush()

                        # 若 GUI 请求停止则退出循环
                        if gui_state is not None and gui_state.get('stop'):
                            logging.info('收到 GUI 退出信号，结束调度循环。')
                            return
                        await asyncio.sleep(1)
                finally:
                    try:
                        progress.stop()
                    except Exception:
                        pass

            # 调度：支持冷却与持续监视
            user_counters = {u['uid']: 0 for u in users_with_tokens}
            cooldown_until = {u['uid']: 0.0 for u in users_with_tokens}  # monotonic 时间戳
            in_watch_mode = False
            round_idx = 0
            # 【性能优化】使用 deque 作为剩余任务队列以获得 O(1) 的 popleft
            from collections import deque as _deque
            remaining = _deque()
            last_remaining_update = 0.0
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
            connection_warnings = 0  # 连续警告次数
            max_connection_warnings = 3  # 允许3次连续警告再退出
            
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
                                    connection_issue = True
                                else:
                                    logging.debug("发送任务已退出（无异常）")
                            except Exception as e:
                                logging.debug(f"发送任务已退出: {e}")
                    except Exception as e:
                        logging.debug(f"检查发送任务状态时出错: {e}")
                    
                    # 检查接收任务状态
                    try:
                        if receiver_task.done():
                            try:
                                exc = receiver_task.exception() if not receiver_task.cancelled() else None
                                if exc:
                                    logging.error(f"接收任务异常退出: {exc}")
                                    connection_issue = True
                                else:
                                    logging.debug("接收任务已退出（无异常）")
                            except Exception as e:
                                logging.debug(f"接收任务已退出: {e}")
                    except Exception as e:
                        logging.debug(f"检查接收任务状态时出错: {e}")
                    
                    # 检查连接状态
                    try:
                        is_open = getattr(ws, "open", None)
                        if is_open is None:
                            is_open = not getattr(ws, "closed", False)
                        if not is_open:
                            connection_warnings += 1
                            if connection_warnings >= max_connection_warnings:
                                logging.warning(f"检测到 WebSocket 已关闭（连续{connection_warnings}次）")
                                connection_issue = True
                            else:
                                logging.debug(f"连接暂时断开（警告 {connection_warnings}/{max_connection_warnings}）")
                        else:
                            # 连接恢复，重置警告计数
                            if connection_warnings > 0:
                                logging.info(f"连接已恢复，重置警告计数（之前: {connection_warnings}）")
                                connection_warnings = 0
                    except Exception as e:
                        logging.debug(f"检查连接状态时出错: {e}")
                    
                    # 如果检测到严重连接问题，立即清理并退出
                    if connection_issue:
                        logging.warning("检测到严重连接异常，退出当前循环以便重连。")
                        try:
                            # 只取消未完成的任务，避免对已完成任务调用 cancel()
                            tasks_to_cancel = []
                            if not progress_task.done():
                                progress_task.cancel()
                                tasks_to_cancel.append(progress_task)
                            if not sender_task.done():
                                sender_task.cancel()
                                tasks_to_cancel.append(sender_task)
                            if not receiver_task.done():
                                receiver_task.cancel()
                                tasks_to_cancel.append(receiver_task)
                            
                            # 等待任务取消完成
                            if tasks_to_cancel:
                                await asyncio.wait_for(
                                    asyncio.gather(*tasks_to_cancel, return_exceptions=True),
                                    timeout=2.0
                                )
                        except asyncio.TimeoutError:
                            logging.debug("等待任务取消超时")
                        except Exception as e:
                            logging.debug(f"取消任务时出错: {e}")
                        logging.info("任务已取消，即将退出 WebSocket 上下文并返回 run_forever 进行重连")
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
                        connection_warnings += 1
                        if connection_warnings >= max_connection_warnings:
                            logging.warning(f"健康检查：检测到 WebSocket 已关闭（连续{connection_warnings}次），退出以便重连。")
                            try:
                                # 只取消未完成的任务
                                tasks_to_cancel = []
                                if not progress_task.done():
                                    progress_task.cancel()
                                    tasks_to_cancel.append(progress_task)
                                if not sender_task.done():
                                    sender_task.cancel()
                                    tasks_to_cancel.append(sender_task)
                                if not receiver_task.done():
                                    receiver_task.cancel()
                                    tasks_to_cancel.append(receiver_task)
                                
                                if tasks_to_cancel:
                                    await asyncio.wait_for(
                                        asyncio.gather(*tasks_to_cancel, return_exceptions=True),
                                        timeout=2.0
                                    )
                            except:
                                pass
                            break
                        else:
                            logging.debug(f"连接暂时断开（警告 {connection_warnings}/{max_connection_warnings}）")
                    else:
                        # 连接正常，重置警告计数
                        if connection_warnings > 0:
                            logging.info(f"连接已恢复，重置警告计数（之前: {connection_warnings}）")
                            connection_warnings = 0
                    
                    # 检查任务健康状态（仅记录，不退出）
                    sender_ok = not sender_task.done()
                    receiver_ok = not receiver_task.done()
                    progress_ok = not progress_task.done()
                    
                    if not sender_ok or not receiver_ok or not progress_ok:
                        logging.debug(f"健康检查：任务状态 [发送:{sender_ok} 接收:{receiver_ok} 进度:{progress_ok}]")
                    else:
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

                # 若后台视频帧已更新，重新构建目标映射以反映新帧（注入的视频作为虚拟图片由 tool.VIDEO_STATE 管理）
                try:
                    if getattr(tool, 'VIDEO_FRAME_UPDATED', False):
                        tool.VIDEO_FRAME_UPDATED = False
                        _res2 = tool.merge_target_maps(images_data)
                        if isinstance(_res2, tuple) and len(_res2) == 3:
                            target_map, positions_by_mode, pos_to_image_idx = _res2
                        else:
                            target_map, positions_by_mode = _res2
                            pos_to_image_idx = {}
                        # rebuild merged positions list and per-image grouping
                        target_positions = []
                        for mode in ['horizontal', 'concentric', 'random']:
                            if mode in positions_by_mode:
                                target_positions.extend(positions_by_mode[mode])
                        from collections import defaultdict, deque as _deque
                        positions_by_image = defaultdict(list)
                        for pos in target_positions:
                            idx = pos_to_image_idx.get(pos)
                            if idx is not None:
                                positions_by_image[idx].append(pos)
                        positions_by_image = {k: _deque(v) for k, v in positions_by_image.items()}
                        # reset remaining queue so scheduler will consider new targets immediately
                        remaining = _deque([pos for pos in target_positions if board_state.get(pos) != target_map[pos]])
                        last_remaining_update = time.monotonic()
                        logging.debug('检测到视频帧更新，已重建目标映射以反映新帧')
                except Exception:
                    logging.exception('处理视频帧更新时出错')

                # 如果存在虚拟视频图片，检查当前视频帧是否已全部绘制完成，完成后推进到下一帧（不再使用 CRT 隔行）
                try:
                    # 遍历 images_data 查找 type=='video' 的虚拟图片
                    for img in images_data:
                        if str(img.get('type', '')).lower() != 'video':
                            continue
                        folder = img.get('folder') or img.get('image_path')
                        if not folder:
                            continue
                        vs = tool.VIDEO_STATE.get(folder)
                        if not vs:
                            continue
                        cfg_idx = img.get('config_index')
                        if cfg_idx is None:
                            continue
                        # 收集属于此视频虚拟图片的所有目标坐标
                        video_positions = [pos for pos, idx in pos_to_image_idx.items() if idx == cfg_idx]
                        if not video_positions:
                            continue
                        # 计算已达成的像素比例，达到配置的 complete_rate 即可推进
                        try:
                            matched = 0
                            for p in video_positions:
                                if board_state.get(p) == target_map.get(p):
                                    matched += 1
                            total = len(video_positions)
                            ratio = (matched / total) if total > 0 else 0.0
                            threshold = float(vs.get('complete_rate', 1.0))
                        except Exception:
                            matched = 0
                            total = len(video_positions)
                            ratio = 0.0
                            threshold = float(vs.get('complete_rate', 1.0))

                        logging.debug(f"视频完成率: {matched}/{total} = {ratio:.2%}, 阈值 {threshold:.2%} (folder={folder})")

                        if ratio >= threshold:
                            # 若配置为非循环且已到最后一帧，则不再推进
                            if not bool(vs.get('loop', True)) and int(vs.get('frame', 1)) >= int(vs.get('frame_count', 1)):
                                logging.info(f"视频已完成最后一帧: folder={folder} frame={vs.get('frame')}")
                                continue
                            # 推进到下一帧（1-based）
                            cur = int(vs.get('frame', 1))
                            cur = (cur % int(vs.get('frame_count', 1))) + 1
                            vs['frame'] = cur
                            tool.VIDEO_FRAME_UPDATED = True
                            logging.info(f"视频完成率达到阈值，推进到下一帧: folder={folder} frame={cur} (达成 {ratio:.2%})")
                            # 发现并推进一个视频后可继续检查下一个（若有多个视频）
                except Exception:
                    logging.exception('视频完成检测时出错')

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
                # 使用 deque 保存 remaining（未达到目标的绝对坐标），并在周期性更新时重建队列。
                # 同时跳过已经在 inflight 中的坐标以避免重复分配。
                if last_remaining_update == 0.0:
                    remaining = _deque([pos for pos in target_positions if board_state.get(pos) != target_map[pos]])
                    last_remaining_update = now
                else:
                    # 每10轮或每秒更新一次 remaining，其他时候使用缓存队列
                    if round_idx % 10 == 0 or (now - last_remaining_update) > 1.0:
                        remaining = _deque([pos for pos in target_positions if board_state.get(pos) != target_map[pos] and pos not in inflight])
                        last_remaining_update = now
                    else:
                        # 否则保留现有 remaining 队列（会在 popleft 时跳过已满足的位置）
                        pass

                # 可用用户（不在冷却期）
                available_users = [u for u in users_with_tokens if cooldown_until.get(u['uid'], 0.0) <= now]

                assigned = 0
                if remaining and available_users:
                    # 【性能优化】使用更高效的派发策略，避免每轮都重建复杂的字典结构
                    # 简化版本：直接从 remaining 列表中按轮转策略取出像素分配给用户
                    
                    for user in available_users:
                        if not remaining:
                            break
                        # 从队列头部取出一个尚未满足且不在 inflight 的位置（跳过已被外部更新的或正在处理的）
                        pick = None
                        while remaining:
                            cand = remaining.popleft()
                            # 如果此位置已达到目标颜色，跳过
                            if board_state.get(cand) == target_map.get(cand):
                                continue
                            # 如果已在 inflight，则跳过以避免重复分配
                            if cand in inflight:
                                continue
                            pick = cand
                            break
                        if pick is None:
                            break
                        x, y = pick
                        r, g, b = target_map[(x, y)]
                        uid = user['uid']
                        token_bytes = user.get('token_bytes')
                        uid_bytes3 = user.get('uid_bytes3')
                        paint_id = user_counters[uid]
                        # 标记为 in-flight，避免重复分配
                        inflight.add(pick)
                        # 调用同步的 paint 函数以避免每次分配产生 await/上下文切换开销
                        tool.paint(ws, uid, token_bytes, uid_bytes3, r, g, b, x, y, paint_id)
                        try:
                            # 唤醒发送任务，尽快把新加入的绘画数据写出（事件驱动替代频繁 sleep）
                            paint_queue_event.set()
                        except Exception:
                            pass
                        # 记录 pending paint，待画板更新广播到来时归属成功绘制
                        try:
                            image_idx = pos_to_image_idx.get(pick)
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
                        except Exception as e:
                            log_to_web_if_available(gui_state, f"记录pending paint时出错: {e}")

                        # 【性能优化】在高速模式下减少日志输出，仅在 DEBUG 级别或每100次输出一次
                        if debug or (paint_id % 100 == 0):
                            logging.info(f"UID {uid} 播报绘制像素: ({x},{y}) color=({r},{g},{b}) id={paint_id}")
                        else:
                            logging.debug(f"UID {uid} 绘制: ({x},{y}) id={paint_id}")
                        user_counters[uid] = paint_id + 1
                        cooldown_until[uid] = now + user_cooldown_seconds
                        assigned += 1

                    # 输出轮次/进度日志
                    round_idx += 1
                    # 【性能优化】避免每轮都重新计算 left，使用估算值或周期性更新
                    if round_idx % 10 == 0:
                        left = len([pos for pos in target_positions if board_state.get(pos) != target_map[pos]])
                        logging.info(f"第 {round_idx} 次调度：分配 {assigned} 个修复任务，剩余未达标 {left}。")
                    else:
                        logging.debug(f"第 {round_idx} 次调度：分配 {assigned} 个修复任务。")

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
                    # 之前这里强制最小等待 0.5s 会导致在 cooldown 非常低时（例如 0.05s）出现长时间空转，
                    # 将最小等待缩短到一个很小的值以提高分配频率（例如 5ms）。
                    timeout = max(0.005, min(round_interval_seconds, next_ready_in))
                    try:
                        await asyncio.wait_for(state_changed_event.wait(), timeout=timeout)
                    except asyncio.TimeoutError:
                        pass
                    finally:
                        state_changed_event.clear()
                    continue

                # 有分配则短暂让出控制权（粘包发送会在后台周期触发）
                # 为避免每轮都阻塞 100ms（过长），改为短暂让出（例如 5ms 或根据 round_interval_seconds 缩放）
                # 尽量缩小短等待，使用 asyncio.sleep(0) 作最小的让步以降低空转开销
                sleep_dur = min(0.001, round_interval_seconds)
                if sleep_dur <= 0:
                    await asyncio.sleep(0)
                else:
                    await asyncio.sleep(sleep_dur)

            # 调度循环因检测到连接问题而退出，直接返回以便 run_forever 重连
            logging.info("调度循环退出，准备重连...")

            # 确保所有后台任务都被取消（如果还没被取消）
            tasks_to_cancel = []
            if not progress_task.done():
                progress_task.cancel()
                tasks_to_cancel.append(progress_task)
            if not sender_task.done():
                sender_task.cancel()
                tasks_to_cancel.append(sender_task)
            if not receiver_task.done():
                receiver_task.cancel()
                tasks_to_cancel.append(receiver_task)
            if not heartbeat_monitor_task.done():
                heartbeat_monitor_task.cancel()
                tasks_to_cancel.append(heartbeat_monitor_task)

            # 等待任务完全退出（最多等待1秒）
            if tasks_to_cancel:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks_to_cancel, return_exceptions=True),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    logging.debug("等待后台任务退出超时")
                except Exception:
                    pass

            # 关闭并清理 Ping 进程（如存在）
            try:
                if 'ping_in_q' in locals() and ping_in_q is not None:
                    try:
                        ping_in_q.put({'cmd': 'shutdown'})
                    except Exception:
                        pass
                if 'ping_proc' in locals() and ping_proc is not None:
                    try:
                        ping_proc.join(timeout=1.0)
                    except Exception:
                        pass
            except Exception:
                logging.debug('清理 Ping 进程时出错')

            # 不等待队列清空，直接返回让 run_forever 重连
            # 队列中的数据会在下次连接时重新发送
            logging.info("后台任务已清理，准备关闭连接。")
            # 取消并等待所有写连接任务退出
            try:
                for t in write_worker_tasks:
                    try:
                        t.cancel()
                    except Exception:
                        pass
                if write_worker_tasks:
                    try:
                        await asyncio.gather(*write_worker_tasks, return_exceptions=True)
                    except Exception:
                        pass
            except Exception:
                logging.debug('取消写连接任务时出错')
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
    # Token 刷新控制：默认每 3600 秒（1 小时）刷新一次，可通过 config['token_refresh_interval_seconds'] 覆盖
    token_refresh_interval = int(config.get('token_refresh_interval_seconds', 3600)) if isinstance(config, dict) else 3600
    last_token_refresh = time.monotonic()
    
    while True:
        # GUI 请求停止则退出
        if gui_state is not None and gui_state.get('stop'):
            logging.info('检测到停止标记，结束 run_forever 循环。')
            break
        # 周期性刷新用户 tokens（在后台线程执行以避免阻塞 asyncio loop）
        try:
            now = time.monotonic()
            if now - last_token_refresh >= token_refresh_interval:
                logging.info(f"达到 token 刷新间隔 ({token_refresh_interval}s)，开始重新获取用户 tokens...")

                def fetch_tokens_sync():
                    new_list = []
                    users_cfg = config.get('users', []) if isinstance(config, dict) else []
                    if not users_cfg:
                        return new_list

                    max_workers_local = min(32, max(1, len(users_cfg)))
                    with ThreadPoolExecutor(max_workers=max_workers_local) as ex:
                        future_to_user = {}
                        for u in users_cfg:
                            uid = u.get('uid')
                            ak = u.get('access_key')
                            if ak:
                                fut = ex.submit(get_token, uid, ak)
                            else:
                                token_cfg = u.get('token')
                                fut = ex.submit(lambda t=token_cfg: t)
                            future_to_user[fut] = u

                        for fut in as_completed(future_to_user):
                            u = future_to_user[fut]
                            uid = u.get('uid')
                            ak = u.get('access_key')
                            try:
                                token = fut.result()
                            except Exception:
                                token = None
                            if token:
                                new_list.append({'uid': uid, 'token': token})
                            else:
                                if ak:
                                    logging.warning(f"刷新 token 失败 uid={uid}")
                                else:
                                    logging.debug(f"无 access_key，使用配置内回退 token: uid={uid}")

                    return new_list

                try:
                    loop = asyncio.get_running_loop()
                    new_tokens = await loop.run_in_executor(None, fetch_tokens_sync)
                    if new_tokens:
                        try:
                            # 预计算 token_bytes 与 uid_bytes3 后再更新 users_with_tokens
                            validated_new = []
                            for u in new_tokens:
                                token = u.get('token')
                                uid = u.get('uid')
                                try:
                                    try:
                                        token_bytes = UUID(token).bytes
                                    except Exception:
                                        token_bytes = UUID(hex=token.replace('-', '')).bytes
                                except Exception:
                                    logging.warning(f"刷新后预计算 token_bytes 失败，跳过 uid={uid}")
                                    continue
                                try:
                                    uid_bytes3 = int(uid).to_bytes(3, 'little', signed=False)
                                except Exception:
                                    logging.warning(f"刷新后预计算 uid_bytes 失败，跳过 uid={uid}")
                                    continue
                                validated_new.append({'uid': uid, 'token': token, 'token_bytes': token_bytes, 'uid_bytes3': uid_bytes3})

                            if validated_new:
                                users_with_tokens.clear()
                                users_with_tokens.extend(validated_new)
                                logging.info(f"已刷新 {len(validated_new)} 个用户的 token。")
                            else:
                                logging.warning('刷新 token 后未能预计算到任何有效 token，保留原有 tokens。')
                        except Exception:
                            logging.exception('更新 users_with_tokens 时出错')
                    else:
                        logging.warning('刷新 token 未获取到任何 token，保留原有 tokens。')
                except Exception:
                    logging.exception('后台刷新 tokens 时出错')
                finally:
                    last_token_refresh = time.monotonic()
        except Exception:
            logging.exception('检查 token 刷新条件时发生错误')
        reconnect_count += 1
        connection_start = time.monotonic()
        exit_reason = "未知原因"
        
        try:
            # 标记 GUI 状态为正在连接
            try:
                if gui_state is not None:
                    with gui_state['lock']:
                        gui_state['conn_status'] = 'connecting'
                        gui_state['conn_reason'] = ''
                        gui_state['server_offline'] = False
            except Exception:
                pass
            if reconnect_count == 1:
                logging.info(f"初始连接 WebSocket...")
            else:
                logging.info(f"尝试重新连接 WebSocket (第 {reconnect_count} 次尝试，累计成功连接: {successful_connections} 次，总连接时长: {total_connected_time:.1f}s)...")
            
            duration = await handle_websocket(config, users_with_tokens, images_data, debug, gui_state=gui_state)
            # handle_websocket 返回表示连接持续时间，连接结束后向 GUI 报告为断开（并给出原因为空或剩余信息）
            try:
                if gui_state is not None:
                    with gui_state['lock']:
                        gui_state['conn_status'] = 'disconnected'
                        gui_state['conn_reason'] = '连接已关闭'
                        gui_state['server_offline'] = False
            except Exception:
                pass
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
            
            logging.warning(f'主循环结束（运行时长: {last_successful_duration:.1f}s，原因: {exit_reason}），将在 {backoff:.1f}s 后尝试重连。')
            
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
                log_to_web_if_available(gui_state, f"错误: {exit_reason}")
                # 状态码错误可能是服务器限制，使用较大退避
                backoff = min(backoff * 2.5, backoff_max)
                # 标记 GUI：服务器可能不可用
                try:
                    if gui_state is not None:
                        with gui_state['lock']:
                            gui_state['conn_status'] = 'disconnected'
                            gui_state['conn_reason'] = exit_reason
                            gui_state['server_offline'] = True
                except Exception:
                    pass
            elif isinstance(e, asyncio.TimeoutError):
                exit_reason = "连接超时"
                logging.warning(f'{exit_reason} (第 {reconnect_count} 次尝试，已连接 {connection_duration:.1f}s)')
                log_to_web_if_available(gui_state, f"错误: {exit_reason}")
                # 超时可能是网络问题，适度退避
                backoff = min(backoff * 1.8, backoff_max)
                try:
                    if gui_state is not None:
                        with gui_state['lock']:
                            gui_state['conn_status'] = 'disconnected'
                            gui_state['conn_reason'] = exit_reason
                            gui_state['server_offline'] = True
                except Exception:
                    pass
            elif isinstance(e, OSError):
                exit_reason = f"操作系统网络错误: {err_text}"
                logging.warning(f'{exit_reason} (第 {reconnect_count} 次尝试，已连接 {connection_duration:.1f}s)')
                log_to_web_if_available(gui_state, f"错误: {exit_reason}")
                # OS错误通常是严重网络问题，使用较大退避
                backoff = min(backoff * 2.0, backoff_max)
                try:
                    if gui_state is not None:
                        with gui_state['lock']:
                            gui_state['conn_status'] = 'disconnected'
                            gui_state['conn_reason'] = exit_reason
                            gui_state['server_offline'] = True
                except Exception:
                    pass
            else:
                exit_reason = f"连接异常关闭: {err_text}"
                logging.warning(f'{exit_reason} (第 {reconnect_count} 次尝试，已连接 {connection_duration:.1f}s)')
                log_to_web_if_available(gui_state, f"警告: {exit_reason}")
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
            log_to_web_if_available(gui_state, f"严重错误: {exit_reason}")
            # 未预期异常，使用较大的退避
            backoff = min(backoff * 2.5, backoff_max)
            try:
                if gui_state is not None:
                    with gui_state['lock']:
                        gui_state['conn_status'] = 'disconnected'
                        gui_state['conn_reason'] = exit_reason
                        gui_state['server_offline'] = False
            except Exception:
                pass

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


def main_wrapper():
    """包装 main 函数，以便在重启时重新执行"""
    # 解析命令行参数（支持 -debug、-cli 和端口设置）
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('-debug', action='store_true', help='启用详细日志（DEBUG）并显示完整日志）')
    parser.add_argument('-cli', action='store_true', help='仅命令行模式，禁用 WebUI')
    parser.add_argument('-port', type=int, default=80, help='WebUI 端口（默认 80）')
    args, _ = parser.parse_known_args()
    
    # 启动主逻辑
    main(args)

def main(args):
    """主函数"""
    debug = bool(args.debug)
    cli_only = bool(args.cli)

    config = load_config()
    if not config:
        return

    # 加载所有图片
    images_data = tool.load_all_images(config)
    
 ##   if not images_data:
 ##       logging.error("没有可用的图片配置，程序退出。")
 ##       return
    ##
    # 获取 token 阶段：在 GUI 模式下显示进度条避免无响应感
    def get_tokens_with_progress(users_list, allow_gui=True):
        results = []
        total = len(users_list)
        # 使用 rich 渲染 CLI 进度条
        root = None
        # 并发获取 token：对带 access_key 的用户并行调用 get_token，
        # 对不带 access_key 的用户直接取配置中 token（回退）
        if total == 0:
            return []

        max_workers = min(32, max(1, total))
        idx = 0
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            future_to_user = {}
            for user in users_list:
                uid = user.get('uid')
                ak = user.get('access_key')
                if ak:
                    fut = ex.submit(get_token, uid, ak)
                else:
                    # 直接返回配置中的 token（可能为 None）
                    token_from_config = user.get('token')
                    fut = ex.submit(lambda t=token_from_config: t)
                future_to_user[fut] = user

            # 使用 rich Progress 显示更友好的进度条
            from rich.progress import Progress as _Progress, SpinnerColumn as _SpinnerColumn, BarColumn as _BarColumn, TextColumn as _TextColumn
            with _Progress(_SpinnerColumn(), _TextColumn("获取 token: {task.completed}/{task.total}"), _BarColumn(), _TextColumn("{task.percentage:>3.0f}%")) as p:
                task = p.add_task("fetch", total=total)
                for fut in as_completed(future_to_user):
                    user = future_to_user[fut]
                    uid = user.get('uid')
                    ak = user.get('access_key')
                    token = None
                    try:
                        token = fut.result()
                    except Exception:
                        token = None
                    if token:
                        results.append({'uid': uid, 'token': token})
                    else:
                        if ak:
                            logging.warning(f"无法通过 access_key 获取 token: uid={uid}，该用户将被跳过。")
                        else:
                            logging.warning(f"用户条目缺少 access_key 且未提供 token: uid={uid}，跳过。")
                    p.advance(task)

        # 换行完成进度输出
        print('')
        return results

    users_with_tokens = get_tokens_with_progress(config.get('users', []), allow_gui=True)

    # 预计算 token_bytes 与 uid_bytes3，移除无效 token 的用户，减少后续开销
    validated = []
    for u in users_with_tokens:
        token = u.get('token')
        uid = u.get('uid')
        try:
            try:
                token_bytes = UUID(token).bytes
            except Exception:
                token_bytes = UUID(hex=token.replace('-', '')).bytes
        except Exception:
            logging.warning(f"预计算 token_bytes 失败，跳过 uid={uid}")
            continue
        try:
            uid_bytes3 = int(uid).to_bytes(3, 'little', signed=False)
        except Exception:
            logging.warning(f"预计算 uid_bytes 失败，跳过 uid={uid}")
            continue
        validated.append({'uid': uid, 'token': token, 'token_bytes': token_bytes, 'uid_bytes3': uid_bytes3})
    users_with_tokens = validated

    # --- 将 video 注入 images_data 作为高权重虚拟图片（动态帧由 tool.VIDEO_STATE 管理） ---
    try:
        video_cfg = config.get('video') if isinstance(config, dict) else None
        if isinstance(video_cfg, dict) and video_cfg.get('enabled', False):
            folder = video_cfg.get('folder') or video_cfg.get('folder_path') or video_cfg.get('path')
            if folder:
                # 收集帧文件（按文件名中第一个数字排序）
                try:
                    files = [fn for fn in os.listdir(folder) if fn.lower().endswith(('.png', '.jpg', '.jpeg'))]
                except Exception:
                    files = []
                def _numkey(n):
                    m = re.search(r"(\d+)", n)
                    return int(m.group(1)) if m else float('inf')
                files.sort(key=_numkey)
                full_paths = [os.path.join(folder, f) for f in files]
                if full_paths:
                    fps_val = float(video_cfg.get('fps', 12.0))
                    # 注册 VIDEO_STATE，使用 folder 作为 key
                    try:
                        complete_rate = float(video_cfg.get('complete_rate', video_cfg.get('completion_rate', 1.0)))
                    except Exception:
                        complete_rate = 1.0
                    # clamp to [0,1]
                    complete_rate = max(0.0, min(1.0, complete_rate))
                    tool.VIDEO_STATE[folder] = {
                        'folder': folder,
                        'files': full_paths,
                        'frame': 1,
                        'frame_count': len(full_paths),
                        'fps': fps_val,
                        'loop': bool(video_cfg.get('loop', True)),
                        'complete_rate': complete_rate
                    }
                    tool.VIDEO_FRAME_UPDATED = True

                    # 插入为虚拟图片，权重设为很大以确保优先
                    try:
                        max_cfg_idx = max((img.get('config_index', i) for i, img in enumerate(images_data)), default=-1)
                    except Exception:
                        max_cfg_idx = len(images_data)
                    vid_cfg_idx = int(max_cfg_idx) + 1
                    # 强制使用随机绘制顺序以均匀分散派发，避免局部队列卡住导致帧推进延迟
                    images_data.append({
                        'type': 'video',
                        'folder': folder,
                        'start_x': int(video_cfg.get('start_x', video_cfg.get('startx', 0))),
                        'start_y': int(video_cfg.get('start_y', video_cfg.get('starty', 0))),
                        'draw_mode': 'random',
                        'weight': float(video_cfg.get('weight', 1000000.0)),
                        'enabled': True,
                        'config_index': vid_cfg_idx
                    })
                    logging.info(f"已把视频注入为虚拟图片: folder={folder} frames={len(full_paths)} fps={fps_val}")

                    # 视频的帧推进现在由主调度逻辑控制（仅在上一帧全部画完后推进），因此不再启动独立的更新时间线程。
                else:
                    logging.warning(f"视频目录中没有找到帧文件: {folder}")
            else:
                logging.warning('video 配置缺少 folder 字段')
    except Exception:
        logging.exception("初始化视频注入时出错")

    # 移除 WebUI 后：如果没有可用 token 直接退出，避免尝试启动前端
    if not users_with_tokens:
        logging.error("没有可用的用户 Token，程序退出。")
        return

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
        # 已移除 WebUI 后：直接以稳定的后台/CLI 模式运行主循环
        # 启动自动重启线程（如果配置中启用）
        restart_stop_event = threading.Event()
        def _auto_restart_worker(minutes, stop_evt):
            try:
                m = float(minutes)
            except Exception:
                return
            if m <= 0:
                return
            secs = m * 60.0
            # 分段睡眠以便能及时响应 stop_evt
            slept = 0.0
            interval = 1.0
            try:
                while slept < secs and not stop_evt.is_set():
                    to_sleep = min(interval, secs - slept)
                    time.sleep(to_sleep)
                    slept += to_sleep
            except Exception:
                return
            if stop_evt.is_set():
                return
            try:
                logging.info(f"自动重启倒计时已到 ({m} 分钟)，触发重启...")
            except Exception:
                pass
            try:
                tool.restart_script()
            except Exception:
                logging.exception("触发自动重启时出错")

        auto_minutes = 0
        try:
            auto_minutes = int(config.get('auto_restart_minutes', 0)) if isinstance(config, dict) else 0
        except Exception:
            try:
                auto_minutes = int(float(config.get('auto_restart_minutes')))
            except Exception:
                auto_minutes = 0

        if auto_minutes and auto_minutes > 0:
            t = threading.Thread(target=_auto_restart_worker, args=(auto_minutes, restart_stop_event), daemon=True, name='AutoRestartThread')
            t.start()

        # 支持多线程/多进程 worker：按配置将 users_with_tokens 划分到多个 worker 中，
        # 每个 worker 维护独立的 asyncio 事件循环并运行完整的 run_forever，以提高并发发送吞吐量。
        thread_workers = 1
        process_workers = 0
        try:
            thread_workers = int(config.get('thread_workers', 1)) if isinstance(config, dict) else 1
        except Exception:
            thread_workers = 1
        try:
            process_workers = int(config.get('process_workers', 0)) if isinstance(config, dict) else 0
        except Exception:
            process_workers = 0

        thread_workers = max(1, min(32, thread_workers))
        process_workers = max(0, min(16, process_workers))

        # 优先使用进程模式（可真正利用多核），否则使用线程模式
        if process_workers > 0:
            # 将 users_with_tokens 轮询分配到 N 个分片，保证尽量均衡
            def partition_round_robin(items, parts):
                groups = [[] for _ in range(parts)]
                for i, it in enumerate(items):
                    groups[i % parts].append(it)
                return [g for g in groups if g]

            groups = partition_round_robin(users_with_tokens, process_workers)

            procs = []
            try:
                for i, grp in enumerate(groups):
                    p = multiprocessing.Process(target=process_worker, args=(i, config, grp, images_data, debug), daemon=False)
                    p.start()
                    procs.append(p)

                # 主进程等待所有 worker 进程结束（通常只有在用户停止或异常退出时）
                try:
                    for p in procs:
                        p.join()
                except KeyboardInterrupt:
                    logging.info('收到 KeyboardInterrupt，等待子进程退出...')
            finally:
                # 尝试优雅终止子进程
                for p in procs:
                    try:
                        if p.is_alive():
                            p.terminate()
                    except Exception:
                        pass

        elif thread_workers == 1:
            asyncio.run(run_forever(config, users_with_tokens, images_data, debug))
        else:
            # 将 users_with_tokens 轮询分配到 N 个分片，保证尽量均衡
            def partition_round_robin(items, parts):
                groups = [[] for _ in range(parts)]
                for i, it in enumerate(items):
                    groups[i % parts].append(it)
                return [g for g in groups if g]

            groups = partition_round_robin(users_with_tokens, thread_workers)

            import threading as _threading
            import asyncio as _asyncio

            threads = []

            def _worker_thread(idx, cfg, users_sub, images_sub, dbg):
                """每个线程创建独立的 asyncio loop 并运行 run_forever"""
                try:
                    loop = _asyncio.new_event_loop()
                    _asyncio.set_event_loop(loop)
                    loop.run_until_complete(run_forever(cfg, users_sub, images_sub, dbg))
                except Exception:
                    import logging as _logging
                    _logging.exception(f"线程 worker #{idx} 出现未处理异常")
                finally:
                    try:
                        loop.close()
                    except Exception:
                        pass

            for i, grp in enumerate(groups):
                t = _threading.Thread(target=_worker_thread, args=(i, config, grp, images_data, debug), daemon=False, name=f"WSWorker-{i}")
                t.start()
                threads.append(t)

            # 主线程等待所有 worker 线程结束（通常只有在用户停止或异常退出时）
            try:
                for t in threads:
                    t.join()
            except KeyboardInterrupt:
                logging.info('收到 KeyboardInterrupt，等待子线程退出...')
    except Exception as e:
        # 捕获顶层未处理异常，记录日志并尝试优雅停止后台任务
        logging.exception(f"主程序发生未处理异常: {e}")
        # 通过 locals().get 获取 gui_state，避免在没有该变量时触发静态分析错误
        try:
            gs = locals().get('gui_state')
            if gs:
                try:
                    with gs['lock']:
                        gs['stop'] = True
                        try:
                            gs['backend_status'] = 'exception'
                            gs['backend_exception'] = str(e)
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception:
            pass
    finally:
        # 程序退出时通知自动重启线程取消（如果存在）
        try:
            restart_stop_event.set()
        except Exception:
            pass

if __name__ == "__main__":
    main_wrapper()
