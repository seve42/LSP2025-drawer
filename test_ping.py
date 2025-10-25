"""
测试 ping.py 模块的心跳功能

运行此脚本以验证：
1. 能否正确连接到服务器
2. 能否正确识别和响应 Ping
3. 心跳统计是否准确
4. 能否稳定维持连接

使用方法:
    python test_ping.py

按 Ctrl+C 退出测试。
"""

import asyncio
import websockets
import logging
import ping

# 配置日志
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("test_ping.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)

WS_URL = "wss://paintboard.luogu.me/api/paintboard/ws"


async def test_heartbeat_basic():
    """基础心跳测试：连接30秒并记录心跳情况"""
    print("=" * 60)
    print("开始基础心跳测试（持续30秒）")
    print("=" * 60)
    
    try:
        async with websockets.connect(
            WS_URL,
            ping_interval=None,  # 禁用 websockets 库的自动 ping
            ping_timeout=None,
            open_timeout=30,
            close_timeout=10
        ) as ws:
            logging.info("✓ WebSocket 连接成功建立")
            
            # 创建心跳管理器
            hb_manager = ping.HeartbeatManager(ws, timeout=30.0)
            
            # 启动心跳监控任务
            monitor_task = asyncio.create_task(
                ping.monitor_heartbeat(hb_manager, check_interval=5.0)
            )
            
            start_time = asyncio.get_event_loop().time()
            test_duration = 30.0  # 测试30秒
            
            try:
                # 接收并处理消息
                async for message in ws:
                    # 检查是否超时
                    if asyncio.get_event_loop().time() - start_time > test_duration:
                        logging.info("测试时间到达，准备退出")
                        break
                    
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
                            logging.debug(f"收到操作码: 0x{opcode:02x}")
                            
            except KeyboardInterrupt:
                logging.info("测试被用户中断")
            finally:
                # 取消监控任务
                monitor_task.cancel()
                try:
                    await monitor_task
                except asyncio.CancelledError:
                    pass
                
                # 输出最终统计
                print("\n" + "=" * 60)
                print("测试结果")
                print("=" * 60)
                print(hb_manager.get_summary())
                
                status = hb_manager.get_health_status()
                print(f"\n详细统计:")
                print(f"  - 接收 Ping 次数: {status['ping_count']}")
                print(f"  - 发送 Pong 次数: {status['pong_count']}")
                print(f"  - 失败次数: {status['failed_pong_count']}")
                print(f"  - 成功率: {status['success_rate']:.2f}%")
                
                if status['ping_count'] > 0:
                    print(f"\n✓ 测试通过: 成功处理 {status['ping_count']} 次心跳")
                else:
                    print(f"\n✗ 测试失败: 未接收到任何心跳包")
                    
    except Exception as e:
        logging.exception(f"测试过程中出现异常: {e}")
        print(f"\n✗ 测试失败: {e}")


async def test_heartbeat_stress():
    """压力测试：长时间运行并统计心跳稳定性"""
    print("\n" + "=" * 60)
    print("开始压力测试（持续5分钟）")
    print("按 Ctrl+C 可提前退出")
    print("=" * 60)
    
    try:
        async with websockets.connect(
            WS_URL,
            ping_interval=None,
            ping_timeout=None,
            open_timeout=30,
            close_timeout=10
        ) as ws:
            logging.info("✓ WebSocket 连接成功建立")
            
            hb_manager = ping.HeartbeatManager(ws, timeout=30.0)
            monitor_task = asyncio.create_task(
                ping.monitor_heartbeat(hb_manager, check_interval=10.0)
            )
            
            start_time = asyncio.get_event_loop().time()
            test_duration = 300.0  # 5分钟
            
            try:
                async for message in ws:
                    elapsed = asyncio.get_event_loop().time() - start_time
                    if elapsed > test_duration:
                        logging.info(f"压力测试完成（运行 {elapsed:.1f}s）")
                        break
                    
                    # 每30秒输出一次进度
                    if int(elapsed) % 30 == 0 and int(elapsed) > 0:
                        status = hb_manager.get_health_status()
                        avg_interval = elapsed / max(status['ping_count'], 1)
                        print(f"[{int(elapsed)}s] {hb_manager.get_summary()} (平均心跳间隔: {avg_interval:.1f}s)")
                    
                    if isinstance(message, str):
                        data = bytearray(message.encode())
                    else:
                        data = bytearray(message)
                    
                    offset = 0
                    while offset < len(data):
                        opcode = data[offset]
                        offset += 1
                        
                        if opcode == 0xfc:
                            await hb_manager.handle_ping()
                        elif opcode == 0xfa:
                            if offset + 7 <= len(data):
                                offset += 7
                        elif opcode == 0xff:
                            if offset + 5 <= len(data):
                                offset += 5
                                
            except KeyboardInterrupt:
                logging.info("压力测试被用户中断")
            finally:
                monitor_task.cancel()
                try:
                    await monitor_task
                except asyncio.CancelledError:
                    pass
                
                elapsed = asyncio.get_event_loop().time() - start_time
                status = hb_manager.get_health_status()
                
                print("\n" + "=" * 60)
                print("压力测试结果")
                print("=" * 60)
                print(f"运行时长: {elapsed:.1f}s")
                print(hb_manager.get_summary())
                
                if status['ping_count'] > 0:
                    avg_interval = elapsed / status['ping_count']
                    print(f"平均心跳间隔: {avg_interval:.2f}s")
                    
                if status['success_rate'] >= 99.0:
                    print(f"\n✓ 压力测试通过: 成功率 {status['success_rate']:.2f}%")
                else:
                    print(f"\n⚠ 压力测试警告: 成功率仅 {status['success_rate']:.2f}%")
                    
    except Exception as e:
        logging.exception(f"压力测试过程中出现异常: {e}")
        print(f"\n✗ 压力测试失败: {e}")


async def test_simplified_api():
    """测试简化的 API（send_pong 函数）"""
    print("\n" + "=" * 60)
    print("测试简化 API（持续15秒）")
    print("=" * 60)
    
    try:
        async with websockets.connect(
            WS_URL,
            ping_interval=None,
            ping_timeout=None
        ) as ws:
            logging.info("✓ WebSocket 连接成功建立")
            
            ping_count = 0
            pong_success = 0
            start_time = asyncio.get_event_loop().time()
            
            try:
                async for message in ws:
                    if asyncio.get_event_loop().time() - start_time > 15.0:
                        break
                    
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
                            # 使用简化 API
                            if await ping.send_pong(ws):
                                pong_success += 1
                        elif opcode == 0xfa:
                            if offset + 7 <= len(data):
                                offset += 7
                        elif opcode == 0xff:
                            if offset + 5 <= len(data):
                                offset += 5
                                
            except KeyboardInterrupt:
                pass
            
            print("\n" + "=" * 60)
            print("简化 API 测试结果")
            print("=" * 60)
            print(f"接收 Ping: {ping_count}")
            print(f"发送 Pong: {pong_success}")
            print(f"成功率: {(pong_success/max(ping_count,1)*100):.2f}%")
            
            if ping_count > 0 and pong_success == ping_count:
                print("\n✓ 简化 API 测试通过")
            else:
                print("\n✗ 简化 API 测试失败")
                
    except Exception as e:
        logging.exception(f"简化 API 测试过程中出现异常: {e}")
        print(f"\n✗ 简化 API 测试失败: {e}")


async def main():
    """运行所有测试"""
    try:
        # 测试1: 基础功能
        await test_heartbeat_basic()
        
        # 测试2: 简化 API
        await test_simplified_api()
        
        # 询问是否运行压力测试
        print("\n是否运行5分钟压力测试？(y/n): ", end="")
        # 注意：在异步环境中读取输入需要特殊处理
        # 这里简化为自动跳过
        print("已跳过（如需运行，请直接调用 test_heartbeat_stress）")
        
        print("\n" + "=" * 60)
        print("所有基础测试完成")
        print("=" * 60)
        
    except KeyboardInterrupt:
        print("\n测试被用户中断")
    except Exception as e:
        logging.exception(f"测试运行失败: {e}")


if __name__ == "__main__":
    print("""
╔════════════════════════════════════════════════════════════╗
║           Paintboard 心跳功能测试工具                      ║
║                                                            ║
║  此工具将测试 ping.py 模块的心跳处理功能                   ║
║  确保能够正确响应服务器的 Ping 包                          ║
╚════════════════════════════════════════════════════════════╝
""")
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n程序已退出")
