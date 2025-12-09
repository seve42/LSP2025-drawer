"""
测试插件：测量指定区域的敌对势力 token 数量

用法：python main.py -test

测量原理（修正的稳态平衡模型）：
================================

一、基本模型
-----------
设重叠区域面积为 A 像素，双方在此区域对抗。

定义：
- N_m = 我方 token 数
- N_e = 对方 token 数
- η_m = 我方效率（实际有效绘制 / 理论最大），从测试中直接测得
- η_e = 对方效率（需要估算）
- p = 稳态时我方占据率（0~1）
- CD = 冷却时间（秒）

有效绘制速率：
- 我方有效速率 R_m = N_m * η_m / CD (px/s)
- 对方有效速率 R_e = N_e * η_e / CD (px/s)

二、稳态平衡条件
---------------
稳态时，我方覆盖对方像素的速率 = 对方覆盖我方像素的速率：

  R_m * (1-p) = R_e * p
  
  N_m * η_m * (1-p) = N_e * η_e * p
  
  => N_e = N_m * (η_m / η_e) * (1-p) / p

三、效率估算
-----------
我方效率 η_m 可直接从测试数据计算：
  η_m = 实际速度 / 理论速度 = (成功像素数/时间) / (token数/CD)

对方效率 η_e 的估算策略：
1. 智能修复策略（只修错误像素）：η_e ≈ η_m（与我方相当）
2. 全图扫描策略：η_e ≈ η_m * (重叠面积 / 对方图片面积)
3. 保守估计：假设对方效率是我方的 50%~150%

四、多次测量法（推荐）
--------------------
使用不同数量的 token 进行多次测量，建立线性回归模型：

设第 i 次测量：
  - 投入 N_i 个 token
  - 测得占据率 p_i
  - 测得效率 η_i

稳态条件变形：
  N_i * η_i * (1 - p_i) / p_i = N_e * η_e  (常数)

令 X_i = N_i * η_i * (1 - p_i) / p_i

理想情况下，所有 X_i 应该相等，等于 N_e * η_e。
取平均值：N_e * η_e = mean(X_i)

如果假设 η_e ≈ η_m_avg，则：
  N_e ≈ mean(X_i) / η_m_avg

五、注意事项
-----------
1. 需要等待足够长时间达到稳态（建议 2~3 倍理论完成时间）
2. 多帧采样取平均以减少瞬时波动影响
3. 如果完成率变化趋势明显（未达稳态），结果不可靠
4. 建议使用多次不同 token 数测量来提高准确性
"""

import asyncio
import logging
import time
import requests
import os
from PIL import Image

# 配置常量
API_BASE_URL = "https://paintboard.luogu.me"
TEST_IMAGE_SIZE = (50, 50)

# 禁用详细日志
logging.getLogger().setLevel(logging.CRITICAL)
os.environ['PYTHONWARNINGS'] = 'ignore'


def select_contrast_colors(board, start_x, start_y, width=50, height=50):
    """从画板区域选择两种对比度明确的测试颜色
    
    返回：(light_color, dark_color) 两个 RGB 元组
    策略：选择在目标区域中未出现的浅色和深色
    """
    # 收集区域内已有的颜色
    used_colors = set()
    for y in range(height):
        for x in range(width):
            pos = (start_x + x, start_y + y)
            color = board.get(pos)
            if color:
                used_colors.add(color)
    
    # 候选浅色（亮度高）
    light_candidates = [
        (255, 255, 200),  # 浅黄
        (200, 255, 200),  # 浅绿
        (200, 200, 255),  # 浅蓝
        (255, 200, 255),  # 浅粉
        (200, 255, 255),  # 浅青
        (255, 230, 200),  # 浅橙
    ]
    
    # 候选深色（亮度低）
    dark_candidates = [
        (40, 40, 100),    # 深蓝
        (100, 40, 40),    # 深红
        (40, 100, 40),    # 深绿
        (100, 40, 100),   # 深紫
        (40, 100, 100),   # 深青
        (100, 100, 40),   # 深黄
    ]
    
    # 选择未使用的浅色
    light_color = None
    for c in light_candidates:
        if c not in used_colors:
            light_color = c
            break
    
    # 如果都被使用了，生成一个新的
    if light_color is None:
        import random
        while True:
            light_color = (random.randint(200, 255), random.randint(200, 255), random.randint(200, 255))
            if light_color not in used_colors:
                break
    
    # 选择未使用的深色
    dark_color = None
    for c in dark_candidates:
        if c not in used_colors:
            dark_color = c
            break
    
    # 如果都被使用了，生成一个新的
    if dark_color is None:
        import random
        while True:
            dark_color = (random.randint(30, 80), random.randint(30, 80), random.randint(30, 80))
            if dark_color not in used_colors:
                break
    
    return light_color, dark_color


