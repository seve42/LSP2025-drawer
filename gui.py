import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from PIL import Image, ImageTk
import sys
import time
import threading
import json
import os

CONFIG_FILE = "config.json"
REFRESH_CALLBACK = None


def save_config(config: dict):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception:
        pass


def save_and_offer_restart(config: dict):
    """保存配置并询问用户是否刷新配置。"""
    save_config(config)
    try:
        root = tk.Tk()
        root.withdraw()
        res = messagebox.askyesno('配置已保存', '配置已保存。是否刷新配置以应用更改（不会重新获取 token）？')
        root.destroy()
        if res:
            try:
                if REFRESH_CALLBACK is not None:
                    REFRESH_CALLBACK(config)
                    return
            except Exception:
                pass
            os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception:
        pass


def start_gui(config, images_data, users_with_tokens, gui_state):
    """启动 Tkinter GUI - 支持多图片管理。"""
    root = tk.Tk()
    root.title("LSP2025 Drawer - 多图片支持")

    BOARD_W, BOARD_H = 1000, 600

    # 主布局
    main_paned = ttk.PanedWindow(root, orient=tk.VERTICAL)
    main_paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    # 顶部：画板预览
    preview_frame = ttk.LabelFrame(main_paned, text="画板预览")
    main_paned.add(preview_frame, weight=3)

    canvas = tk.Canvas(preview_frame, width=BOARD_W, height=BOARD_H, bg="#222")
    canvas.pack()

    # 中部：进度信息
    info_frame = ttk.LabelFrame(main_paned, text="绘制进度")
    main_paned.add(info_frame, weight=1)

    progress_var = tk.DoubleVar(value=0.0)
    style = ttk.Style()
    try:
        style.theme_use('default')
    except Exception:
        pass
    style.configure('green.Horizontal.TProgressbar', troughcolor='#ddd', background='#4caf50')
    style.configure('red.Horizontal.TProgressbar', troughcolor='#ddd', background='#d32f2f')
    
    progressbar = ttk.Progressbar(info_frame, orient=tk.HORIZONTAL, length=800, mode='determinate', 
                                  variable=progress_var, maximum=100.0, style='green.Horizontal.TProgressbar')
    progressbar.pack(pady=5, padx=10)

    lbl_info = ttk.Label(info_frame, text="")
    lbl_info.pack(pady=5)

    eta_lbl = ttk.Label(info_frame, text='', width=40, anchor='w')
    eta_lbl.pack(pady=5)

    # 底部：图片管理
    images_frame = ttk.LabelFrame(main_paned, text="图片管理")
    main_paned.add(images_frame, weight=2)

    # 图片列表表格
    tree_frame = ttk.Frame(images_frame)
    tree_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    columns = ('启用', '图片路径', '起点X', '起点Y', '宽度', '高度', '绘图模式', '权重')
    tree = ttk.Treeview(tree_frame, columns=columns, show='headings', height=8)
    
    for col in columns:
        tree.heading(col, text=col)
        if col == '图片路径':
            tree.column(col, width=300)
        else:
            tree.column(col, width=80)
    
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    
    scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    tree.configure(yscrollcommand=scrollbar.set)

    # 图片操作按钮
    btn_frame = ttk.Frame(images_frame)
    btn_frame.pack(fill=tk.X, padx=5, pady=5)

    def refresh_tree():
        """刷新图片列表显示"""
        tree.delete(*tree.get_children())
        images_config = config.get('images', [])
        for idx, img_cfg in enumerate(images_config):
            enabled = '✓' if img_cfg.get('enabled', True) else '✗'
            tree.insert('', 'end', iid=str(idx), values=(
                enabled,
                img_cfg.get('image_path', ''),
                img_cfg.get('start_x', 0),
                img_cfg.get('start_y', 0),
                img_cfg.get('width', 'N/A'),  # 实际会从图片文件读取
                img_cfg.get('height', 'N/A'),
                img_cfg.get('draw_mode', 'random'),
                img_cfg.get('weight', 1.0)
            ))

    def add_image():
        """添加新图片"""
        file_path = filedialog.askopenfilename(
            title="选择图片文件",
            filetypes=[("图片文件", "*.png *.jpg *.jpeg *.bmp"), ("所有文件", "*.*")]
        )
        if file_path:
            try:
                # 尝试加载图片以获取尺寸
                img = Image.open(file_path)
                w, h = img.size
                
                new_img = {
                    'image_path': file_path,
                    'start_x': 0,
                    'start_y': 0,
                    'draw_mode': 'random',
                    'weight': 1.0,
                    'enabled': True,
                    'width': w,
                    'height': h
                }
                
                if 'images' not in config:
                    config['images'] = []
                config['images'].append(new_img)
                
                refresh_tree()
                save_and_offer_restart(config)
            except Exception as e:
                messagebox.showerror("错误", f"无法加载图片: {e}")

    def remove_image():
        """删除选中的图片"""
        selection = tree.selection()
        if not selection:
            messagebox.showwarning("提示", "请先选择要删除的图片")
            return
        
        if messagebox.askyesno("确认", "确定要删除选中的图片吗？"):
            # 从后往前删除，避免索引变化
            indices = sorted([int(item) for item in selection], reverse=True)
            for idx in indices:
                del config['images'][idx]
            
            refresh_tree()
            save_and_offer_restart(config)

    def edit_image():
        """编辑选中的图片"""
        selection = tree.selection()
        if not selection or len(selection) != 1:
            messagebox.showwarning("提示", "请选择一个图片进行编辑")
            return
        
        idx = int(selection[0])
        img_cfg = config['images'][idx]
        
        # 创建编辑窗口
        edit_win = tk.Toplevel(root)
        edit_win.title(f"编辑图片 - {os.path.basename(img_cfg['image_path'])}")
        edit_win.geometry("400x350")
        
        ttk.Label(edit_win, text="图片路径:").grid(row=0, column=0, sticky='e', padx=5, pady=5)
        path_var = tk.StringVar(value=img_cfg.get('image_path', ''))
        ttk.Entry(edit_win, textvariable=path_var, width=30, state='readonly').grid(row=0, column=1, padx=5, pady=5)
        
        ttk.Label(edit_win, text="起点 X:").grid(row=1, column=0, sticky='e', padx=5, pady=5)
        x_var = tk.IntVar(value=img_cfg.get('start_x', 0))
        ttk.Spinbox(edit_win, from_=0, to=999, textvariable=x_var, width=28).grid(row=1, column=1, padx=5, pady=5)
        
        ttk.Label(edit_win, text="起点 Y:").grid(row=2, column=0, sticky='e', padx=5, pady=5)
        y_var = tk.IntVar(value=img_cfg.get('start_y', 0))
        ttk.Spinbox(edit_win, from_=0, to=599, textvariable=y_var, width=28).grid(row=2, column=1, padx=5, pady=5)
        
        ttk.Label(edit_win, text="绘图模式:").grid(row=3, column=0, sticky='e', padx=5, pady=5)
        mode_var = tk.StringVar(value=img_cfg.get('draw_mode', 'random'))
        mode_combo = ttk.Combobox(edit_win, textvariable=mode_var, values=['horizontal', 'concentric', 'random'], 
                                  state='readonly', width=26)
        mode_combo.grid(row=3, column=1, padx=5, pady=5)
        
        ttk.Label(edit_win, text="权重:").grid(row=4, column=0, sticky='e', padx=5, pady=5)
        weight_var = tk.DoubleVar(value=img_cfg.get('weight', 1.0))
        ttk.Spinbox(edit_win, from_=0.1, to=10.0, increment=0.1, textvariable=weight_var, width=28).grid(row=4, column=1, padx=5, pady=5)
        
        ttk.Label(edit_win, text="启用:").grid(row=5, column=0, sticky='e', padx=5, pady=5)
        enabled_var = tk.BooleanVar(value=img_cfg.get('enabled', True))
        ttk.Checkbutton(edit_win, variable=enabled_var).grid(row=5, column=1, sticky='w', padx=5, pady=5)
        
        ttk.Label(edit_win, text="说明:\n权重越高的图片在重叠区域优先级越高\n绘图模式决定该图片的绘制顺序", 
                 justify=tk.LEFT, foreground='gray').grid(row=6, column=0, columnspan=2, padx=5, pady=10)
        
        def save_changes():
            img_cfg['start_x'] = x_var.get()
            img_cfg['start_y'] = y_var.get()
            img_cfg['draw_mode'] = mode_var.get()
            img_cfg['weight'] = weight_var.get()
            img_cfg['enabled'] = enabled_var.get()
            
            refresh_tree()
            save_and_offer_restart(config)
            edit_win.destroy()
        
        ttk.Button(edit_win, text="保存", command=save_changes).grid(row=7, column=0, columnspan=2, pady=10)

    def toggle_enabled():
        """切换选中图片的启用状态"""
        selection = tree.selection()
        if not selection:
            messagebox.showwarning("提示", "请先选择要切换的图片")
            return
        
        for item in selection:
            idx = int(item)
            config['images'][idx]['enabled'] = not config['images'][idx].get('enabled', True)
        
        refresh_tree()
        save_and_offer_restart(config)

    ttk.Button(btn_frame, text="添加图片", command=add_image).pack(side=tk.LEFT, padx=5)
    ttk.Button(btn_frame, text="编辑图片", command=edit_image).pack(side=tk.LEFT, padx=5)
    ttk.Button(btn_frame, text="删除图片", command=remove_image).pack(side=tk.LEFT, padx=5)
    ttk.Button(btn_frame, text="启用/禁用", command=toggle_enabled).pack(side=tk.LEFT, padx=5)

    # 控制按钮
    ctrl_btn_frame = ttk.Frame(info_frame)
    ctrl_btn_frame.pack(pady=5)

    overlay_var = tk.BooleanVar(value=False)

    def toggle_overlay():
        v = not overlay_var.get()
        overlay_var.set(v)
        with gui_state['lock']:
            gui_state['overlay'] = v
        redraw()

    ttk.Button(ctrl_btn_frame, text='预览成果', command=toggle_overlay).pack(side=tk.LEFT, padx=5)

    # 缓存的底图与时间戳
    cached = {
        'base_img': None,
        'tk_img': None,
        'last_build': 0.0,
        'was_nonempty': False
    }

    def get_board_state_copy():
        with gui_state['lock']:
            return dict(gui_state.get('board_state', {})), bool(gui_state.get('overlay', False))

    def build_base_from_state(board_state: dict):
        img = Image.new('RGB', (BOARD_W, BOARD_H), color=(34, 34, 34))
        px = img.load()
        for (x, y), (r, g, b) in board_state.items():
            if 0 <= x < BOARD_W and 0 <= y < BOARD_H:
                px[x, y] = (r, g, b)
        return img

    def get_base_image():
        now = time.time()
        board_state, _ = get_board_state_copy()
        
        if cached['base_img'] is None:
            cached['base_img'] = build_base_from_state(board_state)
            cached['last_build'] = now
            cached['was_nonempty'] = bool(board_state)
            return cached['base_img']

        if (now - cached['last_build'] < 30) and board_state and not cached.get('was_nonempty', False):
            cached['base_img'] = build_base_from_state(board_state)
            cached['last_build'] = now
            cached['was_nonempty'] = True
            return cached['base_img']

        if (now - cached['last_build'] >= 30):
            cached['base_img'] = build_base_from_state(board_state)
            cached['last_build'] = now
            cached['was_nonempty'] = bool(board_state)
        
        return cached['base_img']

    def redraw():
        base = get_base_image()
        img = base.copy()
        board_state, overlay_on = get_board_state_copy()
        
        if overlay_on:
            # 从 gui_state 获取最新的 images_data
            with gui_state['lock']:
                current_images_data = gui_state.get('images_data', images_data)
            
            # 绘制所有启用的目标图片
            for img_data in current_images_data:
                try:
                    target_img = Image.new('RGBA', (img_data['width'], img_data['height']))
                    target_img.putdata(img_data['pixels'])
                    mask = target_img.split()[3] if target_img.mode == 'RGBA' else None
                    img.paste(target_img, (img_data['start_x'], img_data['start_y']), mask=mask)
                except Exception:
                    pass
        
        # 绘制所有图片的边框
        with gui_state['lock']:
            current_images_data = gui_state.get('images_data', images_data)
        
        from PIL import ImageDraw
        draw = ImageDraw.Draw(img)
        for img_data in current_images_data:
            x0, y0 = img_data['start_x'], img_data['start_y']
            x1, y1 = x0 + img_data['width'], y0 + img_data['height']
            draw.rectangle([x0, y0, x1-1, y1-1], outline='red', width=2)

        tk_img = ImageTk.PhotoImage(img)
        cached['tk_img'] = tk_img
        canvas.create_image(0, 0, anchor='nw', image=tk_img)

    # 进度更新
    from collections import deque
    gui_history = deque()
    window_seconds = 60.0

    def update_status():
        with gui_state['lock']:
            total = int(gui_state.get('total', 0))
            mismatched = int(gui_state.get('mismatched', 0))
            available = int(gui_state.get('available', 0))
            ready = int(gui_state.get('ready_count', 0))
            resistance_pct = gui_state.get('resistance_pct', None)
            num_images = len(gui_state.get('images_data', []))
        
        pct = 100.0 if total <= 0 else max(0.0, min(100.0, (total - mismatched) * 100.0 / max(1, total)))
        progress_var.set(pct)

        # 计算增长率和 ETA
        now = time.monotonic()
        try:
            gui_history.append((now, pct))
            while gui_history and (now - gui_history[0][0] > window_seconds):
                gui_history.popleft()
            
            growth = None
            growth_str = ''
            eta_str = ''
            
            if len(gui_history) >= 2:
                t0, p0 = gui_history[0]
                t1, p1 = gui_history[-1]
                dt = max(1e-6, t1 - t0)
                growth = (p1 - p0) / dt
            
            if growth is None:
                growth_str = '增长: --'
            else:
                growth_str = f'增长: {growth:+.2f}%/s'
                if growth > 1e-6:
                    remain_pct = max(0.0, 100.0 - pct)
                    eta_s = remain_pct / growth
                    if eta_s >= 3600:
                        eta_str = f'估计剩余: {int(eta_s//3600)}h{int((eta_s%3600)//60)}m'
                    elif eta_s >= 60:
                        eta_str = f'估计剩余: {int(eta_s//60)}m{int(eta_s%60)}s'
                    else:
                        eta_str = f'估计剩余: {int(eta_s)}s'
                else:
                    if pct < 95.0:
                        eta_str = '估计剩余: 我们正在被攻击，无法抵抗'
                    else:
                        eta_str = '估计剩余: 即将完成'
        except Exception:
            growth_str = '增长: --'
            eta_str = ''

        # 危险状态检测
        danger = False
        try:
            if growth is not None and growth < 0 and pct < 95.0:
                danger = True
        except Exception:
            pass
        
        try:
            if danger:
                progressbar.configure(style='red.Horizontal.TProgressbar')
            else:
                progressbar.configure(style='green.Horizontal.TProgressbar')
        except Exception:
            pass

        res_str = ''
        if resistance_pct is not None:
            res_str = f'  抵抗率: {resistance_pct:5.1f}%'

        lbl_info.config(text=f"进度: {pct:6.2f}%  总像素: {total}  未达标: {mismatched}  "
                            f"可用用户: {available} (就绪:{ready})  图片数: {num_images}{res_str}  {growth_str}")
        eta_lbl.config(text=eta_str)

    def tick():
        update_status()
        redraw()
        root.after(1000, tick)

    # 初始化
    refresh_tree()
    cached['last_build'] = 0
    tick()

    root.protocol('WM_DELETE_WINDOW', root.destroy)
    root.mainloop()
