"""序列帧播放（CRT 隔行扫描绘制）

用法（示例）:
    python seq_player.py --folder ./frames --startx 100 --starty 50 --fps 12 --loop

说明:
 - 序列帧目录应包含按数字命名的图片文件: 1.png, 2.png, ... n.png
 - 脚本会逐帧读取图片并将像素通过 tool.paint() 加入绘画队列
 - CRT 隔行扫描: 每帧分两场(field)：先绘制与 (starty) 同奇偶性的行，再绘制另一半行；两场之间有短延时以模拟隔行扫描
 - 视频绘制优先级由本脚本立即将绘画数据加入队列实现（若存在并发绘制逻辑，请确保 send_paint_data 正常运行）

该脚本尽量保持轻量：仅依赖 Pillow（项目已有）。
"""

from PIL import Image
import os
import time
import argparse
import logging
import re
import random
import tool

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')


def _numeric_sort_key(name: str):
    # 提取文件名中的第一个数字用于排序（1.png, 2.png 等）
    m = re.search(r"(\d+)", name)
    return int(m.group(1)) if m else float('inf')


def list_frame_files(folder: str):
    files = []
    for fn in os.listdir(folder):
        lower = fn.lower()
        if lower.endswith('.png') or lower.endswith('.jpg') or lower.endswith('.jpeg'):
            files.append(fn)
    files.sort(key=_numeric_sort_key)
    return [os.path.join(folder, f) for f in files]


def play_sequence(folder: str,
                  startx: int = 0,
                  starty: int = 0,
                  fps: float = 12.0,
                  loop: bool = True,
                  enabled: bool = True,
                  uid: int = 1000,
                  token_bytes: bytes = None,
                  uid_bytes3: bytes = None,
                  ignore_alpha: bool = True,
                  max_frames: int = None,
                  max_pixels_per_second: int = None):
    """播放序列帧到绘画队列（使用 tool.paint）。

    参数:
      folder: 帧文件夹路径
      startx,starty: 目标画布左上角偏移
      fps: 目标帧率（以整帧为单位），隔行两场之间使用 half-frame 延迟
      loop: 是否循环播放
      enabled: 若 False 则直接返回
      uid, token_bytes, uid_bytes3: 传递给 tool.paint 的标识和 token（可选）
      ignore_alpha: True 跳过 alpha==0 像素
      max_frames: 若指定则最多播放此数量的帧（用于测试）
    """

    if not enabled:
        logging.info("序列帧播放被禁用 (enabled=False)")
        return

    if not os.path.isdir(folder):
        raise ValueError(f"找不到文件夹: {folder}")

    files = list_frame_files(folder)
    if not files:
        raise ValueError(f"文件夹中没有找到图片文件: {folder}")

    if token_bytes is None:
        # 随机 16 字节 token 以兼容 tool.paint
        token_bytes = bytes(random.getrandbits(8) for _ in range(16))
    if uid_bytes3 is None:
        uid_bytes3 = (uid & 0xFFFFFF).to_bytes(3, 'little')

    frame_interval = 1.0 / max(0.0001, float(fps))
    half_field_delay = frame_interval / 2.0

    # 节流参数：如果未指定，则使用一个保守默认（500 pps）以避免瞬间入队过多导致发送线程压力
    if max_pixels_per_second is None:
        max_pixels_per_second = 500

    # 用于限速统计
    pixels_sent_in_window = 0
    window_start = time.monotonic()
    window_len = 1.0

    paint_id = int(time.time()) & 0xFFFFFFFF

    logging.info(f"开始播放序列帧: {folder} 帧数={len(files)} start=({startx},{starty}) fps={fps} loop={loop}")

    played = 0
    try:
        while True:
            for idx, fpath in enumerate(files):
                if max_frames is not None and played >= max_frames:
                    logging.info("达到 max_frames，停止播放")
                    return

                try:
                    img = Image.open(fpath).convert('RGBA')
                except Exception as e:
                    logging.warning(f"无法打开帧 {fpath}: {e}")
                    continue

                w, h = img.size
                pixels = list(img.getdata())

                # 两场：先绘制 (starty + y) % 2 == parity 的行，再绘制另一半
                for field in (0, 1):
                    # 将该场的像素逐个入队
                    for y in range(h):
                        abs_y = starty + y
                        if (abs_y & 1) != field:
                            continue
                        row_base = y * w
                        for x in range(w):
                            px = pixels[row_base + x]
                            # px 是 (r,g,b,a)
                            a = px[3]
                            if ignore_alpha and a == 0:
                                continue
                            r, g, b = px[0], px[1], px[2]
                            abs_x = startx + x
                            # 画布越界检查（与 tool.build_target_map 相同的范围）
                            if not (0 <= abs_x < 1000 and 0 <= abs_y < 600):
                                continue
                            # 限速：每秒最多允许一定数量像素入队
                            now = time.monotonic()
                            # 滑动窗口计数
                            if now - window_start >= window_len:
                                window_start = now
                                pixels_sent_in_window = 0

                            if pixels_sent_in_window >= max_pixels_per_second:
                                # 已达上限，短暂等待到窗口刷新
                                sleep_for = max(0.0, window_len - (now - window_start))
                                logging.debug(f"视频限速：已达每秒 {max_pixels_per_second} 像素上限，睡眠 {sleep_for:.3f}s")
                                time.sleep(sleep_for)
                                # 刷新窗口
                                window_start = time.monotonic()
                                pixels_sent_in_window = 0

                            # 调用 tool.paint 将绘制命令加入队列
                            tool.paint(None, uid, token_bytes, uid_bytes3, int(r), int(g), int(b), int(abs_x), int(abs_y), paint_id)
                            paint_id = (paint_id + 1) & 0xFFFFFFFF
                            pixels_sent_in_window += 1

                    # 在两场之间短暂停顿以模拟隔行扫描（允许发送任务将数据发布到服务器）
                    time.sleep(half_field_delay)

                # 全帧绘制完后，维持剩余时间以达到帧间隔（场内已有 half_field_delay*2 = frame_interval，总体节奏基本由 field 延时控制）
                # 计数并继续到下一帧
                played += 1

            if not loop:
                logging.info("播放完成（loop=False）")
                break

    except KeyboardInterrupt:
        logging.info("播放被中断 (KeyboardInterrupt)")


