import logging
import os
import requests
import json
import time
import random
from uuid import UUID
from PIL import Image
import asyncio
import websockets

# --- 绘画相关工具与队列（从 main.py 拆分） ---
# 全局粘包队列（供 send_paint_data 与 paint 使用）
paint_queue = []
total_size = 0

def append_to_queue(paint_data):
    """将绘画数据添加到粘包队列"""
    global paint_queue, total_size
    paint_queue.append(paint_data)
    total_size += len(paint_data)

def get_merged_data():
    """合并队列中的所有数据块并清空队列"""
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
    """准备绘画数据并加入队列（非阻塞）"""
    try:
        try:
            token_bytes = UUID(token).bytes
        except Exception:
            try:
                token_bytes = UUID(hex=token.replace('-', '')).bytes
            except Exception as e:
                logging.error(f"无效的 Token 格式: {token}，创建 UUID 失败: {e}")
                return
        paint_data = bytearray(31)
        paint_data[0] = 0xfe
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
    """定时发送粘合后的绘画数据包（后台任务）
    
    增强稳定性：
    - 更健壮的连接状态检查
    - 更完善的异常处理和数据保护
    - 连续错误时自动退出以便重连
    - 捕获所有可能的 WebSocket 异常
    """
    consecutive_errors = 0
    max_consecutive_errors = 5
    
    try:
        while True:
            await asyncio.sleep(interval_ms / 1000.0)
            
            # 更健壮的连接状态检查
            try:
                is_open = getattr(ws, "open", None)
                if is_open is None:
                    is_open = not getattr(ws, "closed", False)
            except Exception:
                is_open = False
                
            if not is_open:
                # 连接已关闭，保留队列数据并退出
                logging.warning("检测到连接已关闭，发送任务退出以便重连。")
                break
                
            if paint_queue:
                merged_data = get_merged_data()
                if merged_data:
                    try:
                        await ws.send(merged_data)
                        logging.debug(f"已发送 {len(merged_data)} 字节的绘画数据（粘包）。")
                        consecutive_errors = 0  # 成功后重置错误计数
                    except (websockets.exceptions.ConnectionClosed, 
                            websockets.exceptions.ConnectionClosedError, 
                            websockets.exceptions.ConnectionClosedOK) as e:
                        # 连接已关闭，将数据重新入队并退出，交由上层重连逻辑处理
                        err_msg = str(e) if str(e) else e.__class__.__name__
                        logging.warning(f"发送数据时连接已关闭: {err_msg}; 重新入队数据并退出发送任务。")
                        try:
                            append_to_queue(merged_data)
                        except Exception as re_queue_err:
                            # 如果 append 失败，则放到全局 paint_queue 保底
                            logging.debug(f"重新入队时出错: {re_queue_err}，尝试直接插入队列")
                            try:
                                paint_queue.insert(0, merged_data)
                            except Exception as insert_err:
                                logging.warning(f"无法保存待发送数据: {insert_err}")
                        break
                    except asyncio.TimeoutError as e:
                        # 发送超时，重新入队并计数
                        logging.warning(f"发送数据超时: {e}; 重新入队数据。")
                        consecutive_errors += 1
                        try:
                            append_to_queue(merged_data)
                        except Exception:
                            try:
                                paint_queue.insert(0, merged_data)
                            except Exception:
                                pass
                        await asyncio.sleep(1.0)
                    except asyncio.CancelledError:
                        # 任务被取消，重新入队数据并退出
                        logging.info("发送任务被取消，重新入队数据。")
                        try:
                            append_to_queue(merged_data)
                        except Exception:
                            try:
                                paint_queue.insert(0, merged_data)
                            except Exception:
                                pass
                        raise
                    except Exception as e:
                        # 其它异常：记录并将数据重入队
                        err_msg = str(e) if str(e) else e.__class__.__name__
                        err_type = e.__class__.__name__
                        logging.error(f"发送数据时出错 ({err_type}): {err_msg}")
                        consecutive_errors += 1
                        try:
                            append_to_queue(merged_data)
                        except Exception:
                            try:
                                paint_queue.insert(0, merged_data)
                            except Exception:
                                pass
                        # 避免快速循环重试造成 CPU 飙升
                        await asyncio.sleep(1.0)
                    
                    # 连续错误过多时退出，让上层重连
                    if consecutive_errors >= max_consecutive_errors:
                        logging.error(f"连续发送失败 {consecutive_errors} 次，发送任务退出以便重连。")
                        break
    except asyncio.CancelledError:
        # 任务被取消时正常退出
        logging.debug("发送任务被取消。")
        raise
    except Exception as e:
        # 捕获任何未预期的异常，避免任务静默失败
        err_msg = str(e) if str(e) else e.__class__.__name__
        logging.error(f"发送任务遇到未预期异常: {err_msg}，任务退出。")
    finally:
        logging.info("发送任务已退出。")


