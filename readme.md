# LSP2025-drawer

轻量的 Paintboard 绘制器后端 + 简易前端预览（用于 LGS / Luogu Paintboard 活动）。

此仓库包含：
- `main.py`：程序主入口，负责加载配置、获取 Token、建立 WebSocket 连接并向画板发送绘制数据（支持粘包发送）。
- `gui.py`：基于 tkinter 的独立前端预览与控制界面（可切换绘制模式、拖动设置起点、查看进度）。
- `config.json`：程序配置（如果不存在，程序会在首次运行时生成一个示例配置）。
- `config.json.del`：仓库中原始配置示例，用于参考
- `promot.md`：项目相关的 API 说明与实现提示（包含接口、WebSocket 协议等）。

## 要求
- Python 3.8+
- 依赖库：
  - requests
  - websockets
  - pillow

安装依赖（建议在虚拟环境中运行）：

```powershell
python -m pip install -r requirements.txt
```

如果你没有 `requirements.txt`，也可以单独安装：

```powershell
python -m pip install requests websockets pillow
```

## 使用
- 仅命令行模式（不启用 GUI）

```powershell
python main.py -cli
```

- 启动 GUI 模式（默认）：

```powershell
python main.py
```

GUI 功能：
- 顶部显示实时画板预览（1000x600），30 秒重建一次底图；首次加载或后台快照填充时会立即更新。
- 下拉框切换绘制模式（`horizontal` / `concentric` / `random`），会即时生效并保存到 `config.json`。
- 点击“预览成果”按钮可以在预览上覆盖目标图像（透明像素保留）。
- 拖动窗口可调整目标起点（点击“拖动设置起点”打开拖拽弹窗，应用后会写入 `config.json`）。

注意：为了简洁，GUI 中不再提供用户添加/管理功能，请直接编辑 `config.json` 来添加用户（`uid` + `access_key`），或在 CLI 运行前准备好配置文件。

## 配置 (`config.json`)
示例文件会自动生成（或查看仓库中的 `config.json.del`）。主要字段：
- `users`: 数组，每项包含 `uid`（整数）与 `access_key`（字符串）。可选 `token` 字段用于直接指定 token（跳过获取）。
- `image_path`: 目标图片路径（建议为 PNG，带透明通道以实现透明覆盖）。
- `draw_mode`: 绘制顺序模式（`horizontal` / `concentric` / `random`）。
- `start_x`, `start_y`: 目标图片在画板上的起点坐标（0-based）。
- `paint_interval_ms`, `round_interval_seconds`, `user_cooldown_seconds`: 绘制/调度相关时间参数。

## 运行时行为
- 程序会为每个用户（有 `access_key` 的）尝试调用后端接口获取绘制 token；获取阶段会在 GUI 中显示进度条（或在控制台打印进度）。
- 若接口获取失败且 `users` 条目中提供了 `token` 字段，程序会回退使用该 token（发出警告）。
- 在无任何可用 token 的情况下：
  - 若使用 `-cli`，程序会退出并提示错误；
  - 若使用 GUI，程序会继续启动 GUI，允许你手动修改 `config.json` 并重新启动。

## 常见问题
- 获取 token 返回 404/未找到：请检查 `API_BASE_URL` 与 `get_token` 的实现（默认使用 POST `/api/auth/gettoken`），或使用正确的 `access_key`。
- 画板预览为空：后台 snapshot 请求失败或网络问题，请检查网络与日志（`paint.log`）。

## 开发
- 项目使用标准 Python 文件结构；可在本地修改 `config.json`、替换 `image.png`，然后运行程序进行测试。

---
如果你希望我把 `README.md` 调整为中文更详尽的安装/调试指引，或加入示例 `config.json` 模板片段，请告诉我需要哪些额外信息，我会继续完善。