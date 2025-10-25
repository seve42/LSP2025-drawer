"""
ping.py - WebSocket 心跳管理模块

职责：
1. 接收并识别服务器的 Ping (0xfc) 包
2. 立即响应 Pong (0xfb) 包
3. 监控心跳超时并提供健康状态

设计原则：
- Pong 必须立即单独发送，不能使用粘包队列
- 根据 promot.md 文档，心跳是实时性要求最高的操作
- 提供清晰的日志和状态监控
"""

import asyncio
import logging
import time
from typing import Optional
import websockets
import multiprocessing
import queue


class HeartbeatManager:
    """WebSocket 心跳管理器
    
    负责处理服务器的心跳请求并维护连接健康状态。
    """
    
    def __init__(self, ws, timeout: float = 30.0):
        """初始化心跳管理器
        
        Args:
            ws: WebSocket 连接对象
            timeout: 心跳超时时间（秒），默认30秒
        """
        self.ws = ws
        self.timeout = timeout
        self.last_ping_time: Optional[float] = None
        self.last_pong_time: Optional[float] = None
        self.ping_count = 0
        self.pong_count = 0
        self.failed_pong_count = 0
        self._running = False
        
    async def handle_ping(self) -> bool:
        """处理服务器发来的 Ping 并立即回复 Pong
        
        关键实现：
        1. 记录收到 Ping 的时间
        2. 立即发送 Pong (0xfb)，不经过任何队列
        3. 记录发送结果
        
        Returns:
            bool: 是否成功发送 Pong
        """
        self.last_ping_time = time.monotonic()
        self.ping_count += 1
        
        logging.debug(f"[Heartbeat] 收到第 {self.ping_count} 次 Ping")
        
        try:
            # 【关键】立即单独发送 Pong，不使用粘包队列
            # 根据 promot.md 文档：ws.send(new Uint8Array([0xfb]))
            await self.ws.send(bytes([0xfb]))
            
            self.last_pong_time = time.monotonic()
            self.pong_count += 1
            
            response_time_ms = (self.last_pong_time - self.last_ping_time) * 1000
            logging.debug(f"[Heartbeat] 已发送第 {self.pong_count} 次 Pong (响应时间: {response_time_ms:.2f}ms)")
            
            return True
            
        except (websockets.exceptions.ConnectionClosed,
                websockets.exceptions.ConnectionClosedError,
                websockets.exceptions.ConnectionClosedOK) as e:
            self.failed_pong_count += 1
            err_msg = str(e) if str(e) else e.__class__.__name__
            logging.warning(f"[Heartbeat] 发送 Pong 失败（连接已关闭）: {err_msg}")
            return False
            
        except asyncio.TimeoutError:
            self.failed_pong_count += 1
            logging.warning("[Heartbeat] 发送 Pong 超时")
            return False
            
        except Exception as e:
            self.failed_pong_count += 1
            err_msg = str(e) if str(e) else e.__class__.__name__
            logging.error(f"[Heartbeat] 发送 Pong 时出现未预期异常: {err_msg}")
            return False
    
    def get_health_status(self) -> dict:
        """获取心跳健康状态
        
        Returns:
            dict: 包含各项健康指标的字典
        """
        now = time.monotonic()
        
        status = {
            'ping_count': self.ping_count,
            'pong_count': self.pong_count,
            'failed_pong_count': self.failed_pong_count,
            'success_rate': (self.pong_count / self.ping_count * 100) if self.ping_count > 0 else 100.0,
            'last_ping_time': self.last_ping_time,
            'last_pong_time': self.last_pong_time,
        }
        
        # 计算距离上次心跳的时间
        if self.last_ping_time:
            status['seconds_since_last_ping'] = now - self.last_ping_time
            status['is_timeout'] = (now - self.last_ping_time) > self.timeout
        else:
            status['seconds_since_last_ping'] = None
            status['is_timeout'] = False
            
        return status
    
    def reset_stats(self):
        """重置统计数据（通常在重连时使用）"""
        self.ping_count = 0
        self.pong_count = 0
        self.failed_pong_count = 0
        self.last_ping_time = None
        self.last_pong_time = None
        logging.debug("[Heartbeat] 统计数据已重置")
    
    def get_summary(self) -> str:
        """获取心跳统计摘要（用于日志）
        
        Returns:
            str: 格式化的统计摘要
        """
        status = self.get_health_status()
        
        summary = (
            f"心跳统计: Ping={status['ping_count']} "
            f"Pong={status['pong_count']} "
            f"失败={status['failed_pong_count']} "
            f"成功率={status['success_rate']:.1f}%"
        )
        
        if status['seconds_since_last_ping'] is not None:
            summary += f" 距上次Ping={status['seconds_since_last_ping']:.1f}s"
            
        if status['is_timeout']:
            summary += " [超时!]"
            
        return summary


async def monitor_heartbeat(heartbeat_manager: HeartbeatManager, 
                           check_interval: float = 5.0) -> None:
    """心跳监控任务（可选）
    
    定期检查心跳状态并输出日志。
    
    Args:
        heartbeat_manager: 心跳管理器实例
        check_interval: 检查间隔（秒）
    """
    try:
        while True:
            await asyncio.sleep(check_interval)
            
            status = heartbeat_manager.get_health_status()
            
            # 如果检测到超时，发出警告
            if status['is_timeout']:
                logging.warning(
                    f"[Heartbeat Monitor] 心跳超时警告! "
                    f"距上次Ping已过 {status['seconds_since_last_ping']:.1f}s "
                    f"(超时阈值: {heartbeat_manager.timeout}s)"
                )
            
            # 定期输出统计信息（仅在 DEBUG 级别）
            logging.debug(f"[Heartbeat Monitor] {heartbeat_manager.get_summary()}")
            
    except asyncio.CancelledError:
        logging.debug("[Heartbeat Monitor] 监控任务已取消")
        raise
    except Exception as e:
        logging.error(f"[Heartbeat Monitor] 监控任务异常: {e}")


