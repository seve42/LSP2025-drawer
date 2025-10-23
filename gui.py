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
    
    # 设置默认窗口大小为1920×1080的1/4
    default_width = 480
    default_height = 270
    root.geometry(f"{default_width}x{default_height}")
    
    # 允许窗口调整大小
    root.minsize(400, 250)

    BOARD_W, BOARD_H = 1000, 600
    # 画板预览缩放比例（默认缩小到1/3）
    preview_scale = 0.25

    # 主布局 - 使用可滚动的Canvas
    main_canvas = tk.Canvas(root)
    main_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    
    # 添加垂直滚动条
    v_scrollbar = ttk.Scrollbar(root, orient=tk.VERTICAL, command=main_canvas.yview)
    v_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    main_canvas.configure(yscrollcommand=v_scrollbar.set)
    
    # 创建内部Frame来容纳所有内容
    main_frame = ttk.Frame(main_canvas)
    main_canvas_window = main_canvas.create_window((0, 0), window=main_frame, anchor='nw')
    
    # 绑定调整大小事件，并根据内容高度决定是否显示滚动条
    def on_frame_configure(event=None):
        # 更新滚动区域
        try:
            main_canvas.configure(scrollregion=main_canvas.bbox("all"))
        except Exception:
            pass
        # 让内部frame的宽度跟随canvas
        canvas_width = main_canvas.winfo_width()
        if canvas_width > 1:
            main_canvas.itemconfig(main_canvas_window, width=canvas_width)
        # 根据内容高度决定是否显示滚动条
        adjust_scrollbar_visibility()

    main_frame.bind('<Configure>', on_frame_configure)
    main_canvas.bind('<Configure>', on_frame_configure)

    # 鼠标滚轮处理函数（垂直滚动）
    def on_mousewheel(event):
        try:
            main_canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        except Exception:
            pass

    def adjust_scrollbar_visibility():
        # 检查内部内容高度是否超过可视区域
        try:
            bbox = main_canvas.bbox(main_canvas_window)
            if not bbox:
                # 没有内容，隐藏滚动条
                try:
                    v_scrollbar.pack_forget()
                except Exception:
                    pass
                try:
                    main_canvas.unbind_all("<MouseWheel>")
                except Exception:
                    pass
                return
            content_height = bbox[3] - bbox[1]
            view_height = main_canvas.winfo_height()
            if content_height <= max(0, view_height - 4):
                # 内容未超出可视区，隐藏滚动条并解绑滚轮
                try:
                    v_scrollbar.pack_forget()
                except Exception:
                    pass
                try:
                    main_canvas.unbind_all("<MouseWheel>")
                except Exception:
                    pass
            else:
                # 内容超出，显示滚动条并绑定滚轮
                try:
                    # 如果尚未布局，则 pack
                    if not v_scrollbar.winfo_ismapped():
                        v_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
                    main_canvas.configure(yscrollcommand=v_scrollbar.set)
                except Exception:
                    pass
                try:
                    main_canvas.bind_all("<MouseWheel>", on_mousewheel)
                except Exception:
                    pass
        except Exception:
            pass

    # 顶部：画板预览（自适应大小，支持拖拽和缩放）
    preview_frame = ttk.LabelFrame(main_frame, text="画板预览 (滚轮缩放 | 拖拽平移)")
    preview_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    # 预览画布容器
    preview_container = ttk.Frame(preview_frame)
    preview_container.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
    
    # 画布状态
    canvas_state = {
        'scale': preview_scale,  # 当前缩放比例
        'offset_x': 0,           # X方向偏移
        'offset_y': 0,           # Y方向偏移
        'dragging': False,
        'drag_start_x': 0,
        'drag_start_y': 0,
        'canvas_width': 400,     # 画布显示宽度
        'canvas_height': 300     # 画布显示高度
    }
    
    canvas = tk.Canvas(preview_container, bg="#222", highlightthickness=0)
    canvas.pack(fill=tk.BOTH, expand=True)
    
    # 缩放信息标签
    scale_info_label = ttk.Label(preview_frame, text=f"缩放: {int(preview_scale*100)}%", foreground='gray')
    scale_info_label.pack(side=tk.BOTTOM, pady=2)
    
    # 自适应画布大小
    def update_canvas_size(event=None):
        canvas_state['canvas_width'] = canvas.winfo_width()
        canvas_state['canvas_height'] = canvas.winfo_height()
        # 初次加载时，自动调整缩放以适应窗口
        if canvas_state['canvas_width'] > 1 and canvas_state['scale'] == 0.25:
            # 计算合适的缩放比例
            scale_w = canvas_state['canvas_width'] / BOARD_W
            scale_h = canvas_state['canvas_height'] / BOARD_H
            canvas_state['scale'] = min(scale_w, scale_h, 1.0)
            scale_info_label.config(text=f"缩放: {int(canvas_state['scale']*100)}%")
        redraw()
    
    canvas.bind('<Configure>', update_canvas_size)
    
    # 鼠标拖拽功能
    def on_canvas_press(event):
        canvas_state['dragging'] = True
        canvas_state['drag_start_x'] = event.x
        canvas_state['drag_start_y'] = event.y
        canvas.config(cursor='fleur')
    
    def on_canvas_drag(event):
        if canvas_state['dragging']:
            dx = event.x - canvas_state['drag_start_x']
            dy = event.y - canvas_state['drag_start_y']
            canvas_state['offset_x'] += dx
            canvas_state['offset_y'] += dy
            canvas_state['drag_start_x'] = event.x
            canvas_state['drag_start_y'] = event.y
            constrain_offset()
            redraw()
    
    def on_canvas_release(event):
        canvas_state['dragging'] = False
        canvas.config(cursor='')
    
    # 鼠标滚轮缩放
    def on_canvas_wheel(event):
        # 获取鼠标位置
        mouse_x = event.x
        mouse_y = event.y
        
        # 计算缩放前鼠标指向的图像坐标
        old_scale = canvas_state['scale']
        img_x = (mouse_x - canvas_state['offset_x']) / old_scale
        img_y = (mouse_y - canvas_state['offset_y']) / old_scale
        
        # 调整缩放
        if event.delta > 0:
            canvas_state['scale'] *= 1.1
        else:
            canvas_state['scale'] /= 1.1
        
        # 限制缩放范围
        canvas_state['scale'] = max(0.1, min(5.0, canvas_state['scale']))
        
        # 调整偏移以保持鼠标位置不变
        canvas_state['offset_x'] = mouse_x - img_x * canvas_state['scale']
        canvas_state['offset_y'] = mouse_y - img_y * canvas_state['scale']
        
        scale_info_label.config(text=f"缩放: {int(canvas_state['scale']*100)}%")
    
    
    canvas.bind('<Button-1>', on_canvas_press)
    canvas.bind('<B1-Motion>', on_canvas_drag)
    canvas.bind('<ButtonRelease-1>', on_canvas_release)
    canvas.bind('<MouseWheel>', on_canvas_wheel)

    # 中部：进度信息（紧凑布局）
    info_frame = ttk.LabelFrame(main_frame, text="绘制进度")
    info_frame.pack(fill=tk.X, padx=5, pady=5)

    progress_var = tk.DoubleVar(value=0.0)
    style = ttk.Style()
    try:
        style.theme_use('default')
    except Exception:
        pass
    style.configure('green.Horizontal.TProgressbar', troughcolor='#ddd', background='#4caf50')
    style.configure('red.Horizontal.TProgressbar', troughcolor='#ddd', background='#d32f2f')
    
    progressbar = ttk.Progressbar(info_frame, orient=tk.HORIZONTAL, mode='determinate', 
                                  variable=progress_var, maximum=100.0, style='green.Horizontal.TProgressbar')
    progressbar.pack(fill=tk.X, pady=3, padx=5)

    lbl_info = ttk.Label(info_frame, text="", wraplength=450, justify=tk.LEFT)
    lbl_info.pack(fill=tk.X, pady=2, padx=5)

    # 可用/就绪用户展示
    users_lbl = ttk.Label(info_frame, text='可用: 0 | 就绪: 0', anchor='w', foreground='blue')
    users_lbl.pack(fill=tk.X, pady=1, padx=5)

    eta_lbl = ttk.Label(info_frame, text='', anchor='w')
    eta_lbl.pack(fill=tk.X, pady=2, padx=5)

    # 底部：图片管理（可折叠）
    images_frame = ttk.LabelFrame(main_frame, text="图片管理")
    images_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    # 图片列表表格（紧凑高度）
    tree_frame = ttk.Frame(images_frame)
    tree_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    columns = ('启用', '图片路径', '起点X', '起点Y', '宽度', '高度', '模式', '权重')
    tree = ttk.Treeview(tree_frame, columns=columns, show='headings', height=5)
    
    for col in columns:
        tree.heading(col, text=col)
        if col == '图片路径':
            tree.column(col, width=200, minwidth=100)
        elif col in ('启用', '模式', '权重'):
            tree.column(col, width=50, minwidth=40)
        else:
            tree.column(col, width=60, minwidth=40)
    
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    
    scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    tree.configure(yscrollcommand=scrollbar.set)

    # 图片操作按钮（紧凑布局）
    btn_frame = ttk.Frame(images_frame)
    btn_frame.pack(fill=tk.X, padx=5, pady=3)

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

    ttk.Button(btn_frame, text="➕ 添加", command=add_image, width=8).pack(side=tk.LEFT, padx=2)
    ttk.Button(btn_frame, text="✏️ 编辑", command=edit_image, width=8).pack(side=tk.LEFT, padx=2)
    ttk.Button(btn_frame, text="🗑️ 删除", command=remove_image, width=8).pack(side=tk.LEFT, padx=2)
    ttk.Button(btn_frame, text="⚡ 切换", command=toggle_enabled, width=8).pack(side=tk.LEFT, padx=2)

    # 控制按钮（放在进度信息区域）
    ctrl_btn_frame = ttk.Frame(info_frame)
    ctrl_btn_frame.pack(fill=tk.X, pady=3, padx=5)

    overlay_var = tk.BooleanVar(value=False)

    def toggle_overlay():
        v = not overlay_var.get()
        overlay_var.set(v)
        with gui_state['lock']:
            gui_state['overlay'] = v
        redraw()

    ttk.Button(ctrl_btn_frame, text='👁️ 预览成果', command=toggle_overlay).pack(side=tk.LEFT, padx=2)
    
    # 拖动设置起点功能
    def open_drag_window():
        """打开拖动设置起点窗口"""
        # 检查是否有图片
        if not config.get('images'):
            messagebox.showwarning("提示", "请先添加至少一个图片")
            return
        
        # 让用户选择要调整的图片
        if len(config['images']) == 1:
            img_idx = 0
        else:
            # 创建选择窗口
            select_win = tk.Toplevel(root)
            select_win.title("选择要调整的图片")
            select_win.geometry("300x200")
            
            ttk.Label(select_win, text="请选择要调整起点的图片:").pack(pady=10)
            
            listbox = tk.Listbox(select_win)
            listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
            
            for idx, img in enumerate(config['images']):
                listbox.insert(tk.END, f"{idx+1}. {os.path.basename(img.get('image_path', ''))}")
            
            listbox.select_set(0)
            
            selected_idx = [0]
            
            def on_select():
                if listbox.curselection():
                    selected_idx[0] = listbox.curselection()[0]
                    select_win.destroy()
                else:
                    messagebox.showwarning("提示", "请选择一个图片")
            
            ttk.Button(select_win, text="确定", command=on_select).pack(pady=5)
            
            select_win.wait_window()
            img_idx = selected_idx[0]
        
        img_config = config['images'][img_idx]
        
        # 创建拖动窗口
        drag_win = tk.Toplevel(root)
        drag_win.title(f'拖动设置起点 - {os.path.basename(img_config["image_path"])}')
        drag_win.geometry(f"{BOARD_W}x{BOARD_H+50}")
        
        # 加载该图片
        try:
            target_img = Image.open(img_config['image_path']).convert('RGBA')
            img_w, img_h = target_img.size
        except Exception as e:
            messagebox.showerror("错误", f"无法加载图片: {e}")
            drag_win.destroy()
            return
        
        drag_canvas = tk.Canvas(drag_win, width=BOARD_W, height=BOARD_H, bg="#222")
        drag_canvas.pack()
        
        # 当前起点
        current_x = img_config.get('start_x', 0)
        current_y = img_config.get('start_y', 0)
        
        drag_state = {
            'x': current_x,
            'y': current_y,
            'dragging': False,
            'start_mouse_x': 0,
            'start_mouse_y': 0,
            'start_img_x': current_x,
            'start_img_y': current_y
        }
        
        tk_img_holder = {'img': None}
        
        def redraw_drag():
            # 获取底图
            base = get_base_image()
            display_img = base.copy()
            
            # 贴上目标图片
            try:
                display_img.paste(target_img, (drag_state['x'], drag_state['y']), 
                                mask=target_img.split()[3] if target_img.mode == 'RGBA' else None)
            except Exception:
                pass
            
            # 画红框
            from PIL import ImageDraw
            draw = ImageDraw.Draw(display_img)
            x0, y0 = drag_state['x'], drag_state['y']
            x1, y1 = x0 + img_w, y0 + img_h
            draw.rectangle([x0, y0, x1-1, y1-1], outline='red', width=2)
            
            # 显示坐标
            draw.text((x0+5, y0+5), f"({drag_state['x']}, {drag_state['y']})", fill='yellow')
            
            tk_img = ImageTk.PhotoImage(display_img)
            tk_img_holder['img'] = tk_img
            drag_canvas.delete("all")
            drag_canvas.create_image(0, 0, anchor='nw', image=tk_img)
        
        def on_drag_press(event):
            # 检查是否点击在图片范围内
            if (drag_state['x'] <= event.x <= drag_state['x'] + img_w and
                drag_state['y'] <= event.y <= drag_state['y'] + img_h):
                drag_state['dragging'] = True
                drag_state['start_mouse_x'] = event.x
                drag_state['start_mouse_y'] = event.y
                drag_state['start_img_x'] = drag_state['x']
                drag_state['start_img_y'] = drag_state['y']
                drag_canvas.config(cursor='fleur')
        
        def on_drag_motion(event):
            if drag_state['dragging']:
                dx = event.x - drag_state['start_mouse_x']
                dy = event.y - drag_state['start_mouse_y']
                new_x = max(0, min(BOARD_W - img_w, drag_state['start_img_x'] + dx))
                new_y = max(0, min(BOARD_H - img_h, drag_state['start_img_y'] + dy))
                drag_state['x'] = new_x
                drag_state['y'] = new_y
                redraw_drag()
        
        def on_drag_release(event):
            drag_state['dragging'] = False
            drag_canvas.config(cursor='')
        
        def apply_position():
            img_config['start_x'] = drag_state['x']
            img_config['start_y'] = drag_state['y']
            save_and_offer_restart(config)
            drag_win.destroy()
        
        drag_canvas.bind('<Button-1>', on_drag_press)
        drag_canvas.bind('<B1-Motion>', on_drag_motion)
        drag_canvas.bind('<ButtonRelease-1>', on_drag_release)
        
        btn_frame = ttk.Frame(drag_win)
        btn_frame.pack(pady=5)
        ttk.Button(btn_frame, text='✓ 应用', command=apply_position).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text='✗ 取消', command=drag_win.destroy).pack(side=tk.LEFT, padx=5)
        
        redraw_drag()
    
    ttk.Button(ctrl_btn_frame, text='🎯 拖动设置起点', command=open_drag_window).pack(side=tk.LEFT, padx=2)

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

        # 根据当前缩放和偏移进行变换
        scale = canvas_state['scale']
        if scale != 1.0:
            new_w = int(BOARD_W * scale)
            new_h = int(BOARD_H * scale)
            img = img.resize((new_w, new_h), Image.Resampling.NEAREST)
        
        tk_img = ImageTk.PhotoImage(img)
        cached['tk_img'] = tk_img
        canvas.delete("all")
        canvas.create_image(canvas_state['offset_x'], canvas_state['offset_y'], anchor='nw', image=tk_img)

    def constrain_offset():
        """限制偏移量，保证图像不会被无限拖出可视区域。"""
        try:
            scale = canvas_state['scale']
            disp_w = int(BOARD_W * scale)
            disp_h = int(BOARD_H * scale)
            vw = max(1, canvas_state.get('canvas_width', canvas.winfo_width()))
            vh = max(1, canvas_state.get('canvas_height', canvas.winfo_height()))

            # 最小和最大偏移
            min_x = min(0, vw - disp_w)
            max_x = 0
            min_y = min(0, vh - disp_h)
            max_y = 0

            if canvas_state['offset_x'] < min_x:
                canvas_state['offset_x'] = min_x
            if canvas_state['offset_x'] > max_x:
                canvas_state['offset_x'] = max_x
            if canvas_state['offset_y'] < min_y:
                canvas_state['offset_y'] = min_y
            if canvas_state['offset_y'] > max_y:
                canvas_state['offset_y'] = max_y
        except Exception:
            pass

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

        # 使用紧凑的多行显示
        info_text = f"进度: {pct:6.2f}% | 总: {total} | 未达标: {mismatched}\n"
        info_text += f"用户: {available} (就绪:{ready}) | 图片: {num_images}{res_str}\n{growth_str}"
        lbl_info.config(text=info_text)
        eta_lbl.config(text=eta_str)
        try:
            users_lbl.config(text=f"可用: {available} | 就绪: {ready}")
        except Exception:
            pass

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