def generate_test_image(test_png_path='test.png', used_png_path='used.png', 
                       light_color=None, dark_color=None):
    """从 test.png 生成 used.png
    
    策略：将灰度 < 50% 的像素用浅色，>= 50% 的用深色
    """
    try:
        # 读取原始测试图像
        img = Image.open(test_png_path).convert('RGBA')
        width, height = img.size
        
        # 如果尺寸不对，调整
        if (width, height) != TEST_IMAGE_SIZE:
            img = img.resize(TEST_IMAGE_SIZE)
            width, height = TEST_IMAGE_SIZE
        
        pixels = list(img.getdata())
        new_pixels = []
        
        # 转换每个像素
        for p in pixels:
            # 计算灰度（考虑 alpha 通道）
            if len(p) >= 4:
                r, g, b, a = p[0], p[1], p[2], p[3]
            elif len(p) == 3:
                r, g, b = p[0], p[1], p[2]
                a = 255
            else:
                r = g = b = p[0] if isinstance(p, int) else p
                a = 255
            
            # 透明像素保持透明
            if a == 0:
                new_pixels.append((0, 0, 0, 0))
                continue
            
            # 计算感知亮度（使用标准公式）
            brightness = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
            
            # 根据亮度选择颜色
            if brightness < 0.5:
                # 暗色像素 -> 使用浅色
                new_pixels.append(light_color + (255,))
            else:
                # 亮色像素 -> 使用深色
                new_pixels.append(dark_color + (255,))
        
        # 创建新图像
        new_img = Image.new('RGBA', (width, height))
        new_img.putdata(new_pixels)
        new_img.save(used_png_path)
        
        return True
    except Exception as e:
        logging.error(f"生成测试图像失败: {e}")
        return False


def get_user_input():
    """获取用户输入的测试参数"""
    print("\n准备开始测量")
    
    # 获取测试区域坐标
    while True:
        try:
            coord_input = input("请输入测量坐标 x y (用空格分隔): ").strip()
            parts = coord_input.split()
            if len(parts) != 2:
                print("格式错误")
                continue
            start_x, start_y = int(parts[0]), int(parts[1])
            if 0 <= start_x <= 950 and 0 <= start_y <= 550:
                break
            print("坐标超出范围")
        except ValueError:
            print("格式错误")
    
    # 获取使用token数
    while True:
        try:
            token_input = input("请输入使用 token 数: ").strip()
            num_tokens = int(token_input)
            if num_tokens > 0:
                break
            print("token 数必须大于 0")
        except ValueError:
            print("格式错误")
    
    return {
        'start_x': start_x,
        'start_y': start_y,
        'num_tokens': num_tokens
    }


def fetch_board_snapshot():
    """获取画板快照，返回 {(x,y): (r,g,b)} 映射
    
    注意：使用 proxies={} 参数禁用代理，避免修改全局环境变量影响并发的绘制任务
    """
    url = f"{API_BASE_URL}/api/paintboard/getboard"
    try:
        # 直接在请求中禁用代理，不修改全局环境变量
        resp = requests.get(url, timeout=10, proxies={})
        resp.raise_for_status()
        data = resp.content
        
        board = {}
        for y in range(600):
            for x in range(1000):
                offset = (y * 1000 + x) * 3
                if offset + 2 < len(data):
                    r, g, b = data[offset], data[offset+1], data[offset+2]
                    board[(x, y)] = (r, g, b)
        
        logging.debug("已获取画板快照")
        return board
    except Exception as e:
        logging.error(f"获取画板快照失败: {e}")
        return {}


