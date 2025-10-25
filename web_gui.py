"""
WebUI for LSP2025 Drawer - Flask based web interface
使用 Flask + WebSocket 实现与原 Tkinter GUI 完全一致的功能
"""
import json
import os
import threading
import time
from flask import Flask, render_template, jsonify, request, send_from_directory, session, redirect, url_for
from flask_socketio import SocketIO, emit
from PIL import Image
import base64
import io
import logging
import sys

app = Flask(__name__)
app.config['SECRET_KEY'] = 'lsp2025-drawer-secret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# 简单的会话/登录保护
@app.before_request
def require_login():
    # 允许无需登录访问的路径
    allowed_paths = ('/login', '/static/', '/socket.io/')
    # allow flask static endpoint too
    if request.path == '/' and session.get('logged_in'):
        return None
    for p in allowed_paths:
        if request.path.startswith(p):
            return None

    # 若已经登录，允许
    if session.get('logged_in'):
        return None

    # 对 API 返回 JSON 401，对于页面重定向到登录页
    if request.path.startswith('/api'):
        return jsonify({'error': 'authentication required'}), 401
    # 其它页面重定向到登录
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    """简单登录页，POST password 字段进行校验。"""
    # 允许从 config 或环境变量读取密码，默认 KenmaBuGaoJi
    expected = None
    try:
        expected = (config.get('webui_password') if config else None) or os.environ.get('WEBUI_PASSWORD')
    except Exception:
        expected = os.environ.get('WEBUI_PASSWORD')
    if not expected:
        expected = 'KenmaBuGaoJi'

    if request.method == 'POST':
        pw = request.form.get('password', '')
        if pw == expected:
            session['logged_in'] = True
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error='密码错误')

    return render_template('login.html', error=None)


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

# 全局日志
web_logs = []
web_logs_lock = threading.Lock()

def log_to_web(message):
    """向 Web 日志中添加一条消息并推送"""
    with web_logs_lock:
        log_entry = f"[{time.strftime('%H:%M:%S')}] {message}"
        web_logs.append(log_entry)
        if len(web_logs) > 200: # 限制日志数量
            web_logs.pop(0)
    # 在 emit 中加入 broadcast=True 确保所有客户端都能收到
    socketio.emit('log_update', {'message': log_entry})

# 全局状态
gui_state = None
config = None
images_data = None
users_with_tokens = None
REFRESH_CALLBACK = None
MAIN_RESTART_CALLBACK = None

def init_web_gui(cfg, imgs_data, users, state, restart_callback=None):
    """初始化 WebUI 的全局状态"""
    global gui_state, config, images_data, users_with_tokens, MAIN_RESTART_CALLBACK
    gui_state = state
    config = cfg
    images_data = imgs_data
    users_with_tokens = users
    MAIN_RESTART_CALLBACK = restart_callback
    # 将日志函数传递给主状态，以便其他模块调用
    if gui_state is not None:
        gui_state['log_to_web'] = log_to_web

def save_config_to_file(cfg):
    """保存配置到文件"""
    try:
        with open('config.json', 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=4)
        log_to_web("配置已保存。")
        return True
    except Exception as e:
        logging.error(f"保存配置失败: {e}")
        log_to_web(f"错误：保存配置失败: {e}")
        return False

@app.route('/')
def index():
    """主页面"""
    return render_template('index.html')

