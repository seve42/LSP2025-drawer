# LSP2025-drawer

轻量的 冬日绘板2026 绘制器后端 + 简易前端预览。

包含基于 tkinter 的独立前端预览与控制界面，多个绘制模式，良好性能。

## 要求
- Python 3.8+
- 依赖库：
  - requests
  - websockets
  - pillow

## 使用
- 仅命令行模式（不启用 GUI）

```powershell
python main.py -cli
```

- 启动 GUI 模式（默认）：

```powershell
python main.py
```


## 配置 (`config.json`)
示例文件会自动生成（或查看仓库中的 `config.json.del`）。主要字段：
- `users`: 数组，每项包含 `uid`（整数）与 `access_key`（字符串）。可选 `token` 字段用于直接指定 token（跳过获取）。
- `image_path`: 目标图片路径（建议为 PNG，带透明通道以实现透明覆盖）。
- `draw_mode`: 绘制顺序模式（`horizontal` / `concentric` / `random`），分别为扫描模式，中心扩展模式和随即撒点模式。
- `start_x`, `start_y`: 目标图片在画板上的起点坐标（0-based）。
- `paint_interval_ms`, `round_interval_seconds`, `user_cooldown_seconds`: 绘制/调度相关时间参数。
我需要哪些额外信息，我会继续完善。
