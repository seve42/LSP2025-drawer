"""
LSP2025-drawer 主程序

运行模式说明：
1. 默认模式：
   直接运行 `python main.py`。
   启动 WebUI (默认端口80)，自动根据 config.json 配置进行绘图。
   支持多进程/多线程并发绘图。

2. CLI 模式 (`-cli`)：
   运行 `python main.py -cli`。
   仅在命令行运行，不启动 WebUI 服务器。
   适合在无头服务器或不需要 Web 界面时使用。

3. 手动模式 (`-hand`)：
   运行 `python main.py -hand`。
   启动手动绘板模式，强制单线程运行。
   通常用于测试或手动控制绘图。

4. 调试模式 (`-debug`)：
   运行 `python main.py -debug`。
   启用详细日志输出 (DEBUG 级别)，方便排查问题。
   可与其他模式组合使用，如 `python main.py -cli -debug`。

配置说明：
1. 绘制模式 (draw_mode):
   - random: 随机顺序绘制像素（默认）。
   - horizontal: 逐行扫描绘制（从左到右，从上到下）。
   - concentric: 从中心向外扩散绘制。

2. 扫描模式 (scan_mode):
   - normal: 默认模式。绘制失败优先重试，检测到被覆盖放到队尾。
   - strict: 严格模式。绘制失败或检测到像素被覆盖，均优先重试（插入队首）。
   - loop: 循环模式。绘制失败或检测到像素被覆盖，均放到队尾重新排队。
"""
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
LAST_LOG_FILE = "last.log"