def _parse_args():
    p = argparse.ArgumentParser(description='序列帧播放（CRT 隔行扫描）')
    p.add_argument('--folder', '-f', required=True, help='序列帧文件夹路径（按数字命名: 1.png,2.png,...）')
    p.add_argument('--startx', type=int, default=0, help='绘制左上角 X 坐标')
    p.add_argument('--starty', type=int, default=0, help='绘制左上角 Y 坐标')
    p.add_argument('--fps', type=float, default=12.0, help='目标帧率（整帧/秒）')
    p.add_argument('--no-loop', dest='loop', action='store_false', help='只播放一次后退出')
    p.add_argument('--enabled', dest='enabled', action='store_true', help='启用播放（默认启用）')
    p.add_argument('--uid', type=int, default=1000, help='用于 paint 的 uid')
    p.add_argument('--max-frames', type=int, default=None, help='可选：最多播放的帧数（用于测试）')
    return p.parse_args()


if __name__ == '__main__':
    args = _parse_args()
    # enabled 参数默认 False unless passed, 但脚本希望默认启用
    enabled = True if args.enabled or True else False
    try:
        play_sequence(args.folder,
                      startx=args.startx,
                      starty=args.starty,
                      fps=args.fps,
                      loop=args.loop,
                      enabled=enabled,
                      uid=args.uid,
                      max_frames=args.max_frames)
    except Exception as e:
        logging.exception(f"播放序列帧时出错: {e}")