@app.route('/api/status')
def get_status():
    """获取当前状态信息"""
    if gui_state is None:
        return jsonify({'error': 'GUI state not initialized'}), 500
    
    try:
        with gui_state['lock']:
            status = {
                'total': gui_state.get('total', 0),
                'mismatched': gui_state.get('mismatched', 0),
                'available': gui_state.get('available', 0),
                'ready_count': gui_state.get('ready_count', 0),
                'resistance_pct': gui_state.get('resistance_pct'),
                'conn_status': gui_state.get('conn_status', 'unknown'),
                'conn_since': gui_state.get('conn_since', 0),
                'conn_reason': gui_state.get('conn_reason', ''),
                'server_offline': gui_state.get('server_offline', False),
                'overlay': gui_state.get('overlay', False),
                'assigned_per_image': dict(gui_state.get('assigned_per_image', {}))
            }
        return jsonify(status)
    except Exception as e:
        logging.error(f"获取状态失败: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/logs')
def get_logs():
    """获取历史日志"""
    with web_logs_lock:
        return jsonify({'logs': list(web_logs)})

@app.route('/api/restart', methods=['POST'])
def restart_backend():
    """请求后端重启"""
    log_to_web("收到重启请求，准备重启后端服务...")
    
    def do_restart():
        time.sleep(1) # 给前端一点时间显示消息
        if MAIN_RESTART_CALLBACK:
            try:
                MAIN_RESTART_CALLBACK()
                log_to_web("重启回调已成功执行。")
            except Exception as e:
                log_to_web(f"执行重启回调失败: {e}")
        else:
            log_to_web("未找到重启回调，将使用 sys.exit(1) 退出以供外部脚本重启。")
            # 使用非零退出码，以便外部脚本（如 .bat）可以检测并重启
            sys.exit(1)

    threading.Thread(target=do_restart).start()
    return jsonify({'success': True, 'message': '正在重启...'})

@app.route('/api/board')
def get_board():
    """获取画板状态"""
    if gui_state is None:
        return jsonify({'error': 'GUI state not initialized'}), 500
    
    try:
        with gui_state['lock']:
            board_state = dict(gui_state.get('board_state', {}))
            overlay = gui_state.get('overlay', False)
            current_images = gui_state.get('images_data', [])
        
        # 生成画板图像
        img = Image.new('RGB', (1000, 600), color=(34, 34, 34))
        pixels = img.load()
        
        # 绘制画板状态
        for (x, y), (r, g, b) in board_state.items():
            if 0 <= x < 1000 and 0 <= y < 600:
                pixels[x, y] = (r, g, b)
        
        # 如果开启 overlay，叠加目标图片
        if overlay and current_images:
            for img_data in current_images:
                try:
                    target_img = Image.new('RGBA', (img_data['width'], img_data['height']))
                    target_img.putdata(img_data['pixels'])
                    mask = target_img.split()[3] if target_img.mode == 'RGBA' else None
                    img.paste(target_img, (img_data['start_x'], img_data['start_y']), mask=mask)
                except Exception:
                    pass
        
        # 转换为 base64
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        img_str = base64.b64encode(buffer.getvalue()).decode()
        
        return jsonify({
            'image': img_str,
            'overlay': overlay
        })
    except Exception as e:
        logging.error(f"获取画板失败: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/images')
def get_images():
    """获取图片列表"""
    if config is None:
        return jsonify({'error': 'Config not initialized'}), 500
    
    try:
        images_config = config.get('images', [])
        images_list = []
        
        # 获取派发统计
        assigned_map = {}
        if gui_state:
            try:
                with gui_state['lock']:
                    assigned_map = dict(gui_state.get('assigned_per_image', {}))
            except Exception:
                pass
        
        for idx, img_cfg in enumerate(images_config):
            if str(img_cfg.get('type', '')).lower() == 'attack':
                kind = img_cfg.get('attack_kind', 'white')
                kind_cn = {'white': '白点', 'green': '亮绿色点', 'random': '随机色点'}.get(kind, kind)
                name = f"[攻击] {kind_cn} {img_cfg.get('width', '?')}x{img_cfg.get('height', '?')}"
            else:
                name = os.path.basename(img_cfg.get('image_path', ''))
            
            images_list.append({
                'index': idx,
                'enabled': img_cfg.get('enabled', True),
                'name': name,
                'start_x': img_cfg.get('start_x', 0),
                'start_y': img_cfg.get('start_y', 0),
                'width': img_cfg.get('width', 'N/A'),
                'height': img_cfg.get('height', 'N/A'),
                'draw_mode': img_cfg.get('draw_mode', 'random'),
                'weight': img_cfg.get('weight', 1.0),
                'assigned': assigned_map.get(idx, 0),
                'type': img_cfg.get('type', 'normal')
            })
        
        return jsonify({'images': images_list})
    except Exception as e:
        logging.error(f"获取图片列表失败: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/images/toggle', methods=['POST'])
def toggle_image():
    """切换图片启用状态"""
    data = request.get_json()
    idx = data.get('index')
    
    if idx is None or config is None:
        return jsonify({'error': 'Invalid request'}), 400
    
    try:
        config['images'][idx]['enabled'] = not config['images'][idx].get('enabled', True)
        save_config_to_file(config)
        
        # 重新加载图片
        def reload_worker():
            try:
                import tool
                new_images = tool.load_all_images(config)
                if gui_state:
                    with gui_state['lock']:
                        gui_state['images_data'] = new_images or []
                        gui_state['reload_pixels'] = True
            except Exception:
                pass
        
        threading.Thread(target=reload_worker, daemon=True).start()
        
        if REFRESH_CALLBACK:
            try:
                REFRESH_CALLBACK(config)
            except Exception:
                pass
        
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"切换图片失败: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/images/edit', methods=['POST'])
def edit_image():
    """编辑图片配置"""
    data = request.get_json()
    idx = data.get('index')
    
    if idx is None or config is None:
        return jsonify({'error': 'Invalid request'}), 400
    
    try:
        img_cfg = config['images'][idx]
        
        if 'start_x' in data:
            img_cfg['start_x'] = int(data['start_x'])
        if 'start_y' in data:
            img_cfg['start_y'] = int(data['start_y'])
        if 'draw_mode' in data:
            img_cfg['draw_mode'] = data['draw_mode']
        if 'weight' in data:
            img_cfg['weight'] = float(data['weight'])
        if 'enabled' in data:
            img_cfg['enabled'] = bool(data['enabled'])
        
        save_config_to_file(config)
        
        if REFRESH_CALLBACK:
            try:
                REFRESH_CALLBACK(config)
            except Exception:
                pass
        
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"编辑图片失败: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/images/delete', methods=['POST'])
def delete_image():
    """删除图片"""
    data = request.get_json()
    idx = data.get('index')
    
    if idx is None or config is None:
        return jsonify({'error': 'Invalid request'}), 400
    
    try:
        del config['images'][idx]
        save_config_to_file(config)
        
        if REFRESH_CALLBACK:
            try:
                REFRESH_CALLBACK(config)
            except Exception:
                pass
        
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"删除图片失败: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/overlay/toggle', methods=['POST'])
def toggle_overlay():
    """切换预览模式"""
    if gui_state is None:
        return jsonify({'error': 'GUI state not initialized'}), 500
    
    try:
        with gui_state['lock']:
            current = gui_state.get('overlay', False)
            gui_state['overlay'] = not current
        
        return jsonify({'success': True, 'overlay': not current})
    except Exception as e:
        logging.error(f"切换预览失败: {e}")
        return jsonify({'error': str(e)}), 500



@app.route('/api/images/<int:index>/preview')
def get_image_preview(index):
    """获取指定图片的预览（用于拖动设置起点）"""
    if config is None:
        return jsonify({'error': 'Config not initialized'}), 500
    
    try:
        images_config = config.get('images', [])
        if index < 0 or index >= len(images_config):
            return jsonify({'error': 'Invalid index'}), 400
        
        img_cfg = images_config[index]
        
        # 生成图片预览
        if str(img_cfg.get('type', '')).lower() == 'attack':
            # 攻击类型图片
            W = int(img_cfg.get('width', 0) or 0)
            H = int(img_cfg.get('height', 0) or 0)
            if W <= 0 or H <= 0:
                return jsonify({'error': 'Invalid dimensions'}), 400
            
            target_img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            kind = (img_cfg.get('attack_kind') or 'white').lower()
            import random as _random
            _rnd = _random.Random(W * 1315423911 ^ H * 2654435761)
            total = W * H
            dot_count = img_cfg.get('dot_count')
            try:
                dot_count = int(dot_count) if dot_count is not None else max(1, total // 50)
            except Exception:
                dot_count = max(1, total // 50)
            _px = target_img.load()
            _used = set()
            for _ in range(dot_count):
                _tries = 0
                while _tries < 5:
                    _x = _rnd.randrange(0, W)
                    _y = _rnd.randrange(0, H)
                    if (_x, _y) not in _used:
                        _used.add((_x, _y))
                        break
                    _tries += 1
                if kind == 'white':
                    _color = (255, 255, 255, 255)
                elif kind == 'green':
                    _color = (0, 255, 0, 255)
                elif kind == 'random':
                    _color = (_rnd.randrange(256), _rnd.randrange(256), _rnd.randrange(256), 255)
                else:
                    _color = (255, 255, 255, 255)
                try:
                    _px[_x, _y] = _color
                except Exception:
                    pass
        else:
            # 普通图片
            image_path = img_cfg.get('image_path')
            if not image_path or not os.path.exists(image_path):
                return jsonify({'error': 'Image file not found'}), 404
            target_img = Image.open(image_path).convert('RGBA')
        
        # 将图片编码为 base64
        buffer = io.BytesIO()
        target_img.save(buffer, format='PNG')
        img_str = base64.b64encode(buffer.getvalue()).decode()
        
        return jsonify({
            'image': img_str,
            'width': target_img.width,
            'height': target_img.height,
            'start_x': img_cfg.get('start_x', 0),
            'start_y': img_cfg.get('start_y', 0)
        })
    except Exception as e:
        logging.error(f"获取图片预览失败: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/images/<int:index>/position', methods=['POST'])
def update_image_position(index):
    """更新图片位置（拖动设置起点）"""
    data = request.get_json()
    
    if config is None:
        return jsonify({'error': 'Config not initialized'}), 400
    
    try:
        images_config = config.get('images', [])
        if index < 0 or index >= len(images_config):
            return jsonify({'error': 'Invalid index'}), 400
        
        img_cfg = images_config[index]
        img_cfg['start_x'] = int(data.get('start_x', 0))
        img_cfg['start_y'] = int(data.get('start_y', 0))
        
        save_config_to_file(config)
        
        if REFRESH_CALLBACK:
            try:
                REFRESH_CALLBACK(config)
            except Exception:
                pass
        
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"更新图片位置失败: {e}")
        return jsonify({'error': str(e)}), 500

# WebSocket 事件处理
@socketio.on('connect')
def handle_connect():
    """客户端连接"""
    emit('connected', {'status': 'ok'})
    # 启动状态推送线程
    threading.Thread(target=push_status_updates, daemon=True).start()

def push_status_updates():
    """定时推送状态更新到所有客户端"""
    while True:
        try:
            if gui_state:
                with gui_state['lock']:
                    status = {
                        'total': gui_state.get('total', 0),
                        'mismatched': gui_state.get('mismatched', 0),
                        'available': gui_state.get('available', 0),
                        'ready_count': gui_state.get('ready_count', 0),
                        'resistance_pct': gui_state.get('resistance_pct'),
                        'conn_status': gui_state.get('conn_status', 'unknown'),
                        'conn_since': gui_state.get('conn_since', 0),
                        'server_offline': gui_state.get('server_offline', False)
                    }
                socketio.emit('status_update', status)
            time.sleep(1)
        except Exception as e:
            logging.error(f"推送状态更新失败: {e}")
            time.sleep(1)

def start_web_gui(cfg, imgs_data, users, state, host='0.0.0.0', port=80, restart_callback=None):
    """启动 WebUI"""
    init_web_gui(cfg, imgs_data, users, state, restart_callback)
    
    # 设置日志级别
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.WARNING)
    
    print(f"\n{'='*60}")
    print(f"WebUI 已启动")
    print(f"{'='*60}")
    print(f"本地访问: http://localhost:{port}")
    print(f"局域网访问: http://你的IP地址:{port}")
    print(f"{'='*60}\n")
    
    socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)