def build_target_map(pixels, width, height, start_x, start_y, config=None):
    """构建目标像素颜色映射：{(abs_x,abs_y): (r,g,b)}，跳过透明与越界。"""
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
                        skipped_transparent += 1
                        continue
                else:
                    r = g = b = int(p)
                    a = 255
            except Exception:
                skipped_transparent += 1
                continue
            try:
                if int(a) == 0:
                    skipped_transparent += 1
                    continue
            except Exception:
                pass
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


def fetch_board_snapshot(api_base_url="https://paintboard.luogu.me"):
    """通过 HTTP 接口获取当前画板所有像素的快照，返回 dict {(x,y):(r,g,b)}。

    带简易重试与禁用环境代理，以提升在临时断网/代理环境下的稳定性。
    """
    url = f"{api_base_url}/api/paintboard/getboard"
    session = requests.Session()
    try:
        try:
            session.trust_env = False
        except Exception:
            pass
        data = None
        delay = 1.0
        for attempt in range(4):
            try:
                resp = session.get(url, timeout=10)
                resp.raise_for_status()
                data = resp.content
                break
            except Exception as e:
                logging.warning(f"获取画板快照尝试 {attempt+1}/4 失败: {e}")
                time.sleep(delay)
                delay = min(delay * 2, 8)
        if data is None:
            return {}
        expected = 1000 * 600 * 3
        if len(data) < expected:
            logging.warning(f"获取画板快照数据长度不够: {len(data)} < {expected}")
        board = {}
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
    except Exception as e:
        logging.exception(f"解析画板快照时出现异常: {e}")
        return {}


def get_draw_order(mode: str, width: int, height: int):
    """根据模式返回绘制顺序坐标列表（相对坐标）。"""
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
    return coords


