"""
多连接补丁 - 用于支持多个 WebSocket 连接

这个模块提供了handle_websocket_multi函数，用于替代原来的单连接实现。
使用方法：在main.py中导入此模块并调用 handle_websocket_multi 替代 handle_websocket
"""

import asyncio
import websockets
import logging
import time
import tool
from collections import defaultdict, deque

WS_URL = "wss://paintboard.luogu.me/api/paintboard/ws"


async def handle_websocket_multi(config, users_with_tokens, images_data, debug=False, gui_state=None):
    """支持多连接的 WebSocket 处理函数
    
    建立多个只写连接来提升稳定性和吞吐量，同时保留一个读写连接用于接收更新
    """
    paint_interval_ms = config.get("paint_interval_ms", 20)
    round_interval_seconds = config.get("round_interval_seconds", 30)
    user_cooldown_seconds = config.get("user_cooldown_seconds", 30)
    writeonly_count = min(5, max(1, config.get("writeonly_connections", 3)))
    
    # 合并所有图片的目标像素映射
    result = tool.merge_target_maps(images_data)
    if isinstance(result, tuple) and len(result) == 3:
        target_map, positions_by_mode, pos_to_image_idx = result
    else:
        target_map, positions_by_mode = result
        pos_to_image_idx = {}
    
    target_positions = []
    for mode in ['horizontal', 'concentric', 'random']:
        if mode in positions_by_mode:
            target_positions.extend(positions_by_mode[mode])
    
    positions_by_image = defaultdict(list)
    for pos in target_positions:
        idx = pos_to_image_idx.get(pos)
        if idx is not None:
            positions_by_image[idx].append(pos)
    positions_by_image = {k: deque(v) for k, v in positions_by_image.items()}
    
    logging.info(f"已加载 {len(images_data)} 个图片，合并后共 {len(target_map)} 个目标像素")
    logging.info(f"准备建立 1 个读写连接 + {writeonly_count} 个只写连接")
    
    # 初始化 GUI 状态
    if gui_state is not None:
        with gui_state['lock']:
            gui_state['total'] = len(target_positions)
            gui_state['mismatched'] = len(target_positions)
            gui_state['available'] = len(users_with_tokens)
            gui_state['ready_count'] = len(users_with_tokens)
            gui_state['images_data'] = images_data
            gui_state['stop'] = False
    
    conn_started_at = time.monotonic()
    connections = []  # [(ws, conn_id), ...]
    connection_tasks = []
    
    try:
        # 建立主连接（读写）
        main_ws = await websockets.connect(
            WS_URL,
            ping_interval=30,
            ping_timeout=60,
            open_timeout=15,
            close_timeout=10,
        )
        logging.info("主 WebSocket 连接（读写）已建立。")
        connections.append((main_ws, 0))
        tool.init_connection_queue(0)
        
        # 建立只写连接
        for i in range(1, writeonly_count + 1):
            try:
                wo_ws = await websockets.connect(
                    f"{WS_URL}?writeonly=1",
                    ping_interval=30,
                    ping_timeout=60,
                    open_timeout=15,
                    close_timeout=10,
                )
                logging.info(f"只写连接 #{i} 已建立。")
                connections.append((wo_ws, i))
                tool.init_connection_queue(i)
            except Exception as e:
                logging.warning(f"建立只写连接 #{i} 失败: {e}")
        
        logging.info(f"成功建立 {len(connections)} 个连接")
        
        if gui_state is not None:
            with gui_state['lock']:
                gui_state['connection_active'] = True
                gui_state['connection_count'] = len(connections)
        
        # 启动所有连接的发送任务
        for ws, conn_id in connections:
            task = asyncio.create_task(tool.send_paint_data(ws, paint_interval_ms, conn_id))
            connection_tasks.append(task)
            logging.debug(f"连接 {conn_id} 的发送任务已启动")
        
        # 初始化画板状态
        board_state = {}
        try:
            snapshot = tool.fetch_board_snapshot()
            if snapshot:
                board_state = snapshot.copy()
                if gui_state is not None:
                    with gui_state['lock']:
                        gui_state['board_state'] = board_state.copy()
                        gui_state['pos_to_image_idx'] = pos_to_image_idx
        except Exception:
            logging.debug("初始化画板快照时出现异常")
        
        state_changed_event = asyncio.Event()
        
        # 主连接的接收任务
        async def receiver():
            consecutive_errors = 0
            max_consecutive_errors = 10
            message_count = 0
            
            try:
                async for message in main_ws:
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
                                try:
                                    await main_ws.send(bytes([0xfb]))
                                    consecutive_errors = 0
                                except Exception as e:
                                    logging.warning(f"响应 Pong 失败: {e}")
                                    consecutive_errors += 1
                                    if consecutive_errors >= max_consecutive_errors:
                                        return
                            elif opcode == 0xff:  # 绘画结果
                                if offset + 5 > len(data):
                                    break
                                offset += 5
                            elif opcode == 0xfa:  # 画板更新
                                if offset + 7 > len(data):
                                    break
                                x = int.from_bytes(data[offset:offset+2], 'little'); offset += 2
                                y = int.from_bytes(data[offset:offset+2], 'little'); offset += 2
                                r, g, b = data[offset], data[offset+1], data[offset+2]; offset += 3
                                board_state[(x, y)] = (r, g, b)
                                if (x, y) in target_map:
                                    state_changed_event.set()
                                if gui_state is not None:
                                    try:
                                        with gui_state['lock']:
                                            gui_state['board_state'][(x, y)] = (r, g, b)
                                    except:
                                        pass
                        consecutive_errors = 0
                    except Exception:
                        consecutive_errors += 1
                        if consecutive_errors >= max_consecutive_errors:
                            break
                logging.info(f"接收了 {message_count} 条消息后正常退出")
            except Exception as e:
                logging.error(f"接收任务异常: {e}")
        
        receiver_task = asyncio.create_task(receiver())
        
        # 轮询分配连接 ID
        conn_round_robin = 0
        
        # 用户计数器和冷却时间
        user_counters = {u['uid']: 0 for u in users_with_tokens}
        cooldown_until = {u['uid']: 0.0 for u in users_with_tokens}
        
        # 主调度循环
        round_idx = 0
        while True:
            if gui_state is not None and gui_state.get('stop'):
                logging.info('收到停止信号')
                break
            
            now = time.monotonic()
            remaining = [pos for pos in target_positions if board_state.get(pos) != target_map[pos]]
            available_users = [u for u in users_with_tokens if cooldown_until.get(u['uid'], 0.0) <= now]
            
            assigned = 0
            if remaining and available_users:
                for user in available_users:
                    if not remaining:
                        break
                    
                    pick = remaining.pop(0)
                    x, y = pick
                    r, g, b = target_map[(x, y)]
                    uid = user['uid']
                    token = user['token']
                    paint_id = user_counters[uid]
                    
                    # 轮询选择连接
                    conn_id = conn_round_robin % len(connections)
                    conn_round_robin += 1
                    
                    # 使用选定的连接发送绘画任务
                    await tool.paint(None, uid, token, r, g, b, x, y, paint_id, conn_id)
                    
                    logging.info(f"UID {uid} [连接#{conn_id}] 绘制: ({x},{y}) RGB=({r},{g},{b}) ID={paint_id}")
                    user_counters[uid] = paint_id + 1
                    cooldown_until[uid] = now + user_cooldown_seconds
                    assigned += 1
                
                round_idx += 1
                left = len([pos for pos in target_positions if board_state.get(pos) != target_map[pos]])
                logging.info(f"第 {round_idx} 轮：分配 {assigned} 个任务，剩余 {left}")
            
            if not remaining:
                try:
                    await asyncio.wait_for(state_changed_event.wait(), timeout=round_interval_seconds)
                except asyncio.TimeoutError:
                    pass
                finally:
                    state_changed_event.clear()
                continue
            
            if remaining and assigned == 0:
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
            
            await asyncio.sleep(0.1)
        
        # 清理
        receiver_task.cancel()
        for task in connection_tasks:
            task.cancel()
        
        await asyncio.gather(receiver_task, *connection_tasks, return_exceptions=True)
        
        # 关闭所有连接
        for ws, conn_id in connections:
            try:
                await ws.close()
            except:
                pass
        
        logging.info("所有连接已关闭")
        
    except Exception as e:
        logging.error(f"多连接处理异常: {e}")
        # 清理
        for ws, _ in connections:
            try:
                await ws.close()
            except:
                pass
    finally:
        if gui_state is not None:
            with gui_state['lock']:
                gui_state['connection_active'] = False
    
    return max(0.0, time.monotonic() - conn_started_at)
