"""
测试多连接功能

运行此脚本以验证多连接设置是否正常工作
"""

import json
import sys

def test_config():
    """测试配置文件"""
    print("正在检查配置文件...")
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        writeonly_conn = config.get('writeonly_connections', 0)
        print(f"✓ 配置文件读取成功")
        print(f"  - 只写连接数: {writeonly_conn}")
        print(f"  - 用户数量: {len(config.get('users', []))}")
        print(f"  - 图片数量: {len(config.get('images', []))}")
        
        if writeonly_conn > 0:
            print(f"✓ 多连接已启用 ({writeonly_conn} 个只写连接)")
        else:
            print("ℹ 多连接未启用（writeonly_connections = 0）")
        
        return True
    except Exception as e:
        print(f"✗ 配置文件读取失败: {e}")
        return False

def test_modules():
    """测试必要模块"""
    print("\n正在检查模块...")
    modules_ok = True
    
    try:
        import tool
        print("✓ tool.py 加载成功")
        
        # 检查多连接队列支持
        if hasattr(tool, 'init_connection_queue'):
            print("✓ 多连接队列函数可用")
            tool.init_connection_queue(0)
            tool.init_connection_queue(1)
            print("  - 测试队列初始化成功")
        else:
            print("✗ 多连接队列函数不可用")
            modules_ok = False
    except Exception as e:
        print(f"✗ tool.py 加载失败: {e}")
        modules_ok = False
    
    try:
        import multi_conn_patch
        print("✓ multi_conn_patch.py 加载成功")
        
        if hasattr(multi_conn_patch, 'handle_websocket_multi'):
            print("✓ 多连接处理函数可用")
        else:
            print("✗ 多连接处理函数不可用")
            modules_ok = False
    except Exception as e:
        print(f"✗ multi_conn_patch.py 加载失败: {e}")
        modules_ok = False
    
    return modules_ok

def test_dependencies():
    """测试依赖库"""
    print("\n正在检查依赖库...")
    deps_ok = True
    
    required = ['websockets', 'PIL', 'requests']
    for dep in required:
        try:
            if dep == 'PIL':
                __import__('PIL')
            else:
                __import__(dep)
            print(f"✓ {dep} 可用")
        except ImportError:
            print(f"✗ {dep} 未安装")
            deps_ok = False
    
    return deps_ok

def main():
    print("=" * 60)
    print("LSP2025-drawer 多连接功能测试")
    print("=" * 60)
    
    all_ok = True
    
    # 测试配置
    if not test_config():
        all_ok = False
    
    # 测试模块
    if not test_modules():
        all_ok = False
    
    # 测试依赖
    if not test_dependencies():
        all_ok = False
    
    print("\n" + "=" * 60)
    if all_ok:
        print("✓ 所有测试通过！")
        print("\n使用说明：")
        print("1. 在 config.json 中设置 'writeonly_connections' 参数（1-5）")
        print("2. 运行 main.py 启动程序")
        print("3. 程序将自动使用多连接模式")
        print("\n优势：")
        print("- 更高的吞吐量（每秒最多 256 * N 个包，N 为连接数）")
        print("- 更好的稳定性（单个连接故障不影响其他连接）")
        print("- 负载均衡（任务在多个连接间轮询分配）")
    else:
        print("✗ 部分测试失败，请检查上述错误")
    print("=" * 60)
    
    return 0 if all_ok else 1

if __name__ == "__main__":
    sys.exit(main())