def load_image_pixels(config):
    """根据配置加载目标图片，返回 (pixels,width,height)。pixels 为 RGBA 四元组列表。
    
    兼容旧配置格式（单个 image_path）和新格式（images 列表）
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


def load_all_images(config):
    """加载所有启用的图片配置，返回图片信息列表。

    支持两类来源：
    1) 普通文件图片（image_path 存在且可读）
    2) 特殊攻击图片（type == 'attack'），按配置生成随机点阵

    返回格式：[
        {
            'pixels': [...],
            'width': int,
            'height': int,
            'start_x': int,
            'start_y': int,
            'draw_mode': str,
            'weight': float,
            'image_path': str (可选)
            'config_index': int
        },
        ...
    ]
    """
    images_config = config.get('images', [])

    # 如果没有 images 配置，尝试兼容旧格式
    if not images_config:
        image_path = config.get('image_path')
        if image_path:
            images_config = [{
                'image_path': image_path,
                'start_x': config.get('start_x', 0),
                'start_y': config.get('start_y', 0),
                'draw_mode': config.get('draw_mode', 'random'),
                'weight': 1.0,
                'enabled': True
            }]

    def _gen_attack_pixels(img_cfg):
        """根据攻击配置生成 RGBA 像素列表与尺寸。背景透明，点为实心 1px。

        支持 attack_kind: white | green | random
        可选字段：dot_count（默认按面积 2% 取整）
        """
        width = int(img_cfg.get('width', 0) or 0)
        height = int(img_cfg.get('height', 0) or 0)
        if width <= 0 or height <= 0:
            return None, 0, 0
        total = width * height
        dot_count = img_cfg.get('dot_count')
        try:
            dot_count = int(dot_count) if dot_count is not None else max(1, total // 50)  # ~2%
        except Exception:
            dot_count = max(1, total // 50)

        kind = (img_cfg.get('attack_kind') or img_cfg.get('attack') or 'white').lower()
        rnd = random.Random(width * 1315423911 ^ height * 2654435761)

        pixels = [(0, 0, 0, 0)] * total
        used = set()
        for _ in range(dot_count):
            # 防止死循环，尝试有限次
            tries = 0
            while tries < 5:
                x = rnd.randrange(0, width)
                y = rnd.randrange(0, height)
                idx = y * width + x
                if idx not in used:
                    used.add(idx)
                    break
                tries += 1
            if not used:
                continue
            if kind == 'white':
                color = (255, 255, 255)
            elif kind == 'green':
                color = (0, 255, 0)
            elif kind == 'random':
                color = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
            else:
                color = (255, 255, 255)
            try:
                pixels[idx] = (color[0], color[1], color[2], 255)
            except Exception:
                pass
        return pixels, width, height

    loaded_images = []
    for cfg_idx, img_config in enumerate(images_config):
        if not img_config.get('enabled', True):
            continue

        # 分支：特殊攻击图片
        if str(img_config.get('type', '')).lower() == 'attack':
            try:
                pixels, width, height = _gen_attack_pixels(img_config)
                if pixels and width > 0 and height > 0:
                    loaded_images.append({
                        'pixels': pixels,
                        'width': width,
                        'height': height,
                        'start_x': int(img_config.get('start_x', 0)),
                        'start_y': int(img_config.get('start_y', 0)),
                        'draw_mode': img_config.get('draw_mode', 'random'),
                        'weight': float(img_config.get('weight', 1.0)),
                        'config_index': int(cfg_idx),
                        'attack_kind': img_config.get('attack_kind', 'white')
                    })
                else:
                    logging.warning(f"跳过无效的攻击图片配置（尺寸/像素为空）: index={cfg_idx}")
            except Exception:
                logging.exception(f"生成攻击图片失败: index={cfg_idx}")
            continue

        # 分支：普通文件图片
        image_path = img_config.get('image_path')
        if not image_path or not os.path.exists(image_path):
            logging.warning(f"跳过不存在的图片: {image_path}")
            continue
        try:
            img = Image.open(image_path).convert('RGBA')
            width, height = img.size
            pixels = list(img.getdata())

            loaded_images.append({
                'pixels': pixels,
                'width': width,
                'height': height,
                'start_x': int(img_config.get('start_x', 0)),
                'start_y': int(img_config.get('start_y', 0)),
                'draw_mode': img_config.get('draw_mode', 'random'),
                'weight': float(img_config.get('weight', 1.0)),
                'image_path': image_path,
                'config_index': int(cfg_idx)
            })
            logging.debug(f"已加载图片: {image_path} 大小: {width}x{height} 权重: {img_config.get('weight', 1.0)}")
        except Exception:
            logging.exception(f"加载图片失败: {image_path}")

    return loaded_images


def merge_target_maps(images_data):
    """合并多个图片的目标映射，处理重叠像素（按权重优先级）。
    
    返回：
    - combined_target_map: {(x,y): (r,g,b)} 合并后的目标像素映射
    - target_positions_by_mode: {draw_mode: [(x,y), ...]} 按绘制模式分组的坐标列表
    """
    # 首先按权重排序（权重高的优先），同时保留其在配置中的索引 config_index，
    # 以便 GUI 使用相同索引统计派发。
    indexed = []
    for i, img in enumerate(images_data):
        cfg_idx = img.get('config_index', i)
        indexed.append((i, cfg_idx, img))
    sorted_images = sorted(indexed, key=lambda it: it[2].get('weight', 1.0), reverse=True)

    combined_target_map = {}
    positions_by_mode = {}
    # 映射每个绝对坐标到最终被采纳的图片索引（images_data 中的索引）
    pos_to_image_idx = {}

    for orig_idx, cfg_idx, img_data in sorted_images:
        pixels = img_data['pixels']
        width = img_data['width']
        height = img_data['height']
        start_x = img_data['start_x']
        start_y = img_data['start_y']
        draw_mode = img_data['draw_mode']
        
        # 构建该图片的目标映射
        target_map = build_target_map(pixels, width, height, start_x, start_y)
        
        # 生成绘制顺序
        ordered_coords = get_draw_order(draw_mode, width, height)
        
        # 转换为绝对坐标并记录
        if draw_mode not in positions_by_mode:
            positions_by_mode[draw_mode] = []
            
        for x, y in ordered_coords:
            abs_pos = (start_x + x, start_y + y)
            if abs_pos in target_map:
                # 只有当该位置还未被更高权重的图片占用时才添加
                if abs_pos not in combined_target_map:
                    combined_target_map[abs_pos] = target_map[abs_pos]
                    positions_by_mode[draw_mode].append(abs_pos)
                    # 使用配置索引，保证与 GUI 列表一致
                    pos_to_image_idx[abs_pos] = cfg_idx
    
    return combined_target_map, positions_by_mode, pos_to_image_idx
