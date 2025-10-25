# LSP2025-drawer

轻量的 冬日绘板2026 绘制器后端 + WebUI 界面。

包含基于 Flask 的 WebUI 界面，支持多个绘制模式，良好性能，可远程访问。

功能包含

- 多个PNG图片绘制，跳过透明图层，可为每个图片指定绘制模式，绘制权重
- 自适应任务分配，实时计算绘制进度和抵抗率（画下图片被覆盖的占比，例如抵抗率50%且你有100token，那么就有50个token正在抵抗你）与绘制效率。
- 图片自由拖动设置起点，WebUI 实时预览图片绘制成果
- 多个UID+Key token批量获取与绘制
- 攻击功能，支持指定范围进行随机撒白点/绿点/彩点攻击，同样可以指定权重
- 同时支持 CLI 和 WebUI 两种模式

## 要求
- Python 3.8+
- 基础依赖库：
  - requests
  - websockets
  - pillow
- WebUI 依赖：
  - Flask
  - flask-socketio
  - python-socketio

## 使用

### WebUI 模式（推荐，默认）

**安装依赖：**
```powershell
pip install -r requirements_webui.txt
```

**启动 WebUI：**
```powershell
python main.py
```

或使用快捷脚本（默认 8080 端口）：
```powershell
start_webui.bat
```

自定义端口：
```powershell
python main.py -port 8080
```

**访问 WebUI：**
- 本地访问：http://localhost:80 或 http://localhost:8080
- 远程访问：http://你的IP地址:端口

详细说明请查看 [WEBUI_README.md](WEBUI_README.md)

### 仅命令行模式

```powershell
python main.py -cli
```

### 调试模式

```powershell
python main.py -debug
```


## 配置 (`config.json`)
示例文件会自动生成（或查看仓库中的 `config.json.del`）。主要字段：
- `users`: 数组，每项包含 `uid`（整数）与 `access_key`（字符串）。可选 `token` 字段用于直接指定 token（跳过获取）。
- `image_path`: 目标图片路径（建议为 PNG，带透明通道以实现透明覆盖）。
- `draw_mode`: 绘制顺序模式（`horizontal` / `concentric` / `random`），分别为扫描模式，中心扩展模式和随即撒点模式。
- `start_x`, `start_y`: 目标图片在画板上的起点坐标（0-based）。
- `paint_interval_ms`, `round_interval_seconds`, `user_cooldown_seconds`: 绘制/调度相关时间参数。
我需要哪些额外信息，我会继续完善。