async def fetch_board_snapshot_async():
    """异步获取画板快照，避免阻塞事件循环
    
    在后台线程中执行同步 HTTP 请求，不会阻塞绘制任务
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_board_snapshot)


def calculate_matching_pixels(board, target_map):
    """计算画板上有多少像素与目标一致"""
    matching = 0
    for pos, target_color in target_map.items():
        board_color = board.get(pos)
        if board_color == target_color:
            matching += 1
    return matching


def calculate_enemy_tokens(p_me, user_cd, num_my_tokens, enemy_area, overlap_area, my_efficiency=1.0):
    """根据稳态完成率计算对方token数（修正版）
    
    修正后的公式考虑效率因子：
    
    稳态平衡条件：
      N_m * η_m * (1 - p) = N_e * η_e * p
      
    其中：
      N_m = 我方 token 数
      η_m = 我方效率（从测试中测得）
      p = 我方稳态占据率
      N_e = 对方 token 数（待求）
      η_e = 对方效率（需假设）
    
    解得：
      N_e = N_m * η_m * (1 - p) / (η_e * p)
    
    Args:
        p_me: 我方稳态完成率 (0~1)
        user_cd: 用户冷却时间 (秒)
        num_my_tokens: 我方投入的 token 数量
        enemy_area: 对方图片总面积 (像素)
        overlap_area: 重叠区域面积 (像素)
        my_efficiency: 我方实测效率 (0~1)
    
    Returns:
        dict: {
            'effective_rate': 对方有效覆盖速率 (N_e * η_e),
            'n_same_efficiency': 假设对方效率与我方相同时的 token 数,
            'n_high_efficiency': 假设对方效率为100%时的 token 数,
            'n_low_efficiency': 假设对方效率为50%时的 token 数,
            'n_scan_strategy': 假设对方扫描全图策略时的 token 数,
        }
    """
    if p_me <= 0.01 or p_me >= 0.99:
        return None
    
    # 核心计算：对方的有效覆盖速率 (N_e * η_e)
    # 从稳态条件：N_m * η_m * (1 - p) = N_e * η_e * p
    # => N_e * η_e = N_m * η_m * (1 - p) / p
    effective_enemy_rate = num_my_tokens * my_efficiency * (1 - p_me) / p_me
    
    # 场景1: 假设对方效率与我方相同
    n_same = effective_enemy_rate / my_efficiency if my_efficiency > 0 else effective_enemy_rate
    
    # 场景2: 假设对方效率为 100%（理想情况）
    n_high = effective_enemy_rate / 1.0
    
    # 场景3: 假设对方效率为 50%（较差情况）
    n_low = effective_enemy_rate / 0.5
    
    # 场景4: 对方扫描全图策略
    # 对方只有一部分绘制落在重叠区，有效效率降低
    # η_e_effective = η_base * (overlap_area / enemy_area)
    area_ratio = overlap_area / enemy_area if enemy_area > 0 else 1.0
    effective_scan_efficiency = my_efficiency * area_ratio
    n_scan = effective_enemy_rate / effective_scan_efficiency if effective_scan_efficiency > 0 else float('inf')
    
    return {
        'effective_rate': effective_enemy_rate,
        'n_same_efficiency': n_same,
        'n_high_efficiency': n_high,
        'n_low_efficiency': n_low,
        'n_scan_strategy': n_scan,
    }


async def run_test_phase(config, users_with_tokens, test_image_config, show_progress=True):
    """运行测试阶段，持续绘制直到检测到稳态
    
    新策略：
    1. 持续绘制并快速采集帧（每 0.2*CD 一帧，适配不同 CD）
    2. 简化稳态判定：完成率历史最大值在 N 帧内未被刷新
       - N = max(10, int(30/CD)) 确保至少覆盖 30 秒或 10 帧
    3. 达到稳态后继续采集 5 帧用于平均计算
    4. 超时保护：根据 CD 自适应（最少 3 分钟，最多 10 分钟）
    
    返回:
        {
            'duration': 测试持续时间,
            'total_pixels': 目标像素总数,
            'completion_rate': 稳态完成率 (0~1),
            'actual_speed': 实际绘制速度 (px/s),
            'packets_sent': 发送的数据包数,
            'packets_success': 成功的数据包数,
            'frame_completions': 各帧完成率列表,
            'steady_state_reached': 是否达到稳态
        }
    """
    import tool
    from main import handle_websocket
    import threading
    
    # 临时禁用所有日志输出（除了 CRITICAL）
    old_log_level = logging.getLogger().level
    for handler in logging.getLogger().handlers:
        handler.setLevel(logging.CRITICAL)
    
    try:
        # 创建临时配置
        temp_config = config.copy()
        temp_config['images'] = test_image_config
        temp_config['auto_restart_minutes'] = 0  # 禁用自动重启
        
        # 从配置中获取CD时间和token数量
        user_cd = config.get('user_cooldown_seconds', 30.0)
        num_tokens = len(users_with_tokens)
        
        # 加载图像数据
        images_data = tool.load_all_images(temp_config)
        
        # 预计算目标映射
        precomputed = tool.merge_target_maps(images_data)
        target_map, positions_by_mode, pos_to_image_idx = precomputed
        total_pixels = len(target_map)
        
        # 稳态检测参数（自适应CD）
        # 帧间隔：小CD快速采样，大CD也保证合理间隔
        frame_interval = max(0.5, user_cd * 0.2)  # 0.2倍CD，最少0.5秒
        
        # 稳态判定窗口：确保至少覆盖30秒
        steady_window_time = 30.0  # 30秒内最大值未刷新
        steady_window_frames = max(10, int(steady_window_time / frame_interval))
        
        # 最小采集帧数：确保采集足够多的数据（至少60秒）
        min_frames_before_check = max(20, int(60.0 / frame_interval))
        
        # 稳态后继续采集的帧数
        steady_frames_needed = 10  # 增加到10帧以获得更稳定的平均值
        
        # 超时时间：根据CD自适应
        # CD小（如0.05s）：3分钟足够
        # CD大（如30s）：需要更长时间
        max_timeout = max(180, min(600, user_cd * 20))  # 3-10分钟
        
        # 创建 GUI 状态对象
        gui_state = {
            'stop': False,
            'lock': threading.Lock(),
            'stats': {
                'sent': 0,
                'success': 0,
                'failed': 0,
                'conflict': 0
            },
            'disable_main_progress': False
        }
        
        # 记录初始画板状态
        if show_progress:
            print("  📊 正在获取初始画板状态...")
        board_start = await fetch_board_snapshot_async()
        start_matching = calculate_matching_pixels(board_start, target_map)
        
        # 进度显示
        if show_progress:
            from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
            from rich.console import Console
            console = Console()
            progress = Progress(
                SpinnerColumn(),
                TextColumn("[bold blue]{task.description}"),
                TextColumn("[progress.percentage]{task.fields[info]}"),
                TimeElapsedColumn(),
                console=console,
                transient=False,
                refresh_per_second=1,
                auto_refresh=True
            )
            task_id = progress.add_task("[cyan]初始化...", info="")
            progress.start()
        
        # 运行绘图
        start_time = time.time()
        
        # 用于计算速度的历史记录
        from collections import deque
        pixels_history = deque()
        pixels_window_seconds = 10.0
        
        # 启动绘图任务
        paint_task = asyncio.create_task(
            handle_websocket(
                temp_config,
                users_with_tokens,
                images_data,
                debug=False,
                gui_state=gui_state,
                precomputed_target=precomputed
            )
        )
        
        # 采集帧数据
        frame_completions = []
        frame_timestamps = []
        frame_matching = []
        
        steady_state_reached = False
        steady_frame_count = 0  # 达到稳态后采集的帧数
        frame_idx = 0
        
        # 稳态检测：追踪历史最大值
        max_completion = 0.0
        max_completion_frame = 0  # 最大值出现的帧号
        
        # 持续采集直到稳态或超时
        while True:
            # 超时检查
            elapsed = time.time() - start_time
            if elapsed > max_timeout:
                if show_progress:
                    progress.update(task_id, description="[red]超时，停止测量", info="")
                break
            
            # 等待下一帧
            await asyncio.sleep(frame_interval)
            
            # 计算当前速度
            now = time.time()
            success_count = gui_state['stats']['success']
            pixels_history.append((now, success_count))
            while pixels_history and (now - pixels_history[0][0] > pixels_window_seconds):
                pixels_history.popleft()
            
            pixels_per_sec = 0.0
            if len(pixels_history) >= 2:
                t0, p0 = pixels_history[0]
                t1, p1 = pixels_history[-1]
                dt = max(1e-6, t1 - t0)
                pixels_per_sec = (p1 - p0) / dt
            
            # 获取当前画板状态
            frame_time = time.time()
            board_current = await fetch_board_snapshot_async()
            current_matching = calculate_matching_pixels(board_current, target_map)
            current_completion = (current_matching / total_pixels * 100) if total_pixels > 0 else 0
            
            frame_completions.append(current_completion)
            frame_timestamps.append(frame_time)
            frame_matching.append(current_matching)
            frame_idx += 1
            
            # 更新历史最大值
            if current_completion > max_completion:
                max_completion = current_completion
                max_completion_frame = frame_idx
            
            # 稳态检测：需要同时满足多个条件
            # 1. 采集足够多的帧（至少 min_frames_before_check 帧）
            # 2. 最大值在 steady_window_frames 帧内未刷新
            # 3. 波动幅度在合理范围内，或者无明显趋势
            frames_since_max = frame_idx - max_completion_frame
            
            # 计算最近帧的波动幅度
            volatility_window = min(steady_window_frames, len(frame_completions))
            recent_completions = frame_completions[-volatility_window:] if volatility_window > 0 else []
            
            volatility = 0.0
            volatility_ok = False
            has_trend = False
            
            if len(recent_completions) >= 5:
                avg_recent = sum(recent_completions) / len(recent_completions)
                if avg_recent > 1:  # 避免除以0
                    # 计算变异系数 (CV = std / mean)
                    variance = sum((x - avg_recent) ** 2 for x in recent_completions) / len(recent_completions)
                    std_dev = variance ** 0.5
                    volatility = std_dev / avg_recent
                    
                    # 放宽波动阈值：考虑对方可能采用周期性策略
                    # - 低完成率(<40%): 30% (允许更大波动)
                    # - 中完成率(40%~70%): 35%
                    # - 高完成率(>70%): 40%
                    if avg_recent < 40:
                        volatility_threshold = 0.30
                    elif avg_recent < 70:
                        volatility_threshold = 0.35
                    else:
                        volatility_threshold = 0.40
                    
                    volatility_ok = volatility < volatility_threshold
                    
                    # 检测趋势：使用线性回归判断是否有明显的上升/下降趋势
                    # 如果趋势系数的绝对值 > 0.5%/帧，认为有趋势
                    if len(recent_completions) >= 10:
                        n = len(recent_completions)
                        x_values = list(range(n))
                        x_mean = sum(x_values) / n
                        y_mean = avg_recent
                        
                        # 计算斜率
                        numerator = sum((x_values[i] - x_mean) * (recent_completions[i] - y_mean) for i in range(n))
                        denominator = sum((x_values[i] - x_mean) ** 2 for i in range(n))
                        
                        if denominator > 0:
                            slope = numerator / denominator
                            # 斜率的绝对值 > 0.5 认为有明显趋势
                            has_trend = abs(slope) > 0.5
            
            if not steady_state_reached:
                # 必须先采集足够多的帧
                if frame_idx < min_frames_before_check:
                    if show_progress:
                        progress.update(task_id,
                            description=f"[yellow]采集数据 (帧{frame_idx}/{min_frames_before_check})",
                            info=f"{current_completion:.1f}%")
                elif frames_since_max >= steady_window_frames and (volatility_ok or not has_trend):
                    # 达到稳态：最大值未刷新 + (波动小 或 无明显趋势)
                    steady_state_reached = True
                    reason = "波动小" if volatility_ok else "无趋势"
                    if show_progress:
                        progress.update(task_id, 
                            description=f"[green]✓ 达到稳态-{reason} (帧{frame_idx})",
                            info=f"{current_completion:.1f}% | 波动:{volatility*100:.1f}%")
                elif frames_since_max >= steady_window_frames and has_trend:
                    # 最大值未刷新但有明显趋势 - 可能在变化中
                    if show_progress:
                        progress.update(task_id,
                            description=f"[yellow]⚠️ 有趋势 (帧{frame_idx})",
                            info=f"{current_completion:.1f}% | 波动:{volatility*100:.1f}%")
                else:
                    # 还在等待稳态
                    if show_progress:
                        wait_info = f"{frames_since_max}/{steady_window_frames}"
                        vol_info = f" | 波动:{volatility*100:.1f}%" if volatility > 0 else ""
                        progress.update(task_id,
                            description=f"[yellow]等待稳态 (帧{frame_idx})",
                            info=f"{current_completion:.1f}% | 距最大值:{wait_info}帧{vol_info}")
            else:
                # 已达稳态，继续采集
                steady_frame_count += 1
                if show_progress:
                    progress.update(task_id,
                        description=f"[green]稳态采集 ({steady_frame_count}/{steady_frames_needed})",
                        info=f"{current_completion:.1f}%")
                
                if steady_frame_count >= steady_frames_needed:
                    break
        
        # 停止绘图
        gui_state['stop'] = True
        await asyncio.sleep(1)
        
        try:
            paint_task.cancel()
            await asyncio.sleep(0.5)
        except:
            pass
        
        if show_progress:
            progress.update(task_id, description="[green]✓ 测量完成", info="")
            await asyncio.sleep(0.5)
            progress.stop()
            print()
        
        actual_duration = time.time() - start_time
        
        # 等待服务器同步
        await asyncio.sleep(2)
        
        # 获取最终画板状态（使用异步版本）
        board_end = await fetch_board_snapshot_async()
        end_matching = calculate_matching_pixels(board_end, target_map)
        
        # 计算平均完成率：如果达到稳态，使用最后 steady_frames_needed 帧的平均值
        if steady_state_reached and len(frame_completions) >= steady_frames_needed:
            # 使用稳态帧的平均值
            avg_completion = sum(frame_completions[-steady_frames_needed:]) / steady_frames_needed
        elif frame_completions:
            # 使用最后 N 帧的平均值（排除初期不稳定阶段）
            stable_frames = min(30, len(frame_completions) // 2)  # 使用后半部分或最后30帧
            avg_completion = sum(frame_completions[-stable_frames:]) / stable_frames if stable_frames > 0 else sum(frame_completions) / len(frame_completions)
        else:
            avg_completion = (end_matching / total_pixels * 100) if total_pixels > 0 else 0
        
        # 计算实际绘制速度
        final_success_count = gui_state['stats'].get('success', 0)
        actual_speed = final_success_count / actual_duration if actual_duration > 0 else 0
        
        # 获取所有统计数据
        stats = gui_state.get('stats', {})
        
        # 计算最终波动率
        final_volatility = 0.0
        if len(frame_completions) >= 5:
            recent = frame_completions[-min(steady_window_frames, len(frame_completions)):]
            avg_recent = sum(recent) / len(recent)
            if avg_recent > 1:
                variance = sum((x - avg_recent) ** 2 for x in recent) / len(recent)
                final_volatility = (variance ** 0.5) / avg_recent
        
        return {
            'duration': actual_duration,
            'total_pixels': total_pixels,
            'start_matching': start_matching,
            'end_matching': end_matching,
            'completion_rate': avg_completion / 100.0,  # 转换为 0~1
            'actual_speed': actual_speed,
            'packets_sent': stats.get('sent', 0),
            'packets_success': final_success_count,
            'frame_completions': frame_completions,
            'frame_matching': frame_matching,
            'steady_state_reached': steady_state_reached,
            'total_frames': len(frame_completions),
            'volatility': final_volatility  # 波动率 (变异系数)
        }
    
    finally:
        # 恢复日志级别
        for handler in logging.getLogger().handlers:
            handler.setLevel(old_log_level)


async def main_test(config, users_with_tokens):
    """测试模式主入口 - 使用单次绘制计算敌对势力token数"""
    import tool
    from main import build_target_map
    
    # 获取用户输入
    test_params = get_user_input()
    
    start_x = test_params['start_x']
    start_y = test_params['start_y']
    num_tokens = test_params['num_tokens']
    
    # 计算面积（假设对方图片大小为100x100）
    overlap_area = TEST_IMAGE_SIZE[0] * TEST_IMAGE_SIZE[1]  # 50x50 = 2500
    enemy_area = 100 * 100  # 默认假设对方图片为100x100
    
    # 获取冷却时间
    user_cd = config.get('user_cooldown_seconds', 30.0)
    
    # 只使用指定数量的token
    users_with_tokens = users_with_tokens[:num_tokens]
    
    # 获取画板快照
    board = fetch_board_snapshot()
    if not board:
        print("无法获取画板快照")
        return
    
    # 选择对比色并生成测试图像
    light_color, dark_color = select_contrast_colors(board, start_x, start_y)
    
    test_png = 'test.png'
    used_png = 'used.png'
    
    if not os.path.exists(test_png):
        print("找不到 test.png")
        return
    
    if not generate_test_image(test_png, used_png, light_color, dark_color):
        print("生成测试图像失败")
        return
    
    # 加载生成的测试图像
    try:
        img = Image.open(used_png).convert('RGBA')
        pixels = list(img.getdata())
    except Exception as e:
        print(f"❌ 无法加载 {used_png}: {e}")
        return
    
    # 构建目标映射
    target_map = build_target_map(pixels, TEST_IMAGE_SIZE[0], TEST_IMAGE_SIZE[1], start_x, start_y, config)
    total_pixels = len(target_map)
    
    test_image_config = [{
        'image_path': used_png,
        'start_x': start_x,
        'start_y': start_y,
        'draw_mode': 'horizontal', # horizontal保证效率
        'scan_mode': 'normal',
        'weight': 1.0,
        'enabled': True
    }]
    
    print()
    
    # 运行测试
    result = await run_test_phase(
        config,
        users_with_tokens,
        test_image_config,
        show_progress=True
    )
    
    # 提取结果
    p_me = result['completion_rate']
    actual_speed = result['actual_speed']
    duration = result['duration']
    frame_completions = result.get('frame_completions', [])
    frame_matching = result.get('frame_matching', [])
    packets_sent = result.get('packets_sent', 0)
    packets_success = result.get('packets_success', 0)
    steady_state_reached = result.get('steady_state_reached', False)
    total_frames = result.get('total_frames', 0)
    volatility = result.get('volatility', 0)
    
    print()  # 空行分隔
    
    # 调试输出：显示所有采集数据
    print("\n=== 调试数据 ===")
    print(f"测试时长: {duration:.1f}秒")
    print(f"采集帧数: {total_frames} 帧 (帧间隔: {user_cd * 0.2:.1f}秒)")
    
    # 稳态状态显示（包含波动率）
    volatility_pct = volatility * 100
    if steady_state_reached:
        print(f"稳态状态: ✓ 已达稳态 (波动率: {volatility_pct:.1f}%)")
    else:
        # 判断是否是周期性稳态波动
        if volatility_pct > 15 and volatility_pct <= 45:
            print(f"稳态状态: ⚠️ 周期性波动 ({volatility_pct:.1f}%，可能是对方批量策略)")
        elif volatility_pct > 45:
            print(f"稳态状态: ❌ 未达稳态 - 波动过大 ({volatility_pct:.1f}% > 45%)")
        else:
            print(f"稳态状态: ⚠️ 未达稳态 (超时，波动率: {volatility_pct:.1f}%)")
    
    print(f"数据包: 发送={packets_sent}, 成功={packets_success}")
    
    # 计算效率
    theoretical_speed = num_tokens / user_cd if user_cd > 0 else 1
    my_efficiency = actual_speed / theoretical_speed if theoretical_speed > 0 else 0
    
    print(f"实际速度: {actual_speed:.2f} px/s (总成功数/总时长)")
    print(f"理论速度: {theoretical_speed:.2f} px/s")
    print(f"效率 η_m: {my_efficiency * 100:.1f}%")
    
    # 找到最大完成率及其位置
    if frame_completions:
        max_comp = max(frame_completions)
        max_idx = frame_completions.index(max_comp) + 1
        frames_since_max = len(frame_completions) - max_idx + 1
        print(f"最大完成率: {max_comp:.2f}% (帧{max_idx}，{frames_since_max}帧前)")
    
    print(f"\n关键帧数据 (仅显示最后15帧):")
    
    # 显示最后15帧（或全部，如果少于15帧）
    display_count = 15
    display_frames = frame_completions[-display_count:] if len(frame_completions) > display_count else frame_completions
    display_matching = frame_matching[-display_count:] if len(frame_matching) > display_count else frame_matching
    start_idx = max(0, len(frame_completions) - display_count)
    
    if display_frames and display_matching:
        for i in range(len(display_frames)):
            matching = display_matching[i]
            change = ""
            if i > 0:
                delta = display_matching[i] - display_matching[i-1]
                change = f" ({delta:+d})"
            # 标记最大值
            marker = " ←最大" if display_frames[i] == max_comp and abs(display_frames[i] - max_comp) < 0.01 else ""
            print(f"  帧{start_idx + i + 1}: {display_frames[i]:.2f}% ({matching}/{result.get('total_pixels', 0)}像素{change}){marker}")
        
        # 显示净增长（最后15帧）
        if len(display_matching) >= 2:
            net_change = display_matching[-1] - display_matching[0]
            print(f"\n  净增长 (最后{len(display_matching)}帧): {net_change:+d} 像素")
        
        print(f"\n平均完成率 p: {p_me * 100:.2f}%")
        
        # 显示稳态状态（使用函数返回的结果）
        if steady_state_reached:
            print(f"  ✓ 已达到稳态，测量结果可靠 (波动率: {volatility_pct:.1f}%)")
        else:
            if volatility_pct > 15 and volatility_pct <= 45:
                print(f"  ⚠️ 周期性波动 ({volatility_pct:.1f}%)")
                print("  说明: 完成率呈周期性波动，但平均值可作为参考")
                print("  建议: 结果基本可靠，但可考虑延长测量时间以获得更多周期数据")
            elif volatility_pct > 45:
                print(f"  ❌ 未达稳态 - 波动过大 ({volatility_pct:.1f}%)")
                print("  原因: 投入 token 数不足，被对方压制导致完成率剧烈波动")
                print("  建议: 至少增加 50% 的 token 数量后重新测量")
            else:
                print(f"  ⚠️ 未达稳态 (超时，波动率: {volatility_pct:.1f}%)")
                print("  建议: 增加 token 数量或延长超时时间")
    else:
        print(f"  完成率 p: {p_me * 100:.2f}%")
    print("=" * 40)
    
    # 计算对方token数
    if p_me <= 0.01:
        print("\n完成率过低，测量失败")
        return
    
    if p_me >= 0.99:
        print("\n完成率过高，可能无对抗")
        return
    
    # 使用修正后的公式计算
    enemy_result = calculate_enemy_tokens(
        p_me=p_me,
        user_cd=user_cd,
        num_my_tokens=num_tokens,
        enemy_area=enemy_area,
        overlap_area=overlap_area,
        my_efficiency=my_efficiency
    )
    
    if enemy_result is None:
        print("\n无法计算，完成率超出有效范围")
        return
    
    # 输出核心指标
    print("\n=== 分析结果 ===")
    
    # 如果未达稳态，给出警告
    if not steady_state_reached:
        if volatility_pct > 15 and volatility_pct <= 45:
            print("\n⚠️ 提示: 系统显示周期性波动（可能是对方批量/定时策略）")
            print("   完成率: 在合理范围内周期性波动")
            print("   评估: 使用平均值作为估算依据，结果基本可靠")
            print("   建议: 可适当增加测量时间以获得更准确的平均值\n")
        elif volatility_pct > 45:
            print("\n❌ 警告: 系统未达稳态 - 波动过大，估算结果不可靠!")
            print(f"   波动率: {volatility_pct:.1f}% (阈值: 45%)")
            print("   原因: 投入 token 数量不足，被对方压制")
            print("   结果: 完成率过低导致公式产生严重高估")
            print("   建议: 增加 token 数量使完成率达到 40%~60% 再测量\n")
        else:
            print("\n⚠️ 警告: 系统未达稳态，以下估算可能不准确!")
            print("   原因: 测试超时前未能稳定")
            print("   建议: 增加投入的 token 数量后重新测量\n")
    
    print(f"核心公式: N_e * η_e = N_m * η_m * (1-p) / p")
    print(f"  N_m = {num_tokens} (我方token数)")
    print(f"  η_m = {my_efficiency:.3f} (我方效率)")
    print(f"  p = {p_me:.3f} (我方占据率)")
    print(f"  => N_e * η_e = {enemy_result['effective_rate']:.1f} (对方有效覆盖速率)")
    
    print(f"\n对方 token 数估算 (取决于对方效率假设):")
    print(f"  若对方效率 = 100%: 约 {int(round(enemy_result['n_high_efficiency']))} 个 token")
    print(f"  若对方效率 = {my_efficiency*100:.0f}% (与我方相同): 约 {int(round(enemy_result['n_same_efficiency']))} 个 token")
    print(f"  若对方效率 = 50%: 约 {int(round(enemy_result['n_low_efficiency']))} 个 token")
    print(f"  若对方扫描全图 (100×100): 约 {int(round(enemy_result['n_scan_strategy']))} 个 token")
    
    # 给出综合建议
    print(f"\n📊 综合评估:")
    min_enemy = int(round(enemy_result['n_high_efficiency']))
    max_enemy = int(round(enemy_result['n_low_efficiency']))
    likely_enemy = int(round(enemy_result['n_same_efficiency']))
    
    if steady_state_reached:
        print(f"  对方 token 数范围: {min_enemy} ~ {max_enemy}")
        print(f"  最可能值: 约 {likely_enemy} 个")
    elif volatility_pct > 15 and volatility_pct <= 45:
        # 周期性波动，结果基本可靠
        print(f"  对方 token 数范围: {min_enemy} ~ {max_enemy} (基于周期平均)")
        print(f"  最可能值: 约 {likely_enemy} 个")
        print(f"  可信度: 中等（周期性波动，建议延长测量时间验证）")
    elif volatility_pct > 45:
        # 波动过大时，数据不可信
        print(f"  对方 token 数: ⚠️ 数据不可信 (波动过大)")
        print(f"  以上估算值严重虚高，请增加 token 后重新测量")
    else:
        print(f"  对方 token 数: 估算不可靠 (未达稳态)")
        print(f"  参考范围: >{min_enemy} 个 (下限)")
    
    # 推荐投入量
    recommend_equal = int(round(likely_enemy * 1.0))  # 持平
    recommend_advantage = int(round(likely_enemy * 1.5))  # 优势
    recommend_dominant = int(round(likely_enemy * 2.0))  # 压制
    
    print(f"\n推荐 token 投入量:")
    if steady_state_reached or (volatility_pct > 15 and volatility_pct <= 45):
        # 稳态或周期性稳定，给出推荐
        print(f"  持平 (p≈50%): {recommend_equal} 个")
        print(f"  优势 (p≈60%): {recommend_advantage} 个")
        print(f"  压制 (p≈70%): {recommend_dominant} 个")
        if not steady_state_reached:
            print(f"  注: 基于周期平均值计算，建议验证")
    elif volatility_pct > 45:
        # 建议增加到能达到稳态的数量
        suggested = max(num_tokens * 2, 80)  # 至少翻倍或80个
        print(f"  当前测量无效，建议先用 {suggested} 个 token 重新测量")
        print(f"  目标: 使完成率稳定在 40%~60% 区间")
    else:
        print(f"  建议先用 {max(num_tokens * 2, min_enemy * 2)} 个 token 重新测量")
    print()
