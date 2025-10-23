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

# --- 全局配置 ---
API_BASE_URL = "https://paintboard.luogu.me"
WS_URL = "wss://paintboard.luogu.me/api/paintboard/ws"
CONFIG_FILE = "config.json"

# --- 日志配置 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("paint.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# --- 绘画队列 ---
paint_queue = []
total_size = 0

def load_config():
    """从 config.json 加载配置"""
    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
        logging.getLogger().setLevel(config.get("log_level", "INFO").upper())
        logging.info("配置加载成功。")
        return config
    except FileNotFoundError:
        logging.error(f"错误：找不到配置文件 {CONFIG_FILE}。")
        return None
    except json.JSONDecodeError:
        logging.error(f"错误：配置文件 {CONFIG_FILE} 格式无效。")
        return None

def get_token(uid, access_key):
    """使用 UID 和 Access Key 获取 Token。

    兼容多种响应格式，并禁用环境代理（避免受 HTTP(S)_PROXY 干扰）。
    在失败时记录响应内容以便调试。
    """
    url = f"{API_BASE_URL}/api/auth/gettoken"
    payload = {"uid": uid, "access_key": access_key}
    try:
        # 使用 Session 并禁用环境代理（trust_env=False），避免受系统代理影响
        session = requests.Session()
        session.trust_env = False
        response = session.post(url, json=payload, timeout=10)
        response.raise_for_status()

        try:
            data = response.json()
        except ValueError:
            logging.error(f"获取 UID {uid} 的 Token 失败：响应不是 JSON，status={response.status_code}")
            logging.debug(f"响应文本: {response.text}")
            return None

        # 支持多种可能的响应结构：
        # 1) { "token": "..." }
        # 2) { "data": { "token": "..." } }
        # 3) { "status": 200, "data": { "token": "..." } }
        if isinstance(data, dict):
            if data.get("token"):
                logging.info(f"成功获取 UID {uid} 的 Token。")
                return data["token"]
            if isinstance(data.get("data"), dict) and data["data"].get("token"):
                logging.info(f"成功获取 UID {uid} 的 Token。")
                return data["data"]["token"]
            if data.get("status") == 200 and isinstance(data.get("data"), dict) and data["data"].get("token"):
                logging.info(f"成功获取 UID {uid} 的 Token。")
                return data["data"]["token"]

            # 无 token，尝试读取 errorType
            error_type = data.get("errorType") or (data.get("data") or {}).get("errorType")
            logging.error(f"获取 UID {uid} 的 Token 失败: {error_type or '未知错误'}")
            logging.debug(f"Token 接口返回内容: {data}")
            return None
        else:
            logging.error(f"获取 UID {uid} 的 Token 失败：响应格式未知（{type(data)}）。")
            logging.debug(f"Token 接口返回内容: {data}")
            return None
    except requests.RequestException as e:
        logging.error(f"请求 Token 时发生网络错误: {e}")
        return None

def load_image_pixels(config):
    """加载图像并返回像素数据"""
    image_path = config.get("image_path")
    if not image_path:
        logging.error("配置文件中未指定 image_path。")
        return None, 0, 0
    
    try:
        # 使用 RGBA 加载以便检测透明通道
        with Image.open(image_path) as img:
            img = img.convert("RGBA")
            width, height = img.size
            raw_pixels = list(img.getdata())
            # raw_pixels 中每个元素为 (r, g, b, a)
            logging.info(f"成功加载图像 '{image_path}'，尺寸: {width}x{height}。")
            return raw_pixels, width, height
    except FileNotFoundError:
        logging.error(f"找不到图像文件: {image_path}")
        return None, 0, 0
    except Exception as e:
        logging.error(f"加载图像时出错: {e}")
        return None, 0, 0

def get_draw_order(mode, width, height, board_width=1000, board_height=600):
    """根据模式生成绘画坐标序列"""
    coords = []
    if mode == "horizontal":
        for y in range(height):
            for x in range(width):
                coords.append((x, y))
    elif mode == "concentric":
        left, top, right, bottom = 0, 0, width - 1, height - 1
        while left <= right and top <= bottom:
            for x in range(left, right + 1): coords.append((x, top))
            top += 1
            if top > bottom: break
            for y in range(top, bottom + 1): coords.append((right, y))
            right -= 1
            if left > right: break
            for x in range(right, left - 1, -1): coords.append((x, bottom))
            bottom -= 1
            if top > bottom: break
            for y in range(bottom, top - 1, -1): coords.append((left, y))
            left += 1
    else: # random
        coords = [(x, y) for y in range(height) for x in range(width)]
        random.shuffle(coords)
    
    logging.info(f"使用 '{mode}' 模式生成 {len(coords)} 个绘画坐标。")
    return coords

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

