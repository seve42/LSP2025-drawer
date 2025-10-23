import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk
import time
import threading
import json
import os

CONFIG_FILE = "config.json"


def save_config(config: dict):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception:
        # GUI 中失败不抛出
        pass


def start_gui(config, pixels, width, height, users_with_tokens, gui_state):
    """启动 Tkinter GUI。

    - 顶部预览整个画板（1000x600），支持每30秒重建底图
    - “预览成果”按钮：切换目标图片覆盖
    - 下拉框切换 draw_mode，并即时写回 config + gui_state
    - “拖动设置起点”：在弹窗中拖动目标图片以设置 start_x/start_y（自动保存）
    - 简易配置编辑：显示/添加用户
    """
    root = tk.Tk()
    root.title("LSP2025 Drawer")

    # 画板尺寸固定
    BOARD_W, BOARD_H = 1000, 600

    # 将目标像素转换为 Image
    target_img = Image.new('RGBA', (width, height))
    try:
        target_img.putdata(pixels)
    except Exception:
        # 兼容像素可能是 (r,g,b) 的情况
        target_img = target_img.convert('RGB')
        target_img.putdata([(p[0], p[1], p[2]) for p in pixels])
        target_img = target_img.convert('RGBA')

    # 顶部预览区
    preview_frame = ttk.Frame(root)
    preview_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=False, padx=8, pady=8)

    canvas = tk.Canvas(preview_frame, width=BOARD_W, height=BOARD_H, bg="#222")
    canvas.pack()

    # 进度与控制区
    ctrl_frame = ttk.Frame(root)
    ctrl_frame.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)

    progress_var = tk.DoubleVar(value=0.0)
    progressbar = ttk.Progressbar(ctrl_frame, orient=tk.HORIZONTAL, length=400, mode='determinate', variable=progress_var, maximum=100.0)
    progressbar.grid(row=0, column=0, columnspan=4, sticky='w', padx=(0, 10))

    lbl_info = ttk.Label(ctrl_frame, text="")
    lbl_info.grid(row=1, column=0, columnspan=4, sticky='w')

    # 模式切换
    ttk.Label(ctrl_frame, text="模式:").grid(row=0, column=4, sticky='e')
    mode_var = tk.StringVar(value=config.get('draw_mode', 'random'))
    mode_box = ttk.Combobox(ctrl_frame, textvariable=mode_var, values=['horizontal', 'concentric', 'random'], state='readonly', width=12)
    mode_box.grid(row=0, column=5, sticky='w', padx=(4, 10))

    def on_mode_change(event=None):
        m = mode_var.get()
        with gui_state['lock']:
            gui_state['draw_mode'] = m
        config['draw_mode'] = m
        save_config(config)
    mode_box.bind('<<ComboboxSelected>>', on_mode_change)

    # 预览成果开关
    overlay_var = tk.BooleanVar(value=False)

    def toggle_overlay():
        v = not overlay_var.get()
        overlay_var.set(v)
        with gui_state['lock']:
            gui_state['overlay'] = v
        redraw()

    btn_overlay = ttk.Button(ctrl_frame, text='预览成果', command=toggle_overlay)
    btn_overlay.grid(row=0, column=6, sticky='w', padx=(4, 0))

    # 拖动设置起点
    def open_drag_window():
        top = tk.Toplevel(root)
        top.title('拖动设置起点')
        cv = tk.Canvas(top, width=BOARD_W, height=BOARD_H, bg="#222")
        cv.pack()

        # 局部状态
        with gui_state['lock']:
            start_x = int(gui_state.get('start_x', 0))
            start_y = int(gui_state.get('start_y', 0))
        ox, oy = start_x, start_y
        dragging = {'active': False, 'sx': 0, 'sy': 0, 'ox': ox, 'oy': oy}

        img_tk_holder = {'img': None}

        def clamp(n, lo, hi):
            return max(lo, min(hi, n))

        def redraw_drag():
            # 用最新的底图
            base = get_base_image()
            img = base.copy()
            # 覆盖目标
            img.paste(target_img, (ox, oy), mask=target_img.split()[3] if target_img.mode == 'RGBA' else None)
            # 转为 Tk 图像
            tkimg = ImageTk.PhotoImage(img)
            img_tk_holder['img'] = tkimg
            cv.create_image(0, 0, anchor='nw', image=tkimg)
            # 红框
            cv.create_rectangle(ox, oy, ox + width, oy + height, outline='red', width=2)

        def on_down(ev):
            dragging['active'] = True
            dragging['sx'] = ev.x
            dragging['sy'] = ev.y
            dragging['ox'] = ox
            dragging['oy'] = oy

        def on_move(ev):
            nonlocal ox, oy
            if not dragging['active']:
                return
            dx = ev.x - dragging['sx']
            dy = ev.y - dragging['sy']
            nx = clamp(dragging['ox'] + dx, 0, BOARD_W - width)
            ny = clamp(dragging['oy'] + dy, 0, BOARD_H - height)
            if (nx, ny) != (ox, oy):
                ox, oy = nx, ny
                redraw_drag()

        def on_up(ev):
            dragging['active'] = False

        def apply_and_close():
            # 写入 gui_state + config
            with gui_state['lock']:
                gui_state['start_x'] = int(ox)
                gui_state['start_y'] = int(oy)
            config['start_x'] = int(ox)
            config['start_y'] = int(oy)
            save_config(config)
            redraw()  # 主窗口也重绘
            top.destroy()

        cv.bind('<Button-1>', on_down)
        cv.bind('<B1-Motion>', on_move)
        cv.bind('<ButtonRelease-1>', on_up)

        ttk.Button(top, text='应用', command=apply_and_close).pack(pady=6)

        redraw_drag()

    btn_drag = ttk.Button(ctrl_frame, text='拖动设置起点', command=open_drag_window)
    btn_drag.grid(row=0, column=7, sticky='w', padx=(6, 0))

    # 用户添加区
    users_frame = ttk.LabelFrame(root, text='用户管理')
    users_frame.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)

    ttk.Label(users_frame, text=f"已配置用户: {len(config.get('users', []))}，有效Token: {len(users_with_tokens)}").grid(row=0, column=0, columnspan=4, sticky='w')
    ttk.Label(users_frame, text='UID').grid(row=1, column=0, sticky='e')
    uid_var = tk.StringVar()
    ttk.Entry(users_frame, textvariable=uid_var, width=12).grid(row=1, column=1, sticky='w')
    ttk.Label(users_frame, text='AccessKey').grid(row=1, column=2, sticky='e')
    ak_var = tk.StringVar()
    ttk.Entry(users_frame, textvariable=ak_var, width=18).grid(row=1, column=3, sticky='w')

    def add_user():
        try:
            uid = int(uid_var.get().strip())
            ak = ak_var.get().strip()
            if not ak:
                raise ValueError('access_key 不能为空')
        except Exception as e:
            messagebox.showerror('错误', f'输入无效: {e}')
            return
        users = config.setdefault('users', [])
        if any(u.get('uid') == uid for u in users):
            messagebox.showinfo('提示', '该 UID 已存在于配置中')
            return
        users.append({'uid': uid, 'access_key': ak})
        save_config(config)
        messagebox.showinfo('成功', '已添加用户并保存配置')

    ttk.Button(users_frame, text='添加用户', command=add_user).grid(row=1, column=4, sticky='w', padx=8)

    # 缓存的底图与时间戳
    cached = {
        'base_img': None,
        'tk_img': None,
        'last_build': 0.0
    }

    def get_board_state_copy():
        with gui_state['lock']:
            # 返回一个浅拷贝，避免遍历时被并发修改
            return dict(gui_state.get('board_state', {})), bool(gui_state.get('overlay', False)), int(gui_state.get('start_x', 0)), int(gui_state.get('start_y', 0))

    def build_base_from_state(board_state: dict):
        # 构建 RGB 底图（1000x600）
        img = Image.new('RGB', (BOARD_W, BOARD_H), color=(34, 34, 34))
        px = img.load()
        for (x, y), (r, g, b) in board_state.items():
            if 0 <= x < BOARD_W and 0 <= y < BOARD_H:
                px[x, y] = (r, g, b)
        return img

    def get_base_image():
        # 每 30 秒重建一次底图；首次强制构建
        now = time.time()
        # 若底图为空则立即构建
        board_state, _, _, _ = get_board_state_copy()
        if cached['base_img'] is None:
            cached['base_img'] = build_base_from_state(board_state)
            cached['last_build'] = now
            return cached['base_img']

        # 若底图已有但当前快照比上次为空->非空（首次填充），则立即重建以避免等待 30s
        if (now - cached['last_build'] < 30) and board_state and not cached.get('was_nonempty', False):
            # previous cached version was empty; rebuild now
            cached['base_img'] = build_base_from_state(board_state)
            cached['last_build'] = now
            cached['was_nonempty'] = True
            return cached['base_img']

        if (now - cached['last_build'] >= 30):
            board_state, _, _, _ = get_board_state_copy()
            cached['base_img'] = build_base_from_state(board_state)
            cached['last_build'] = now
        return cached['base_img']

    def redraw():
        # 生成显示图像（底图 + 可选覆盖 + 红框）
        base = get_base_image()
        img = base.copy()
        board_state, overlay_on, sx, sy = get_board_state_copy()
        if overlay_on:
            # 使用 alpha 遮罩
            mask = target_img.split()[3] if target_img.mode == 'RGBA' else None
            img.paste(target_img, (sx, sy), mask=mask)
        # 红框
        img2 = img.copy()
        # 使用 Canvas 绘制红框更高效，但为简化，这里直接画一条矩形边界
        # 简易画边：四条线
        def draw_h(y, x0, x1):
            for x in range(x0, x1):
                if 0 <= x < BOARD_W and 0 <= y < BOARD_H:
                    img2.putpixel((x, y), (255, 0, 0))
        def draw_v(x, y0, y1):
            for y in range(y0, y1):
                if 0 <= x < BOARD_W and 0 <= y < BOARD_H:
                    img2.putpixel((x, y), (255, 0, 0))
        x0, y0, x1, y1 = sx, sy, sx + width, sy + height
        draw_h(y0, x0, min(x1, BOARD_W))
        draw_h(min(y1-1, BOARD_H-1), x0, min(x1, BOARD_W))
        draw_v(x0, y0, min(y1, BOARD_H))
        draw_v(min(x1-1, BOARD_W-1), y0, min(y1, BOARD_H))

        tk_img = ImageTk.PhotoImage(img2)
        cached['tk_img'] = tk_img
        canvas.create_image(0, 0, anchor='nw', image=tk_img)

    # 文本与进度更新
    def update_status():
        with gui_state['lock']:
            total = int(gui_state.get('total', 0))
            mismatched = int(gui_state.get('mismatched', 0))
            available = int(gui_state.get('available', 0))
            ready = int(gui_state.get('ready_count', 0))
            # 读取最新 start_x/y 以便红框更新
            sx = int(gui_state.get('start_x', config.get('start_x', 0)))
            sy = int(gui_state.get('start_y', config.get('start_y', 0)))
        pct = 100.0 if total <= 0 else max(0.0, min(100.0, (total - mismatched) * 100.0 / max(1, total)))
        progress_var.set(pct)
        lbl_info.config(text=f"进度: {pct:6.2f}%  总像素: {total}  未达标: {mismatched}  可用用户: {available} (就绪:{ready})  起点: ({sx},{sy}) 大小: {width}x{height}")

    # 定时器：每秒刷新文字与图像；底图构建每 30 秒处理一次
    def tick():
        update_status()
        redraw()
        root.after(1000, tick)

    # 立即首次刷新
    cached['last_build'] = 0
    tick()

    root.protocol('WM_DELETE_WINDOW', root.destroy)
    root.mainloop()
