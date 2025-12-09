import asyncio
import pygame
import sys
import time
import struct
import logging
import websockets
import random
import math
import os
from uuid import UUID
import tool

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 常用颜色调色板
PALETTE = [
    (0, 0, 0), (255, 255, 255), (170, 170, 170), (85, 85, 85),
    (254, 211, 199), (255, 196, 206), (250, 172, 142), (255, 139, 131),
    (244, 67, 54), (233, 30, 99), (226, 102, 158), (156, 39, 176),
    (103, 58, 183), (63, 81, 181), (0, 70, 112), (5, 113, 151),
    (33, 150, 243), (0, 188, 212), (59, 229, 219), (151, 253, 220),
    (22, 115, 0), (55, 169, 60), (137, 230, 66), (215, 255, 7),
    (255, 246, 209), (248, 203, 140), (255, 235, 59), (255, 193, 7),
    (255, 152, 0), (255, 87, 34), (184, 63, 39), (121, 85, 72)
]

WS_URL = "wss://paintboard.luogu.me/api/paintboard/ws"

class PaintApp:
    def __init__(self, config, users_with_tokens):
        self.config = config
        self.width = 1000
        self.height = 600
        self.screen_width = 1200
        self.screen_height = 800
        
        # Pygame 初始化
        if not pygame.get_init():
            pygame.init()
            pygame.display.set_caption("LSP2025 Hand Paint - 手动绘板")
            self.screen = pygame.display.set_mode((self.screen_width, self.screen_height), pygame.RESIZABLE)
        else:
            self.screen = pygame.display.get_surface()
            
        self.clock = pygame.time.Clock()
        self.font = pygame.font.SysFont("Arial", 16)
        self.large_font = pygame.font.SysFont("Arial", 24, bold=True)
        
        # 绘板状态
        self.board_surface = pygame.Surface((self.width, self.height))
        self.board_surface.fill((255, 255, 255))
        
        # 视图控制
        self.zoom = 1.0
        self.offset_x = 100.0
        self.offset_y = 100.0
        self.dragging = False
        self.last_mouse_pos = None
        
        # 绘画状态
        self.selected_color = (0, 0, 0)
        self.painting = False
        self.last_paint_pos = None
        
        # Token 管理
        self.tokens = []
        self.user_cooldown = self.config.get('user_cooldown_seconds', 30)
        self.load_tokens(users_with_tokens)
        
        # WebSocket
        self.ws = None
        self.connected = False
        
        # 统计
        self.paint_count = 0
        self.start_time = time.time()
        
        # 初始快照
        self.snapshot_loaded = False

    def load_tokens(self, users_with_tokens):
        print(f"正在加载 {len(users_with_tokens)} 个用户的 Token...")
        for user in users_with_tokens:
            uid = user.get('uid')
            token = user.get('token')
            if not uid or not token:
                continue
                
            # 预处理 token bytes
            try:
                token_bytes = bytes.fromhex(token.replace('-', ''))
                if len(token_bytes) != 16:
                    token_bytes = UUID(token).bytes
            except Exception:
                print(f"Token 格式错误 uid={uid}")
                continue
                
            self.tokens.append({
                'uid': uid,
                'token': token,
                'token_bytes': token_bytes,
                'last_used': 0,
                'cooldown': self.user_cooldown
            })
        print(f"成功加载 {len(self.tokens)} 个可用 Token")

    def fetch_board(self):
        if self.snapshot_loaded:
            return
        print("正在下载绘板快照...")
        board_data = tool.fetch_board_snapshot()
        if board_data:
            print("快照下载完成，正在渲染...")
            px_array = pygame.PixelArray(self.board_surface)
            for (x, y), color in board_data.items():
                if 0 <= x < self.width and 0 <= y < self.height:
                    px_array[x, y] = color
            del px_array
            print("渲染完成")
            self.snapshot_loaded = True
        else:
            print("获取快照失败，使用空白画板")

    def get_available_token(self):
        now = time.time()
        for t in self.tokens:
            if now - t['last_used'] >= t['cooldown']:
                return t
        return None

    def get_available_count(self):
        now = time.time()
        count = 0
        for t in self.tokens:
            if now - t['last_used'] >= t['cooldown']:
                count += 1
        return count

    async def send_paint(self, x, y, color):
        token_info = self.get_available_token()
        if not token_info:
            return False
        
        token_info['last_used'] = time.time()
        
        r, g, b = color
        uid = token_info['uid']
        token_bytes = token_info['token_bytes']
        paint_id = random.randint(0, 4294967295)
        
        packet = bytearray(31)
        packet[0] = 0xfe
        packet[1:3] = x.to_bytes(2, 'little')
        packet[3:5] = y.to_bytes(2, 'little')
        packet[5:8] = bytes((r, g, b))
        packet[8:11] = uid.to_bytes(3, 'little')
        packet[11:27] = token_bytes
        packet[27:31] = paint_id.to_bytes(4, 'little')
        
        if self.ws and self.connected:
            try:
                await self.ws.send(packet)
                self.paint_count += 1
                return True
            except Exception as e:
                print(f"发送绘画失败: {e}")
        return False

    def screen_to_board(self, sx, sy):
        bx = (sx - self.offset_x) / self.zoom
        by = (sy - self.offset_y) / self.zoom
        return int(bx), int(by)

    def bresenham_line(self, x0, y0, x1, y1):
        """Bresenham 直线算法，返回两点之间的所有像素坐标"""
        points = []
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy
        
        x, y = x0, y0
        while True:
            points.append((x, y))
            if x == x1 and y == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x += sx
            if e2 < dx:
                err += dx
                y += sy
        
        return points

    def draw_ui(self):
        palette_h = 40
        palette_y = self.screen_height - palette_h
        pygame.draw.rect(self.screen, (50, 50, 50), (0, palette_y, self.screen_width, palette_h))
        
        swatch_w = 30
        swatch_h = 30
        margin = 5
        start_x = 10
        
        for i, color in enumerate(PALETTE):
            x = start_x + i * (swatch_w + margin)
            y = palette_y + (palette_h - swatch_h) // 2
            if color == self.selected_color:
                pygame.draw.rect(self.screen, (255, 255, 0), (x-2, y-2, swatch_w+4, swatch_h+4), 2)
            pygame.draw.rect(self.screen, color, (x, y, swatch_w, swatch_h))
            pygame.draw.rect(self.screen, (200, 200, 200), (x, y, swatch_w, swatch_h), 1)

        preview_size = 60
        pygame.draw.rect(self.screen, self.selected_color, (self.screen_width - preview_size - 10, palette_y - preview_size - 10, preview_size, preview_size))
        pygame.draw.rect(self.screen, (255, 255, 255), (self.screen_width - preview_size - 10, palette_y - preview_size - 10, preview_size, preview_size), 2)

        avail = self.get_available_count()
        total = len(self.tokens)
        info_text = f"可用 Token: {avail} / {total}"
        text_surf = self.large_font.render(info_text, True, (0, 255, 0) if avail > 0 else (255, 0, 0))
        self.screen.blit(text_surf, (10, 10))
        
        fps_text = self.font.render(f"FPS: {int(self.clock.get_fps())} | Zoom: {self.zoom:.2f}x", True, (0, 0, 0))
        self.screen.blit(fps_text, (10, 40))
        
        status_text = "已连接" if self.connected else "断开连接"
        status_surf = self.font.render(status_text, True, (0, 100, 0) if self.connected else (200, 0, 0))
        self.screen.blit(status_surf, (10, 60))

    def process_events(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                sys.exit()
            elif event.type == pygame.VIDEORESIZE:
                self.screen_width = event.w
                self.screen_height = event.h
                self.screen = pygame.display.set_mode((self.screen_width, self.screen_height), pygame.RESIZABLE)
            elif event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 1:
                    mx, my = event.pos
                    palette_y = self.screen_height - 40
                    if my >= palette_y:
                        swatch_w = 30
                        margin = 5
                        start_x = 10
                        idx = (mx - start_x) // (swatch_w + margin)
                        if 0 <= idx < len(PALETTE):
                            self.selected_color = PALETTE[idx]
                    else:
                        self.painting = True
                        bx, by = self.screen_to_board(mx, my)
                        if 0 <= bx < self.width and 0 <= by < self.height:
                            asyncio.create_task(self.send_paint(bx, by, self.selected_color))
                            self.last_paint_pos = (bx, by)
                            # 立即绘制到本地画板，提供即时反馈
                            self.board_surface.set_at((bx, by), self.selected_color)
                elif event.button == 3:
                    self.dragging = True
                    self.last_mouse_pos = event.pos
                elif event.button == 4:
                    mx, my = event.pos
                    bx, by = self.screen_to_board(mx, my)
                    self.zoom = min(self.zoom * 1.1, 20.0)
                    self.offset_x = mx - bx * self.zoom
                    self.offset_y = my - by * self.zoom
                elif event.button == 5:
                    mx, my = event.pos
                    bx, by = self.screen_to_board(mx, my)
                    self.zoom = max(self.zoom / 1.1, 0.1)
                    self.offset_x = mx - bx * self.zoom
                    self.offset_y = my - by * self.zoom
            elif event.type == pygame.MOUSEBUTTONUP:
                if event.button == 1:
                    self.painting = False
                    self.last_paint_pos = None
                elif event.button == 3:
                    self.dragging = False
            elif event.type == pygame.MOUSEMOTION:
                if self.dragging:
                    dx = event.pos[0] - self.last_mouse_pos[0]
                    dy = event.pos[1] - self.last_mouse_pos[1]
                    self.offset_x += dx
                    self.offset_y += dy
                    self.last_mouse_pos = event.pos
                if self.painting:
                    bx, by = self.screen_to_board(event.pos[0], event.pos[1])
                    if 0 <= bx < self.width and 0 <= by < self.height:
                        # 使用 Bresenham 算法填充从上一个点到当前点的所有像素
                        if self.last_paint_pos:
                            line_points = self.bresenham_line(
                                self.last_paint_pos[0], self.last_paint_pos[1],
                                bx, by
                            )
                            # 绘制线段上的所有点
                            for px, py in line_points:
                                if 0 <= px < self.width and 0 <= py < self.height:
                                    asyncio.create_task(self.send_paint(px, py, self.selected_color))
                                    # 立即更新本地画板
                                    self.board_surface.set_at((px, py), self.selected_color)
                        else:
                            # 如果没有上一个点，直接绘制当前点
                            asyncio.create_task(self.send_paint(bx, by, self.selected_color))
                            self.board_surface.set_at((bx, by), self.selected_color)
                        self.last_paint_pos = (bx, by)

    def render(self):
        self.screen.fill((200, 200, 200))
        bx1, by1 = self.screen_to_board(0, 0)
        bx2, by2 = self.screen_to_board(self.screen_width, self.screen_height)
        bx1 = max(0, bx1)
        by1 = max(0, by1)
        bx2 = min(self.width, bx2 + 1)
        by2 = min(self.height, by2 + 1)
        
        if bx2 > bx1 and by2 > by1:
            sub_w = bx2 - bx1
            sub_h = by2 - by1
            sub_surf = self.board_surface.subsurface((bx1, by1, sub_w, sub_h))
            scale_w = int(sub_w * self.zoom)
            scale_h = int(sub_h * self.zoom)
            if scale_w > 0 and scale_h > 0:
                scaled_sub = pygame.transform.scale(sub_surf, (scale_w, scale_h))
                dest_x = bx1 * self.zoom + self.offset_x
                dest_y = by1 * self.zoom + self.offset_y
                self.screen.blit(scaled_sub, (dest_x, dest_y))
        
        self.draw_ui()
        pygame.display.flip()

# 全局实例，保持状态
_app_instance = None

async def run_hand_paint(config, users_with_tokens, images_data=None):
    global _app_instance
    if _app_instance is None:
        _app_instance = PaintApp(config, users_with_tokens)
    
    app = _app_instance
    # 确保快照已加载
    app.fetch_board()
    
    # 代理设置 (复制自 main.py)
    proxy_keys = ['HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy']
    _saved_env = {}
    for _k in proxy_keys:
        if _k in os.environ:
            _saved_env[_k] = os.environ[_k]
            del os.environ[_k]
    
    try:
        no_proxy = os.environ.get('NO_PROXY') or os.environ.get('no_proxy') or ''
        add_hosts = ['paintboard.luogu.me', 'localhost', '127.0.0.1']
        for h in add_hosts:
            if h not in no_proxy:
                no_proxy = (no_proxy + ',' + h) if no_proxy else h
        os.environ['NO_PROXY'] = no_proxy
        os.environ['no_proxy'] = no_proxy
    except Exception:
        pass

    conn_started_at = time.monotonic()
    try:
        async with websockets.connect(
            WS_URL,
            ping_interval=None,
            ping_timeout=None,
            open_timeout=30,
            close_timeout=10,
            max_size=10 * 1024 * 1024,
        ) as ws:
            app.ws = ws
            app.connected = True
            logging.info("手动绘板模式: WebSocket 已连接")
            
            # 消息接收循环
            async def receive_loop():
                try:
                    while True:
                        msg = await ws.recv()
                        if isinstance(msg, bytes):
                            offset = 0
                            while offset < len(msg):
                                opcode = msg[offset]
                                offset += 1
                                if opcode == 0xfa: # 绘画更新
                                    if offset + 7 <= len(msg):
                                        x = int.from_bytes(msg[offset:offset+2], 'little')
                                        y = int.from_bytes(msg[offset+2:offset+4], 'little')
                                        r = msg[offset+4]
                                        g = msg[offset+5]
                                        b = msg[offset+6]
                                        offset += 7
                                        if 0 <= x < app.width and 0 <= y < app.height:
                                            app.board_surface.set_at((x, y), (r, g, b))
                                elif opcode == 0xfc: # Ping
                                    await ws.send(bytes([0xfb])) # Pong
                                elif opcode == 0xff: # 结果
                                    offset += 5
                                else:
                                    break
                except Exception as e:
                    logging.error(f"接收循环错误: {e}")
                    raise

            recv_task = asyncio.create_task(receive_loop())
            
            # 主循环：处理 GUI 和等待
            try:
                while not recv_task.done():
                    app.process_events()
                    app.render()
                    app.clock.tick(60)
                    await asyncio.sleep(0)
            finally:
                if not recv_task.done():
                    recv_task.cancel()
                try:
                    await recv_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                    
    except Exception as e:
        logging.error(f"WebSocket 连接异常: {e}")
        raise
    finally:
        app.connected = False
        app.ws = None
        if _saved_env:
            os.environ.update(_saved_env)
            
    return max(0.0, time.monotonic() - conn_started_at)