# 清空 last.log 文件（启动时）
try:
    with open(LAST_LOG_FILE, 'w', encoding='utf-8') as f:
        f.write(f"=== LSP2025-drawer 启动于 {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
except Exception:
    pass

# 创建专用的 last.log 日志记录器
last_logger = logging.getLogger('last')
last_logger.setLevel(logging.DEBUG)
last_handler = logging.FileHandler(LAST_LOG_FILE, encoding='utf-8', mode='a')
last_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
last_logger.addHandler(last_handler)

def log_last(level: str, message: str):
    """写入 last.log 的便捷函数，同时也写入主日志"""
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    level_upper = level.upper()
    try:
        if level_upper == 'DEBUG':
            last_logger.debug(message)
        elif level_upper == 'INFO':
            last_logger.info(message)
        elif level_upper == 'WARNING':
            last_logger.warning(message)
        elif level_upper == 'ERROR':
            last_logger.error(message)
        else:
            last_logger.info(message)
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("paint.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)


def process_worker(idx, cfg, users_sub, images_sub, dbg, precomputed_target=None):
    """顶层函数：在子进程中启动独立的 asyncio 事件循环并运行 run_forever。

    该函数需要位于模块顶层以便 multiprocessing 在 Windows 上能够正确导入并调用。
    注意：images_sub 可能包含大量像素数据，会在进程间被序列化/复制。
    """
    import asyncio as _asyncio
    try:
        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        loop.run_until_complete(run_forever(cfg, users_sub, images_sub, dbg, precomputed_target=precomputed_target))
    except Exception:
        import logging as _logging
        _logging.exception(f"进程 worker #{idx} 出现未处理异常")
    finally:
        try:
            loop.close()
        except Exception:
            pass

# pending paints waiting for confirmation via board update: dict {paint_id: {uid, pos, color, time, image_idx}}
pending_paints = {}
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

def get_merged_data(max_size=32768):
    """合并队列中的数据块，最多不超过max_size字节
    
    优化：
    1. 限制单次提取大小，避免超过32KB限制
    2. 保留未发送的数据在队列中
    3. 以像素为单位（31字节），不拆分单个像素
    """
    global paint_queue, total_size
    if not paint_queue:
        return None
    
    # 计算可以发送的数据量，以像素为单位（31字节/像素）
    max_pixels = max_size // 31
    actual_size = min(total_size, max_pixels * 31)
    
    # 如果数据量很小且不急，等待更多
    if actual_size < 155 and total_size < max_size // 2:  # 5像素
        return None
    
    merged = bytearray(actual_size)
    offset = 0
    extracted_chunks = []
    
    while paint_queue and offset < actual_size:
        chunk = paint_queue[0]
        chunk_len = len(chunk)
        
        if offset + chunk_len <= actual_size:
            # 完整放入
            merged[offset:offset + chunk_len] = chunk
            offset += chunk_len
            paint_queue.popleft()
            extracted_chunks.append(chunk)
        else:
            # 部分放入（以像素为单位）
            remaining = actual_size - offset
            pixels_to_take = remaining // 31
            bytes_to_take = pixels_to_take * 31
            
            if bytes_to_take > 0:
                merged[offset:offset + bytes_to_take] = chunk[:bytes_to_take]
                offset += bytes_to_take
                # 修改原始 chunk，移除已提取部分
                paint_queue[0] = chunk[bytes_to_take:]
            break
    
    total_size -= offset
    return merged if offset > 0 else None

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
    """定时发送粘合后的绘画数据包，支持动态批量和智能粘包"""
    max_packet_size = 32768  # 32KB 服务器限制
    last_send_time = time.monotonic()
    
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
            merged_data = get_merged_data(max_size=max_packet_size)
            if merged_data:
                data_size = len(merged_data)
                pixel_count = data_size // 31  # 每个像素31字节
                
                try:
                    await ws.send(merged_data)
                    now = time.monotonic()
                    actual_interval = (now - last_send_time) * 1000
                    last_send_time = now
                    logging.debug(f"发送 {pixel_count}px ({data_size}字节)，间隔{actual_interval:.1f}ms")
                except websockets.ConnectionClosed:
                    logging.warning("发送数据时 WebSocket 连接已关闭。")
                    # 连接关闭时数据已经丢失，不放回队列（会在上层重试机制处理）
                except Exception as e:
                    logging.warning(f"发送数据时出错: {e}")
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
        cfg.setdefault('max_enabled_tokens', 0)  # 最大启用token数，0表示不限制
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


async def handle_websocket(config, users_with_tokens, images_data, debug=False, gui_state=None, precomputed_target=None):
    """主 WebSocket 处理函数

    修复点：
    - 将发送/绘制/接收全部放入 WebSocket 上下文中，确保连接在任务期间保持打开。
    - 并发接收服务端消息，及时应答 0xfc 心跳为 0xfb，避免被断开。
    - 支持多图片绘制，合并目标映射并按权重处理重叠
    """
    paint_interval_ms = config.get("paint_interval_ms", 20)
    round_interval_seconds = config.get("round_interval_seconds", 30)
    user_cooldown_seconds = config.get("user_cooldown_seconds", 30)

    # 性能优化：根据用户冷却时间和token数量智能调整发送间隔
    # 目标：在不超过服务器速率限制（256包/秒）的前提下最大化吞吐量
    num_tokens = len(users_with_tokens)
    if user_cooldown_seconds < 0.1 and num_tokens > 0:
        # 极短冷却模式：动态计算最优发送间隔
        # 理论每秒像素数 = token数量 / 冷却时间
        theoretical_px_per_sec = num_tokens / user_cooldown_seconds
        # 服务器限制：256包/秒，即每包间隔约 3.9ms
        # 目标：每个包包含足够多的像素以减少延迟损失
        # 最优策略：让每个包包含 冷却时间内可绘制的像素数
        optimal_pixels_per_packet = max(1, int(num_tokens * paint_interval_ms / 1000.0 / user_cooldown_seconds))
        # 如果每包像素太少，增加发送间隔以积累更多像素
        if optimal_pixels_per_packet < 5:
            # 调整为每包至少5-10个像素，减少网络开销
            paint_interval_ms = max(5, int(5 * user_cooldown_seconds * 1000.0 / num_tokens))
            paint_interval_ms = min(paint_interval_ms, 20)  # 不超过20ms以保持响应性
        else:
            # 使用接近256包/秒限制的间隔，但留有余量
            paint_interval_ms = max(4, paint_interval_ms)  # 250包/秒
        logging.info(f"智能调整：{num_tokens}个token，CD={user_cooldown_seconds*1000:.1f}ms，发送间隔={paint_interval_ms}ms，预期每包{optimal_pixels_per_packet}像素")
    else:
        # 正常冷却模式：保持配置的发送间隔或至少 10ms
        paint_interval_ms = max(10, paint_interval_ms)

    # 合并所有图片的目标像素映射（处理重叠，高权重优先）
    # tool.merge_target_maps 现在也返回每个绝对坐标对应的图片索引映射 pos_to_image_idx
    if precomputed_target:
        result = precomputed_target
    else:
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
    
    # 构建 image_idx -> scan_mode 映射
    image_idx_to_scan_mode = {}
    try:
        for img in images_data:
            cfg_idx = img.get('config_index')
            if cfg_idx is not None:
                image_idx_to_scan_mode[cfg_idx] = str(img.get('scan_mode', 'normal')).lower()
    except Exception:
        pass

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
    # 统计信息：利用率
    stats = {'sent': 0, 'success': 0}
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
            # 添加连接分隔符到日志（方便区分不同连接会话）
            log_last('INFO', '=' * 80)
            log_last('INFO', f"新连接建立于 {time.strftime('%Y-%m-%d %H:%M:%S')}")
            log_last('INFO', '=' * 80)
            logging.info("WebSocket 连接已建立")
            log_last('INFO', f"WebSocket 连接已建立 - URL: {WS_URL}")
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

                设计目标：分摊发送压力到多个连接，提高并发发送能力。
                【重要修复】只写连接仍需响应心跳！服务端会向所有连接发送 Ping，
                只写连接也必须响应 Pong，否则会被 1001 Ping timeout 断开。
                
                解决方案：同时运行心跳接收任务和发送任务。
                """
                write_url = WS_URL + "?writeonly=1"
                try:
                    async with websockets.connect(
                        write_url,
                        ping_interval=None,
                        ping_timeout=None,
                        open_timeout=30,
                        close_timeout=10,
                        max_size=10 * 1024 * 1024,
                    ) as write_ws:
                        logging.info(f"写连接 #{idx} 已建立 (writeonly模式)")
                        log_last('INFO', f"写连接 #{idx} 已建立 (writeonly模式)")
                        
                        async def heartbeat_handler():
                            """处理只写连接的心跳：收到 Ping 立即回复 Pong"""
                            ping_count = 0
                            try:
                                async for message in write_ws:
                                    try:
                                        if isinstance(message, str):
                                            data = bytearray(message.encode())
                                        else:
                                            data = bytearray(message)
                                        offset = 0
                                        while offset < len(data):
                                            opcode = data[offset]
                                            offset += 1
                                            if opcode == 0xfc:  # Ping
                                                ping_count += 1
                                                try:
                                                    await write_ws.send(bytes([0xfb]))
                                                    logging.debug(f"写连接 #{idx} 心跳 #{ping_count}: Ping -> Pong")
                                                except (websockets.exceptions.ConnectionClosed,
                                                        websockets.exceptions.ConnectionClosedError,
                                                        websockets.exceptions.ConnectionClosedOK) as e:
                                                    err_msg = str(e) if str(e) else e.__class__.__name__
                                                    logging.warning(f"写连接 #{idx} 发送 Pong 失败（连接已关闭）: {err_msg} (总计收到{ping_count}个Ping)")
                                                    log_last('ERROR', f"写连接 #{idx} Pong失败（连接关闭）: {err_msg} (总计{ping_count}个Ping)")
                                                    return
                                                except Exception as e:
                                                    logging.warning(f"写连接 #{idx} 发送 Pong 失败: {e}")
                                                    log_last('ERROR', f"写连接 #{idx} 发送 Pong 失败: {e}")
                                                    return
                                            elif opcode == 0xff:  # 绘画结果
                                                offset += 5  # 跳过 paint_id(4) + status(1)
                                            elif opcode == 0xfa:  # 画板更新
                                                offset += 7  # 跳过 x(2) + y(2) + r(1) + g(1) + b(1)
                                            else:
                                                pass  # 忽略其他消息
                                    except Exception as e:
                                        logging.debug(f"写连接 #{idx} 处理消息时出错: {e}")
                            except asyncio.CancelledError:
                                raise
                            except Exception as e:
                                logging.warning(f"写连接 #{idx} 心跳处理器异常退出: {e}")
                                log_last('ERROR', f"写连接 #{idx} 心跳处理器异常退出: {e}")
                        
                        # 同时运行心跳处理和发送任务
                        heartbeat_task = asyncio.create_task(heartbeat_handler())
                        send_task = asyncio.create_task(tool.send_paint_data(write_ws, paint_interval_ms, paint_queue_event))
                        
                        try:
                            # 等待任意一个任务完成（通常是因为连接关闭）
                            done, pending = await asyncio.wait(
                                [heartbeat_task, send_task],
                                return_when=asyncio.FIRST_COMPLETED
                            )
                            # 取消剩余任务
                            for task in pending:
                                task.cancel()
                                try:
                                    await task
                                except asyncio.CancelledError:
                                    pass
                        except asyncio.CancelledError:
                            heartbeat_task.cancel()
                            send_task.cancel()
                            try:
                                await heartbeat_task
                            except asyncio.CancelledError:
                                pass
                            try:
                                await send_task
                            except asyncio.CancelledError:
                                pass
                            raise
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logging.warning(f"写连接 #{idx} 异常退出: {e}")
                    log_last('ERROR', f"写连接 #{idx} 异常退出: {e}")
                    # 短暂等待后将由外层循环重连
                    await asyncio.sleep(1.0)

            async def write_only_worker_with_reconnect(idx: int):
                """带自动重连的只写连接任务"""
                reconnect_count = 0
                while True:
                    try:
                        await write_only_worker(idx)
                        # 如果正常退出（不太可能），等待后重连
                        reconnect_count += 1
                        logging.info(f"写连接 #{idx} 正常退出，准备第 {reconnect_count} 次重连...")
                        await asyncio.sleep(2.0)
                    except asyncio.CancelledError:
                        logging.debug(f"写连接 #{idx} 任务被取消")
                        raise
                    except Exception as e:
                        reconnect_count += 1
                        logging.warning(f"写连接 #{idx} 异常，准备第 {reconnect_count} 次重连: {e}")
                        log_last('WARNING', f"写连接 #{idx} 准备第 {reconnect_count} 次重连")
                        await asyncio.sleep(min(5.0, 1.0 * reconnect_count))  # 指数退避，最多5秒

            for i in range(extra_writers):
                try:
                    t = asyncio.create_task(write_only_worker_with_reconnect(i))
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
            # recent_success 集合：记录最近收到 0xef 成功响应的坐标，避免短时间内重复绘制
            # 格式: {pos: expire_time}
            recent_success = {}
            
            # 【连接健康监控】
            # 跟踪最后一次成功接收服务器消息的时间
            last_message_time = time.monotonic()
            # 跟踪最后一次成功接收心跳(Ping)的时间
            last_ping_received = time.monotonic()
            # 需要重连的标志（由进度监控器或健康检查设置）
            need_reconnect = False
            # 连续零像素增长的秒数（超过阈值触发重连）
            zero_growth_seconds = 0
            # 零增长触发重连的阈值（秒）
            ZERO_GROWTH_RECONNECT_THRESHOLD = 120  # 2分钟无增长则重连

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
            
            # 初始化待绘队列（供 receiver 增量更新）
            remaining = _deque()

            # --- 重要：在定义 receiver() 前初始化所有它依赖的状态变量 ---
            # Token 池：记录每个 Token 的状态（仅记录last_success用于冷却判断）
            # uid -> {'last_success': 0.0, 'fail_count': 0, 'invalid_count': 0}
            # fail_count: 连续失败次数（用于退避）
            # invalid_count: Token 无效错误计数（0xed）
            token_states = {u['uid']: {'last_success': 0.0, 'fail_count': 0, 'invalid_count': 0} for u in users_with_tokens}
            
            # Token 刷新请求队列（uid -> need_refresh）
            token_refresh_needed = set()
            
            # 任务池：记录当前正在进行的绘制任务（仅用于统计和0xff响应匹配）
            # paint_id -> {'pos': (x,y), 'color': (r,g,b), 'uid': uid, 'time': monotonic_time}
            active_tasks = {}
            
            # 位置锁：记录哪些位置最近被绘制过，避免短时间内重复提交
            # pos -> expire_timestamp（锁定到此时间戳，之后可以重新绘制）
            pos_locks = {}
            
            # 扫描游标：记录上次扫描到的位置索引，实现循环扫描（Round Robin）
            scan_cursor = 0
            
            # 统计信息（供进度条显示）
            # 如果 gui_state 已经有 stats，直接使用它（测量模式）
            # 否则创建新的 stats 并暴露给 gui_state（普通模式）
            if gui_state is not None and 'stats' in gui_state:
                stats = gui_state['stats']  # 直接引用，让后续修改生效
            else:
                stats = {'sent': 0, 'success': 0}
                if gui_state is not None:
                    with gui_state['lock']:
                        gui_state['stats'] = stats

            # 启动接收任务：处理 Ping(0xfc)、绘画结果(0xff)、画板更新(0xfa) 等
            async def receiver():
                """接收并处理服务器消息
                
                【关键修复】简化心跳处理：
                - 收到 0xfc Ping 时立即直接发送 0xfb Pong，不使用多进程通信
                - 避免多进程队列延迟导致心跳超时 (1001 Ping timeout)
                - 更新 last_message_time 和 last_ping_received 供健康检查使用
                """
                nonlocal ping_in_q, ping_out_q, ping_proc, last_message_time, last_ping_received, need_reconnect
                
                pong_failures = 0
                message_count = 0
                ping_count = 0
                opcode_stats = {'0xfc': 0, '0xff': 0, '0xfa': 0, 'other': 0}  # 统计各类消息
                
                try:
                    async for message in ws:
                        message_count += 1
                        # 更新最后消息接收时间（用于健康检查）
                        last_message_time = time.monotonic()
                        
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
                                    opcode_stats['0xfc'] += 1
                                    # 【关键修复】立即直接发送 Pong，不使用多进程队列
                                    # 根据文档：收到 Ping 后应立即响应，否则会被断开 (1001 Ping timeout)
                                    ping_count += 1
                                    last_ping_received = time.monotonic()
                                    
                                    try:
                                        t_recv = time.monotonic()
                                        await ws.send(bytes([0xfb]))
                                        t_sent = time.monotonic()
                                        response_time_ms = (t_sent - t_recv) * 1000
                                        logging.info(f"心跳 #{ping_count}: Ping -> Pong (响应时间: {response_time_ms:.2f}ms)")
                                        log_last('INFO', f"心跳 #{ping_count}: Ping -> Pong ({response_time_ms:.2f}ms)")
                                        pong_failures = 0
                                    except (websockets.exceptions.ConnectionClosed,
                                            websockets.exceptions.ConnectionClosedError,
                                            websockets.exceptions.ConnectionClosedOK) as e:
                                        err_msg = str(e) if str(e) else e.__class__.__name__
                                        time_since_last_ping = time.monotonic() - last_ping_received
                                        logging.warning(f"[主连接] 发送 Pong 时连接已关闭: {err_msg} (距上次Ping: {time_since_last_ping:.1f}s, 总计收到{ping_count}个Ping)")
                                        log_last('ERROR', f"[主连接] 发送 Pong 时连接已关闭: {err_msg} (距上次Ping: {time_since_last_ping:.1f}s, 总计{ping_count}个Ping)")
                                        return
                                    except Exception as e:
                                        err_msg = str(e) if str(e) else e.__class__.__name__
                                        pong_failures += 1
                                        logging.warning(f"发送 Pong 失败 (#{pong_failures}): {err_msg}")
                                        log_last('ERROR', f"发送 Pong 失败 (#{pong_failures}): {err_msg}")
                                        if pong_failures >= 3:
                                            logging.error(f"Pong 连续失败 {pong_failures} 次，标记需要重连")
                                            log_last('ERROR', f"Pong 连续失败 {pong_failures} 次，标记需要重连")
                                            need_reconnect = True
                                            return
                                            
                                elif opcode == 0xff:  # 绘画结果
                                    opcode_stats['0xff'] += 1
                                    if offset + 5 > len(data):
                                        break
                                    paint_id = int.from_bytes(data[offset:offset+4], 'little')
                                    status_code = data[offset+4]
                                    
                                    task = active_tasks.get(paint_id)
                                    if not task and status_code == 0xef:
                                        # 调试：成功响应但找不到任务记录
                                        log_last('WARNING', f"收到成功响应(0xef) 但找不到任务 paint_id={paint_id}，active_tasks数量={len(active_tasks)}")
                                    
                                    if task:
                                        uid = task['uid']
                                        pos = task['pos']
                                        r, g, b = task['color']
                                        
                                        if status_code == 0xef:
                                            # 成功响应（仅用于重置失败计数器，success已在发送时计入）
                                            # 注意：stats['success'] 已在发送时 += 1，这里不再重复计数
                                            
                                            # 重置失败计数器
                                            if uid in token_states:
                                                token_states[uid]['fail_count'] = 0
                                                token_states[uid]['invalid_count'] = 0
                                            
                                            # 更新用户快照（用于统计）
                                            snap = user_last_snapshot.get(uid)
                                            if snap is None:
                                                snap = OrderedDict()
                                                user_last_snapshot[uid] = snap
                                            snap[pos] = (r, g, b)
                                            while len(snap) > SNAPSHOT_SIZE:
                                                snap.popitem(last=False)
                                            
                                            # 乐观更新 board_state（稍后 0xfa 会确认）
                                            board_state[pos] = (r, g, b)
                                            
                                            # 清理任务记录
                                            del active_tasks[paint_id]
                                        else:
                                            # 失败响应处理
                                            if uid in token_states:
                                                token_states[uid]['fail_count'] += 1
                                            
                                            # 特殊处理不同的错误码
                                            if status_code == 0xed:  # Token 无效
                                                if uid in token_states:
                                                    token_states[uid]['invalid_count'] += 1
                                                    invalid_cnt = token_states[uid]['invalid_count']
                                                    logging.warning(f"【Token失效】uid={uid} 收到 0xed 错误（第{invalid_cnt}次），标记需要刷新")
                                                    log_last('ERROR', f"Token失效 uid={uid} (0xed) 第{invalid_cnt}次")
                                                    # 标记此用户需要刷新 token
                                                    token_refresh_needed.add(uid)
                                                else:
                                                    logging.warning(f"【Token失效】uid={uid} 收到 0xed 错误，但用户不在状态表中")
                                                    log_last('ERROR', f"Token失效 uid={uid} (0xed) 用户不在状态表")
                                            elif status_code == 0xee:  # 冷却中
                                                pass  # 冷却错误太频繁，不记录
                                            elif status_code == 0xec:  # 请求格式错误
                                                logging.warning(f"绘画请求格式错误(0xec) uid={uid} ID={paint_id} Pos={pos}")
                                                log_last('ERROR', f"请求格式错误 (0xec) uid={uid} Pos={pos}")
                                            elif status_code == 0xeb:  # 无权限
                                                logging.warning(f"绘画无权限(0xeb) uid={uid} ID={paint_id} Pos={pos}")
                                                log_last('ERROR', f"绘画无权限 (0xeb) uid={uid} Pos={pos}")
                                            elif status_code == 0xea:  # 服务器错误
                                                logging.warning(f"服务器错误(0xea) uid={uid} ID={paint_id} Pos={pos}")
                                                log_last('ERROR', f"服务器错误 (0xea) uid={uid} Pos={pos}")
                                            else:
                                                logging.debug(f"绘画失败(0x{status_code:x}) uid={uid} ID={paint_id} Pos={pos}")
                                            
                                            # 清理任务记录
                                            del active_tasks[paint_id]

                                    offset += 5
                                elif opcode == 0xfa:  # 画板像素更新广播 x(2) y(2) rgb(3)
                                    opcode_stats['0xfa'] += 1
                                    if offset + 7 > len(data):
                                        break
                                    try:
                                        x = int.from_bytes(data[offset:offset+2], 'little'); offset += 2
                                        y = int.from_bytes(data[offset:offset+2], 'little'); offset += 2
                                        r, g, b = data[offset], data[offset+1], data[offset+2]; offset += 3
                                        
                                        # 【性能优化】简化画板更新处理，避免遍历 active_tasks
                                        # 0xff 已经足够准确地处理绘画结果，0xfa 只需更新状态
                                        board_state[(x, y)] = (r, g, b)
                                        
                                        # 同步到 GUI（批量更新以减少锁竞争）
                                        if gui_state is not None:
                                            try:
                                                with gui_state['lock']:
                                                    gui_state['board_state'][(x, y)] = (r, g, b)
                                            except Exception:
                                                pass
                                    except Exception:
                                        # 出错则跳过此条
                                        pass
                                else:
                                    opcode_stats['other'] += 1
                                    logging.warning(f"收到未知操作码: 0x{opcode:x}")
                            
                            # 成功处理消息后重置错误计数
                            consecutive_errors = 0
                            
                        except Exception as e:
                            err_msg = str(e) if str(e) else e.__class__.__name__
                            err_type = e.__class__.__name__
                            # 【修复】消息解析错误不应导致任务退出，只记录警告
                            logging.warning(f"处理消息时出错 ({err_type}): {err_msg}，跳过此消息")
                            log_last('WARNING', f"消息解析错误 ({err_type}): {err_msg}")
                    
                    # 正常退出循环（连接关闭）
                    logging.info(f"WebSocket 消息流结束（共接收 {message_count} 条消息），接收任务退出。")
                    log_last('WARNING', f"WebSocket 消息流结束，共接收 {message_count} 条消息，接收任务退出")
                    if ping_proc is not None:
                        logging.info(f"Ping 进程 pid={getattr(ping_proc,'pid',None)} 状态: {'alive' if ping_proc.is_alive() else 'stopped'}")
                    else:
                        logging.info("Ping 进程信息不可用（使用内置心跳或未创建）")
                    
                except (websockets.exceptions.ConnectionClosed,
                        websockets.exceptions.ConnectionClosedError,
                        websockets.exceptions.ConnectionClosedOK) as e:
                    err_msg = str(e) if str(e) else e.__class__.__name__
                    logging.info(f"WebSocket 连接已关闭: {err_msg} (接收了 {message_count} 条消息)")
                    log_last('ERROR', f"WebSocket 连接已关闭: {err_msg} (接收了 {message_count} 条消息)")
                    if ping_proc is not None:
                        logging.info(f"Ping 进程 pid={getattr(ping_proc,'pid',None)} 状态: {'alive' if ping_proc.is_alive() else 'stopped'}")
                    else:
                        logging.info("Ping 进程信息不可用（使用内置心跳或未创建）")
                except asyncio.CancelledError:
                    # 任务被取消时正常退出
                    logging.debug(f"接收任务被取消 (已接收 {message_count} 条消息: Ping={opcode_stats['0xfc']}, 绘画结果={opcode_stats['0xff']}, 画板更新={opcode_stats['0xfa']}, 其他={opcode_stats['other']})")
                    log_last('INFO', f"接收任务被取消 (消息: {message_count}条, Ping={opcode_stats['0xfc']}, 结果={opcode_stats['0xff']}, 更新={opcode_stats['0xfa']}, 其他={opcode_stats['other']})")
                    if ping_proc is not None:
                        logging.debug(f"Ping 进程 pid={getattr(ping_proc,'pid',None)} 状态: {'alive' if ping_proc.is_alive() else 'stopped'}")
                    else:
                        logging.debug("Ping 进程信息不可用（使用内置心跳或未创建）")
                    raise
                except Exception as e:
                    err_msg = str(e) if str(e) else e.__class__.__name__
                    err_type = e.__class__.__name__
                    logging.exception(f"WebSocket 接收处理时发生未预期异常 ({err_type}): {err_msg}")
                    log_last('ERROR', f"WebSocket 接收未预期异常 ({err_type}): {err_msg}")
                    if ping_proc is not None:
                        logging.info(f"Ping 进程 pid={getattr(ping_proc,'pid',None)} 状态: {'alive' if ping_proc.is_alive() else 'stopped'}")
                    else:
                        logging.info("Ping 进程信息不可用（使用内置心跳或未创建）")
                finally:
                    logging.info("接收任务已退出。")

            receiver_task = asyncio.create_task(receiver())

            # 定义重新获取快照的函数
            is_fetching_snapshot = False
            async def try_refetch_snapshot():
                nonlocal is_fetching_snapshot
                if is_fetching_snapshot:
                    return
                is_fetching_snapshot = True
                try:
                    logging.info("检测到画板状态为空，正在重新获取快照...")
                    loop = asyncio.get_running_loop()
                    snapshot = await loop.run_in_executor(None, tool.fetch_board_snapshot)
                    if snapshot:
                        board_state.update(snapshot)
                        logging.info(f"成功重新获取画板快照，包含 {len(snapshot)} 个像素")
                        # 更新 GUI
                        if gui_state is not None:
                            with gui_state['lock']:
                                gui_state['board_state'] = board_state.copy()
                except Exception as e:
                    logging.warning(f"重新获取画板快照失败: {e}")
                finally:
                    is_fetching_snapshot = False

            # 启动进度显示器（每秒刷新）
            async def progress_printer():
                # 声明需要修改的外层变量
                nonlocal zero_growth_seconds, need_reconnect
                
                # 使用 rich 渲染更美观的进度条
                mode_prefix = f"[{len(images_data)}图] "
                history = deque()
                window_seconds = 60.0
                
                # 用于计算每秒成功绘制像素数（基于 stats['success']）
                pixels_history = deque()  # [(timestamp, success_count), ...]
                pixels_window_seconds = 10.0  # 10秒窗口计算平均速度

                console = Console()
                progress = Progress(
                    SpinnerColumn(),
                    TextColumn("[bold blue]{task.fields[line1]}"),
                    BarColumn(bar_width=30),  # 缩短进度条
                    TextColumn("{task.percentage:>3.0f}%"),
                    TimeRemainingColumn(),
                    TextColumn("\n[cyan]{task.fields[line2]}"),
                    console=console,
                    transient=False,
                    refresh_per_second=2,  # 降低刷新率
                )
                task_id = progress.add_task(
                    f"{mode_prefix}进度", 
                    total=100,
                    line1="初始化...",
                    line2=""
                )
                progress.start()
                # 立即更新一次，确保进度条显示
                progress.update(task_id, completed=0)
                try:
                    while True:
                        # 记录当前时间，供后续多个指标使用
                        now = time.monotonic()

                        # 计算完成度（不符合的像素数会降低完成度）
                        total = len(target_positions)
                        if not board_state:
                            mismatched = total
                            # 画板为空时尝试重新获取
                            if int(now) % 10 == 0:
                                asyncio.create_task(try_refetch_snapshot())
                        else:
                            mismatched = len([pos for pos in target_positions if board_state.get(pos) != target_map[pos]])
                        completed = max(0, total - mismatched)
                        pct = (completed / total * 100) if total > 0 else 100.0
                        
                        # 计算每秒成功绘制的像素数（基于收到 0xef 响应的次数）
                        success_count = stats['success']
                        pixels_history.append((now, success_count))
                        while pixels_history and (now - pixels_history[0][0] > pixels_window_seconds):
                            pixels_history.popleft()
                        
                        pixels_per_sec = 0.0
                        if len(pixels_history) >= 2:
                            t0, p0 = pixels_history[0]
                            t1, p1 = pixels_history[-1]
                            dt = max(1e-6, t1 - t0)
                            pixels_per_sec = (p1 - p0) / dt
                        
                        # 【自动重连检测】检测像素增长是否为0，连续超过阈值则触发重连
                        # 条件：有未达成像素 且 有可用token 且 像素增长为0
                        if mismatched > 0 and len(users_with_tokens) > 0 and pixels_per_sec < 0.01:
                            zero_growth_seconds += 1
                            if zero_growth_seconds >= ZERO_GROWTH_RECONNECT_THRESHOLD:
                                logging.warning(f"检测到连续 {zero_growth_seconds} 秒无像素增长，标记需要重连")
                                need_reconnect = True
                                zero_growth_seconds = 0  # 重置计数避免重复触发
                        else:
                            # 有增长，重置计数
                            if zero_growth_seconds > 0:
                                logging.debug(f"像素增长恢复，重置零增长计数（之前: {zero_growth_seconds}秒）")
                            zero_growth_seconds = 0
                        
                        # 【连接健康检测】检查是否长时间没有收到服务器消息
                        # 服务器应该定期发送心跳，如果超过60秒没有任何消息，可能连接已死
                        MESSAGE_TIMEOUT = 60.0
                        time_since_last_message = now - last_message_time
                        if time_since_last_message > MESSAGE_TIMEOUT:
                            logging.warning(f"超过 {time_since_last_message:.1f}s 未收到服务器消息，可能连接已死，标记需要重连")
                            need_reconnect = True

                        # 可用用户数与就绪数
                        available = len(users_with_tokens)
                        # 使用 token_states 计算就绪数：已过冷却期即可用
                        ready_count = 0
                        for u in users_with_tokens:
                            uid = u['uid']
                            state = token_states.get(uid)
                            if state:
                                # 检查是否过了冷却期
                                if now - state['last_success'] >= user_cooldown_seconds:
                                    ready_count += 1

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
                                        eta_str = '  估计剩余: 无限大'
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

                        # 计算CD中的token利用率：(CD中的token数 / 总token数) * 100%
                        cd_util_str = ''
                        try:
                            total_tokens = len(users_with_tokens)
                            if total_tokens > 0:
                                # 统计正在CD中的token数（距离上次成功时间 < 冷却时间）
                                tokens_in_cd = 0
                                for u in users_with_tokens:
                                    uid = u['uid']
                                    state = token_states.get(uid)
                                    if state:
                                        # 如果在冷却期内，则认为在CD中
                                        if now - state['last_success'] < user_cooldown_seconds:
                                            tokens_in_cd += 1
                                cd_util_rate = (tokens_in_cd / total_tokens) * 100.0
                                cd_util_str = f"CD利用:{cd_util_rate:5.1f}%"
                            else:
                                cd_util_str = "CD利用: --"
                        except Exception:
                            cd_util_str = "CD利用: --"
                        
                        # 计算效率：实际每秒像素数 / 理论每秒像素数
                        # 理论每秒像素数 = token总数 / CD时间
                        efficiency_str = ''
                        try:
                            total_tokens = len(users_with_tokens)
                            if total_tokens > 0 and user_cooldown_seconds > 0:
                                theoretical_pps = total_tokens / user_cooldown_seconds
                                if theoretical_pps > 0:
                                    efficiency_rate = (pixels_per_sec / theoretical_pps) * 100.0
                                    efficiency_str = f"效率:{efficiency_rate:5.1f}%"
                                else:
                                    efficiency_str = "效率: --"
                            else:
                                efficiency_str = "效率: --"
                        except Exception:
                            efficiency_str = "效率: --"

                        # 构造多行描述信息
                        danger_mark = '⚠️' if danger else ''
                        
                        pixels_per_sec_str = f"{pixels_per_sec:+.1f}px/s" if pixels_per_sec != 0 else "0px/s"
                        success_total = stats['success']
                        
                        # 第一行：基本状态（紧凑格式）
                        line1_parts = [
                            f"{mode_prefix}可用:{available}",
                            f"就绪:{ready_count}",
                            f"未达:{mismatched}"
                        ]
                        if danger_mark:
                            line1_parts.insert(0, danger_mark)
                        line1 = ' | '.join(line1_parts)
                        
                        # 第二行：合并所有指标（紧凑格式）
                        line2_parts = [
                            f"速度:{pixels_per_sec_str}",
                            f"累计:{success_total}px",
                            cd_util_str,
                            efficiency_str,
                            res_part,
                            growth_str.strip(),
                            eta_str.strip()
                        ]
                        line2 = ' | '.join([p.strip() for p in line2_parts if p.strip()])

                        try:
                            progress.update(
                                task_id, 
                                completed=pct,
                                line1=line1,
                                line2=line2
                            )
                        except Exception as e:
                            # 回退为直接输出（简化版）
                            out_line = f"\r{mode_prefix}进度: [{int(pct):3d}%] 可用:{available} 就绪:{ready_count} 未达:{mismatched} 速度:{pixels_per_sec_str} {cd_util_str} {efficiency_str}"
                            if debug:
                                logging.info(out_line.strip())
                            else:
                                sys.stdout.write(out_line)
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
            # 使用全局 paint_id 计数器，避免不同用户生成相同的 paint_id 导致 pending_paints 冲突
            global_paint_id = 0
            
            # 性能统计（每10秒输出一次诊断信息）
            last_perf_log = time.monotonic()
            perf_stats = {
                'loops': 0,
                'assigned': 0,
                'scanned': 0,
                'no_ready': 0
            }

            # 启动进度显示器（每秒刷新）
            progress_task = asyncio.create_task(progress_printer())

            # 健康检查：定期验证连接和任务状态
            last_health_check = time.monotonic()
            health_check_interval = 10  # 每10秒进行一次完整健康检查
            last_task_check = time.monotonic()
            task_check_interval = 0.1  # 【优化】每0.1秒快速检查任务状态（原0.5秒太长）
            connection_warnings = 0  # 连续警告次数
            max_connection_warnings = 2  # 【优化】允许2次连续警告再退出（原3次太多）
            
            while True:
                now = time.monotonic()
                
                # 0. 【自动重连检测】检查 need_reconnect 标志
                if need_reconnect:
                    logging.warning("检测到需要重连标志，退出当前循环以便重连。")
                    try:
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
                    except asyncio.TimeoutError:
                        logging.debug("等待任务取消超时")
                    except Exception as e:
                        logging.debug(f"取消任务时出错: {e}")
                    break

                # 1. 清理陈旧的active_tasks记录（仅用于统计，不影响Token使用）
                # 保留最近5秒内的任务记录即可
                stale_ids = [pid for pid, task in active_tasks.items() if now - task['time'] > 5.0]
                for pid in stale_ids:
                    active_tasks.pop(pid, None)
                
                # 【修复】定期清理 pos_locks 中过期的条目，避免内存无限增长
                # 每1000次循环清理一次（避免每次循环都遍历大字典）
                if perf_stats['loops'] % 1000 == 0 and pos_locks:
                    expired_positions = [pos for pos, expire_time in pos_locks.items() if now >= expire_time]
                    for pos in expired_positions:
                        pos_locks.pop(pos, None)
                    if expired_positions:
                        logging.debug(f"清理了 {len(expired_positions)} 个过期的位置锁")
                        log_last('DEBUG', f"清理了 {len(expired_positions)} 个过期的位置锁，当前锁数: {len(pos_locks)}")

                # 2. 快速任务状态检查（每0.1秒）- 优先检测任务退出
                if now - last_task_check >= task_check_interval:
                    last_task_check = now
                    connection_issue = False
                    
                    # 检查发送任务状态
                    try:
                        if sender_task.done():
                            try:
                                exc = sender_task.exception() if not sender_task.cancelled() else None
                                if exc:
                                    logging.error(f"【关键】发送任务异常退出: {exc}")
                                    log_last('ERROR', f"发送任务异常退出: {exc}")
                                    connection_issue = True
                                else:
                                    logging.warning("【关键】发送任务已退出（无异常），这会导致停止绘制！")
                                    log_last('ERROR', "发送任务已退出（无异常），这会导致停止绘制！")
                                    connection_issue = True  # 即使无异常，任务退出也需要重连
                            except Exception as e:
                                logging.warning(f"【关键】发送任务已退出: {e}")
                                log_last('ERROR', f"发送任务已退出: {e}")
                                connection_issue = True
                    except Exception as e:
                        logging.debug(f"检查发送任务状态时出错: {e}")
                    
                    # 检查接收任务状态
                    try:
                        if receiver_task.done():
                            try:
                                exc = receiver_task.exception() if not receiver_task.cancelled() else None
                                if exc:
                                    logging.error(f"【关键】接收任务异常退出: {exc}")
                                    log_last('ERROR', f"接收任务异常退出: {exc}")
                                    connection_issue = True
                                else:
                                    logging.warning("【关键】接收任务已退出（无异常），这会导致无法接收心跳！")
                                    log_last('ERROR', "接收任务已退出（无异常），这会导致无法接收心跳！")
                                    connection_issue = True  # 接收任务退出也需要重连
                            except Exception as e:
                                logging.warning(f"【关键】接收任务已退出: {e}")
                                log_last('ERROR', f"接收任务已退出: {e}")
                                connection_issue = True
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
                
                # 3. 完整健康检查（每10秒）- 包含详细日志
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
                
                # 4. 优先处理 GUI 请求的配置刷新
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
                        
                        if gui_state is not None:
                            with gui_state['lock']:
                                gui_state['total'] = len(target_positions)
                                gui_state['mismatched'] = len([pos for pos in target_positions if board_state.get(pos) != target_map[pos]])
                                gui_state['pos_to_image_idx'] = dict(pos_to_image_idx)
                        logging.info('已根据 GUI 请求刷新目标像素与绘制顺序。')
                    except Exception:
                        logging.exception('处理 GUI 刷新请求时出错')

                # 视频功能已废弃

                # 若 GUI 模式要求停止，退出循环
                if gui_state is not None and gui_state.get('stop'):
                    logging.info('收到 GUI 退出信号，结束主循环。')
                    # 立即取消所有后台任务
                    progress_task.cancel()
                    sender_task.cancel()
                    receiver_task.cancel()
                    break
                
                # 实时处理 Token 刷新请求（检测到 0xed 后立即刷新）
                if token_refresh_needed:
                    uids_to_refresh = list(token_refresh_needed)
                    token_refresh_needed.clear()
                    logging.info(f"【实时刷新】检测到 {len(uids_to_refresh)} 个用户 Token 失效，立即刷新...")
                    
                    try:
                        # 在线程池中异步刷新 token
                        async def refresh_tokens_async():
                            def refresh_sync():
                                refreshed = []
                                for uid in uids_to_refresh:
                                    # 从原配置中查找此 uid 的 access_key
                                    user_cfg = None
                                    for u in config.get('users', []):
                                        if u.get('uid') == uid:
                                            user_cfg = u
                                            break
                                    
                                    if not user_cfg:
                                        logging.warning(f"未找到 uid={uid} 的配置，无法刷新")
                                        continue
                                    
                                    access_key = user_cfg.get('access_key')
                                    if not access_key:
                                        logging.warning(f"uid={uid} 没有 access_key，无法刷新")
                                        continue
                                    
                                    try:
                                        new_token = get_token(uid, access_key)
                                        if new_token:
                                            refreshed.append((uid, new_token))
                                            logging.info(f"【刷新成功】uid={uid} 已获取新 token")
                                        else:
                                            logging.warning(f"【刷新失败】uid={uid} 未能获取新 token")
                                    except Exception as e:
                                        logging.error(f"【刷新异常】uid={uid} 刷新失败: {e}")
                                return refreshed
                            
                            loop = asyncio.get_running_loop()
                            return await loop.run_in_executor(None, refresh_sync)
                        
                        refreshed_tokens = await refresh_tokens_async()
                        
                        # 更新 users_with_tokens 中对应用户的 token
                        for uid, new_token in refreshed_tokens:
                            try:
                                # 预计算 token_bytes
                                try:
                                    token_bytes = UUID(new_token).bytes
                                except Exception:
                                    token_bytes = UUID(hex=new_token.replace('-', '')).bytes
                                
                                # 查找并更新
                                for i, u in enumerate(users_with_tokens):
                                    if u['uid'] == uid:
                                        users_with_tokens[i]['token'] = new_token
                                        users_with_tokens[i]['token_bytes'] = token_bytes
                                        logging.info(f"【已更新】uid={uid} 的 token 已更新到工作列表")
                                        
                                        # 重置失败计数器
                                        if uid in token_states:
                                            token_states[uid]['fail_count'] = 0
                                            token_states[uid]['invalid_count'] = 0
                                        break
                            except Exception as e:
                                logging.error(f"更新 uid={uid} token 时出错: {e}")
                        
                        if refreshed_tokens:
                            logging.info(f"【实时刷新完成】成功刷新 {len(refreshed_tokens)}/{len(uids_to_refresh)} 个用户的 token")
                    except Exception:
                        logging.exception("实时刷新 token 时出错")
                
                # 7. 任务分配逻辑 (核心重构 - 性能优化版)
                perf_stats['loops'] += 1
                
                # 性能诊断：定期输出统计信息（info级别，所有模式可见）
                if now - last_perf_log >= 10:
                    if perf_stats['loops'] > 0:
                        avg_assigned = perf_stats['assigned'] / perf_stats['loops']
                        avg_scanned = perf_stats['scanned'] / perf_stats['loops']
                        no_ready_pct = (perf_stats['no_ready'] / perf_stats['loops']) * 100
                        # 计算当前就绪token数（移除busy检查）
                        ready_now = sum(1 for u in users_with_tokens 
                                      if now - token_states[u['uid']]['last_success'] >= user_cooldown_seconds)
                        # 计算利用率
                        util_pct = (stats['success'] / stats['sent'] * 100) if stats['sent'] > 0 else 0
                        perf_msg = f"就绪Token:{ready_now}/{len(users_with_tokens)} | 利用率:{util_pct:.1f}% | 每轮分配:{avg_assigned:.1f}任务 扫描:{avg_scanned:.0f}像素 | 已发送:{stats['sent']} 成功:{stats['success']} | pos_locks:{len(pos_locks)}"
                        logging.info(f"[性能] {perf_msg}")
                        log_last('INFO', f"[性能] {perf_msg}")
                    perf_stats = {'loops': 0, 'assigned': 0, 'scanned': 0, 'no_ready': 0}
                    last_perf_log = now
                
                # 7.1 筛选可用 Token
                # 条件：当前时间 - 上次成功时间 >= 冷却时间
                # 由于Token在发送后立即进入冷却，不再需要busy状态检查
                ready_tokens = []
                for u in users_with_tokens:
                    uid = u['uid']
                    state = token_states[uid]
                    if now - state['last_success'] >= user_cooldown_seconds:
                        ready_tokens.append(u)
                
                # 按上次成功时间排序（最久未使用的优先）
                ready_tokens.sort(key=lambda u: token_states[u['uid']]['last_success'])
                
                if not ready_tokens:
                    # 无可用 Token，极短等待后继续（不要阻塞太久）
                    perf_stats['no_ready'] += 1
                    await asyncio.sleep(0.001)
                    continue
                
                # 7.2 批量扫描并分配任务（一次性尽可能多分配）
                assigned_count = 0
                
                # 循环扫描逻辑：从上次结束的位置开始
                total_targets = len(target_positions)
                if total_targets > 0:
                    steps = 0
                    start_cursor = scan_cursor
                    # 智能扫描策略：
                    # - 如果ready_tokens很多（>50），扫描更多像素以充分利用
                    # - 如果ready_tokens较少，限制扫描范围避免浪费时间
                    # - 最多扫描完整一轮或ready_tokens数量的20倍（取较小值）
                    token_count = len(ready_tokens)
                    if token_count > 50:
                        # Token充足，允许扫描更多以填满所有token
                        max_steps = min(total_targets, token_count * 50)
                    else:
                        # Token较少，限制扫描范围
                        max_steps = min(total_targets, token_count * 20)
                    
                    while ready_tokens and steps < max_steps:
                        idx = (start_cursor + steps) % total_targets
                        pos = target_positions[idx]
                        steps += 1
                        
                        # 检查是否需要绘制
                        target_color = target_map.get(pos)
                        if not target_color:
                            continue
                            
                        current_color = board_state.get(pos)
                        if current_color == target_color:
                            continue
                        
                        # 检查是否已被锁定（最近刚绘制过）
                        lock_until = pos_locks.get(pos)
                        if lock_until and now < lock_until:
                            continue
                        # 锁已过期或不存在，可以绘制
                        
                        # 分配任务
                        user = ready_tokens.pop(0) # 取出最久未使用的 Token
                        uid = user['uid']
                        token_bytes = user.get('token_bytes')
                        uid_bytes3 = user.get('uid_bytes3')
                        r, g, b = target_color
                        
                        # 生成 Paint ID
                        paint_id = global_paint_id
                        global_paint_id = (global_paint_id + 1) % 4294967296
                        
                        # 记录任务（仅用于统计和0xff响应匹配）
                        task = {
                            'pos': pos,
                            'color': (r, g, b),
                            'uid': uid,
                            'time': now
                        }
                        active_tasks[paint_id] = task
                        
                        # 使用优化的 tool.paint 函数批量构建绘画数据
                        tool.paint(ws, uid, token_bytes, uid_bytes3, r, g, b, pos[0], pos[1], paint_id)
                        
                        # 【关键优化】发送后立即释放Token并进入冷却，不等待任何响应
                        # 这样可以最大化Token利用率，消除超时机制开销
                        token_states[uid]['last_success'] = now
                        
                        # 位置锁：短暂锁定避免重复提交（锁定时间=冷却时间）
                        pos_locks[pos] = now + user_cooldown_seconds
                        
                        stats['sent'] += 1
                        # 【修复】将发送计入成功，因为即使被覆盖也代表实际吞吐量
                        # 这样测量模式可以正确反映绘制速度，而不受对抗影响
                        stats['success'] += 1
                        assigned_count += 1
                    
                    # 记录扫描统计
                    perf_stats['scanned'] += steps
                    perf_stats['assigned'] += assigned_count
                    
                    # 更新游标位置，下次从停止的地方继续
                    scan_cursor = (start_cursor + steps) % total_targets
                    
                    # 如果分配了任务，唤醒发送器
                    if assigned_count > 0:
                        try:
                            paint_queue_event.set()
                        except Exception:
                            pass
                
                # 根据分配情况决定等待时间（优化：减少等待但避免CPU过载）
                if assigned_count > 0:
                    # 有分配任务，短暂yield给其他协程（避免霸占CPU）
                    await asyncio.sleep(0.0001)  # 0.1ms
                else:
                    # 没分配，可能所有目标都已达成或都在绘制中
                    # 等待时间取决于是否还有未完成的目标和ready_tokens数量
                    if len(pos_locks) > 0:
                        # 有任务在执行中，等待任务超时或完成
                        # 使用冷却时间的一小部分作为等待时间
                        if user_cooldown_seconds < 0.1:
                            # 极短冷却：等待1ms
                            await asyncio.sleep(0.001)
                        else:
                            # 正常冷却：等待5ms
                            await asyncio.sleep(0.005)
                    else:
                        # 可能所有都完成了，等待更长时间
                        await asyncio.sleep(0.01)

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


async def run_forever(config, users_with_tokens, images_data, debug=False, gui_state=None, custom_handler=None, precomputed_target=None):
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
            
            if custom_handler:
                duration = await custom_handler(config, users_with_tokens, images_data)
            else:
                duration = await handle_websocket(config, users_with_tokens, images_data, debug, gui_state=gui_state, precomputed_target=precomputed_target)
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
    # 解析命令行参数（支持 -debug、-cli、-test 和端口设置）
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('-debug', action='store_true', help='启用详细日志（DEBUG）并显示完整日志）')
    parser.add_argument('-cli', action='store_true', help='仅命令行模式，禁用 WebUI')
    parser.add_argument('-hand', action='store_true', help='启用手动绘板模式')
    parser.add_argument('-test', action='store_true', help='启用 Token 测量模式')
    parser.add_argument('-port', type=int, default=80, help='WebUI 端口（默认 80）')
    args, _ = parser.parse_known_args()
    
    # 启动主逻辑
    main(args)

def main(args):
    """主函数"""
    debug = bool(args.debug)
    cli_only = bool(args.cli)
    test_mode = bool(args.test)

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
        # 过滤掉已标记为失效的用户
        active_users = [u for u in users_list if not u.get('invalid')]
        
        # 根据 max_enabled_tokens 限制用户数量
        max_tokens = config.get('max_enabled_tokens', 0)
        if max_tokens > 0 and len(active_users) > max_tokens:
            logging.info(f"配置限制最大启用token数为 {max_tokens}，当前有 {len(active_users)} 个用户，仅加载前 {max_tokens} 个")
            active_users = active_users[:max_tokens]
        
        total = len(active_users)
        
        # 使用 rich 渲染 CLI 进度条
        root = None
        # 并发获取 token：对带 access_key 的用户并行调用 get_token，
        # 对不带 access_key 的用户直接取配置中 token（回退）
        if total == 0:
            return []

        max_workers = min(32, max(1, total))
        idx = 0
        config_changed = False

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            future_to_user = {}
            for user in active_users:
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
                            logging.warning(f"无法通过 access_key 获取 token: uid={uid}，该用户将被标记为失效。")
                            user['invalid'] = True
                            config_changed = True
                        else:
                            logging.warning(f"用户条目缺少 access_key 且未提供 token: uid={uid}，跳过。")
                    p.advance(task)

        # 换行完成进度输出
        print('')
        
        if config_changed:
            try:
                with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                    json.dump(config, f, indent=4, ensure_ascii=False)
                logging.info("配置已更新（标记了失效用户）。")
            except Exception as e:
                logging.error(f"保存配置失败: {e}")

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

    # 测试模式：在验证 token 后立即启动
    if test_mode:
        try:
            import test_token
            if not users_with_tokens:
                logging.error("没有可用的 token，无法进行测试。")
                print("\n❌ 错误：没有可用的用户 Token")
                return
            # 运行测试
            asyncio.run(test_token.main_test(config, users_with_tokens))
            return
        except ImportError:
            logging.error("找不到 test_token.py 插件，无法启动测试模式。")
            print("\n❌ 错误：找不到 test_token.py 插件")
            print("请确保 test_token.py 文件存在于程序目录中。")
            return
        except Exception as e:
            logging.exception(f"测试模式出错: {e}")
            print(f"\n❌ 测试模式运行失败: {e}")
            return

    # 视频功能已废弃
    try:
        pass
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

        # 视频功能已废弃，直接预计算目标像素映射
        precomputed_target = None
        logging.info("正在预计算目标像素映射...")
        try:
            precomputed_target = tool.merge_target_maps(images_data)
            logging.info("预计算完成。")
        except Exception:
            logging.exception("预计算目标像素映射失败")

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
                    p = multiprocessing.Process(target=process_worker, args=(i, config, grp, images_data, debug, precomputed_target), daemon=False)
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
            if args.hand:
                import hand_paint
                asyncio.run(run_forever(config, users_with_tokens, images_data, debug, custom_handler=hand_paint.run_hand_paint))
            else:
                asyncio.run(run_forever(config, users_with_tokens, images_data, debug, precomputed_target=precomputed_target))
        else:
            # 如果是手动模式，强制单线程运行
            if args.hand:
                logging.info("手动模式下强制使用单线程。")
                import hand_paint
                asyncio.run(run_forever(config, users_with_tokens, images_data, debug, custom_handler=hand_paint.run_hand_paint))
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

                def _worker_thread(idx, cfg, users_sub, images_sub, dbg, precomputed_target=None):
                    """每个线程创建独立的 asyncio loop 并运行 run_forever"""
                    try:
                        loop = _asyncio.new_event_loop()
                        _asyncio.set_event_loop(loop)
                        loop.run_until_complete(run_forever(cfg, users_sub, images_sub, dbg, precomputed_target=precomputed_target))
                    except Exception:
                        import logging as _logging
                        _logging.exception(f"线程 worker #{idx} 出现未处理异常")
                    finally:
                        try:
                            loop.close()
                        except Exception:
                            pass

                for i, grp in enumerate(groups):
                    t = _threading.Thread(target=_worker_thread, args=(i, config, grp, images_data, debug, precomputed_target), daemon=False, name=f"WSWorker-{i}")
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