def build_target_map(pixels, width, height, start_x, start_y):
    """构建目标像素颜色映射：{(abs_x,abs_y): (r,g,b)}，跳过透明与越界"""
    target = {}
    for py in range(height):
        for px in range(width):
            idx = py * width + px
            try:
                r, g, b, a = pixels[idx]
            except Exception:
                continue
            if a == 0:
                continue
            abs_x, abs_y = start_x + px, start_y + py
            if 0 <= abs_x < 1000 and 0 <= abs_y < 600:
                target[(abs_x, abs_y)] = (r, g, b)
    logging.info(f"目标像素数: {len(target)}（非透明且在画布范围内）")
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


async def handle_websocket(config, users_with_tokens, pixels, width, height, debug=False):
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
    target_positions = list(target_map.keys())
    if draw_mode == "random":
        random.shuffle(target_positions)

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
                    # 只保留与目标区域相关的坐标，避免存储整个 1000x600
                    for pos in target_positions:
                        if pos in snapshot:
                            board_state[pos] = snapshot[pos]
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
                                    # 若更新触及目标区域，唤醒调度器以便即时修复
                                    if (x, y) in target_map:
                                        state_changed_event.set()
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
                total = len(target_positions)
                while True:
                    # 计算完成度（不符合的像素数会降低完成度）
                    mismatched = len([pos for pos in target_positions if board_state.get(pos) != target_map[pos]])
                    completed = max(0, total - mismatched)
                    pct = (completed / total * 100) if total > 0 else 100.0
                    # 可用用户数
                    now = time.monotonic()
                    available = len([u for u in users_with_tokens if cooldown_until.get(u['uid'], 0.0) <= now])
                    bar_len = 40
                    filled = int(bar_len * completed / total) if total > 0 else bar_len
                    bar = '#' * filled + '-' * (bar_len - filled)
                    line = f"进度: [{bar}] {pct:6.2f}% 可用用户: {available}  未达标: {mismatched}"
                    if debug:
                        logging.info(line)
                    else:
                        # 非 debug 模式：只在控制台打印进度条（刷新）
                        sys.stdout.write('\r' + line)
                        sys.stdout.flush()
                    await asyncio.sleep(1)

            # 调度：支持冷却与持续监视
            user_counters = {u['uid']: 0 for u in users_with_tokens}
            cooldown_until = {u['uid']: 0.0 for u in users_with_tokens}  # monotonic 时间戳
            in_watch_mode = False
            round_idx = 0

            # 现在 cooldown_until 已初始化，再启动进度显示器
            progress_task = asyncio.create_task(progress_printer())

            while True:
                now = time.monotonic()
                # 未达目标色（未知状态也视为未完成）
                remaining = [pos for pos in target_positions if board_state.get(pos) != target_map[pos]]

                # 可用用户（不在冷却期）
                available_users = [u for u in users_with_tokens if cooldown_until.get(u['uid'], 0.0) <= now]

                assigned = 0
                if remaining and available_users:
                    random.shuffle(remaining)
                    # 立即为可用用户分配修复任务（每人 1 个）
                    for user in available_users:
                        if not remaining:
                            break
                        x, y = remaining.pop()
                        r, g, b = target_map[(x, y)]
                        uid = user['uid']
                        token = user['token']
                        paint_id = user_counters[uid]
                        await paint(ws, uid, token, r, g, b, x, y, paint_id)
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
    # 解析命令行参数（支持 -debug）
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('-debug', action='store_true', help='启用详细日志（DEBUG）并显示完整日志）')
    args, _ = parser.parse_known_args()
    debug = bool(args.debug)

    config = load_config()
    if not config:
        return

    pixels, width, height = load_image_pixels(config)
    if not pixels:
        return

    users_with_tokens = []
    for user in config.get("users", []):
        token = get_token(user["uid"], user["access_key"])
        if token:
            users_with_tokens.append({"uid": user["uid"], "token": token})
        else:
            logging.warning(f"无法为 UID {user['uid']} 获取 Token，将跳过此用户。")
    
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
        asyncio.run(handle_websocket(config, users_with_tokens, pixels, width, height, debug))
    except KeyboardInterrupt:
        logging.info("检测到手动中断，程序退出。")
    except Exception:
        logging.exception("程序运行时发生未捕获的异常")

if __name__ == "__main__":
    main()