# 向后兼容的简化函数（供现有代码直接调用）
async def send_pong(ws) -> bool:
    """立即发送 Pong 包（简化接口）
    
    这是一个独立函数，可以直接在消息处理循环中调用。
    不需要创建 HeartbeatManager 实例。
    
    Args:
        ws: WebSocket 连接对象
        
    Returns:
        bool: 是否成功发送
    """
    try:
        await ws.send(bytes([0xfb]))
        logging.debug("[Heartbeat] Pong 已发送")
        return True
    except Exception as e:
        err_msg = str(e) if str(e) else e.__class__.__name__
        logging.warning(f"[Heartbeat] 发送 Pong 失败: {err_msg}")
        return False


class PingProcess(multiprocessing.Process):
    """独立进程：接收来自主进程的 ping 事件并输出控制命令。

    设计：
    - 主进程将接收到的 Ping 事件通过 in_queue 发送到此进程。
    - 此进程立即在 out_queue 中放入 {'cmd':'send_pong'} 来请求主进程发送 Pong。
    - 进程维护心跳统计，并可在异常或连续失败时发出 {'cmd':'restart'}。
    """

    def __init__(self, in_queue: multiprocessing.Queue, out_queue: multiprocessing.Queue,
                 timeout: float = 30.0, max_consec_fail: int = 10):
        super().__init__()
        self.in_queue = in_queue
        self.out_queue = out_queue
        self.timeout = timeout
        self.max_consec_fail = max_consec_fail
        self.ping_count = 0
        self.last_ping_ts = None
        self.consec_fail = 0
        self.daemon = True

    def run(self):
        # 子进程独立初始化日志，以便调试
        logging.basicConfig(level=logging.INFO, format='[PingProc] %(asctime)s - %(levelname)s - %(message)s')
        logging.info('PingProcess 启动')
        try:
            while True:
                try:
                    item = self.in_queue.get(timeout=1.0)
                except queue.Empty:
                    # 检查超时
                    if self.last_ping_ts is not None:
                        if time.monotonic() - self.last_ping_ts > self.timeout:
                            logging.warning('长时间未收到 Ping，可能超时')
                    continue

                if not isinstance(item, dict):
                    continue

                cmd = item.get('cmd')
                if cmd == 'shutdown':
                    logging.info('收到 shutdown，退出 PingProcess')
                    break

                if item.get('type') == 'ping':
                    self.ping_count += 1
                    self.last_ping_ts = time.monotonic()
                    # 立即请求主进程发送 Pong
                    try:
                        self.out_queue.put({'cmd': 'send_pong', 'ts': self.last_ping_ts})
                    except Exception as e:
                        logging.warning(f'向 out_queue 发送命令失败: {e}')
                        self.consec_fail += 1
                        if self.consec_fail >= self.max_consec_fail:
                            logging.error('连续向主进程发送命令失败，触发重启请求')
                            try:
                                self.out_queue.put({'cmd': 'restart'})
                            except Exception:
                                pass
                # 其他命令可扩展

        except KeyboardInterrupt:
            logging.info('PingProcess 被中断')
        except Exception as e:
            logging.exception(f'PingProcess 发生未预期异常: {e}')
        finally:
            logging.info('PingProcess 退出')


def start_ping_process():
    """向外暴露的简单启动辅助（不使用时可忽略）"""
    in_q = multiprocessing.Queue()
    out_q = multiprocessing.Queue()
    p = PingProcess(in_q, out_q)
    p.start()
    return p, in_q, out_q


# 测试代码（仅在直接运行此文件时执行）
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    async def test_heartbeat():
        """测试心跳功能"""
        import websockets
        
        WS_URL = "wss://paintboard.luogu.me/api/paintboard/ws"
        
        async with websockets.connect(WS_URL, ping_interval=None, ping_timeout=None) as ws:
            logging.info("WebSocket 连接已建立，开始测试心跳...")
            
            hb_manager = HeartbeatManager(ws, timeout=30.0)
            
            monitor_task = asyncio.create_task(monitor_heartbeat(hb_manager, check_interval=10.0))
            
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
                        
                        if opcode == 0xfc:  # Ping
                            await hb_manager.handle_ping()
                        elif opcode == 0xfa:  # 画板更新
                            if offset + 7 <= len(data):
                                offset += 7
                        elif opcode == 0xff:  # 绘画结果
                            if offset + 5 <= len(data):
                                offset += 5
                        else:
                            logging.warning(f"未知操作码: 0x{opcode:02x}")
                            
            except KeyboardInterrupt:
                logging.info("测试被用户中断")
            finally:
                monitor_task.cancel()
                try:
                    await monitor_task
                except asyncio.CancelledError:
                    pass
                
                # 输出最终统计
                logging.info(f"测试结束 - {hb_manager.get_summary()}")
    
    # 运行测试
    try:
        asyncio.run(test_heartbeat())
    except KeyboardInterrupt:
        logging.info("测试程序退出")
