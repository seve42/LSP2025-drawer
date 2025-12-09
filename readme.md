# LSP2025-drawer

洛谷画板 2025 自动绘图工具

## 功能特性

- 🎨 多图片并发绘制
- 🔄 多种绘制模式（随机、水平扫描、同心圆扩散）
- 🔧 灵活的配置系统
- 📊 实时进度展示
- 🔐 多账号支持与自动 Token 管理
- 🧵 多线程/多进程并发优化
- 🎯 智能像素调度与优先级管理
- 🖌️ 手动绘画模式（实时交互绘制）
- 🧪 Token 效率测试与敌对势力分析
- ⚡ 性能基准测试工具

## 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
```

### 配置文件

编辑 `config.json` 配置您的绘图任务：

```json
{
  "users": [
    {
      "uid": 123456,
      "access_key": "your_access_key_here"
    }
  ],
  "images": [
    {
      "enabled": true,
      "image_path": "image.png",
      "start_x": 100,
      "start_y": 100,
      "draw_mode": "random",
      "weight": 1.0
    }
  ]
}
```

### 运行模式

#### 1. 默认模式（WebUI）

启动 Web 界面，默认端口 80：

```bash
python main.py
```

#### 2. CLI 模式

命令行模式，无 Web 界面：

```bash
python main.py -cli
```

#### 3. 手动模式

手动绘板模式，强制单线程：

```bash
python main.py -hand
```

启动交互式绘图界面，支持：
- 🎨 实时选色和绘制
- 🖱️ 鼠标点击绘制像素
- ⌨️ 快捷键操作（空格刷新、B切换画笔、S保存等）
- 👁️ 实时画板预览与更新
- 🔄 自动同步服务器状态

#### 4. Token 测试模式

测量指定区域的敌对势力 token 数量和绘制效率：

```bash
python main.py -test
```

功能说明：
- 📊 测量我方 token 的实际绘制效率
- 🎯 分析敌对势力的 token 投入
- 📈 稳态平衡模型计算
- 🔬 多次测量支持线性回归分析
- 💡 提供战术建议（增援/撤退/维持）

#### 5. 调试模式

启用详细日志输出：

```bash
python main.py -debug
# 可组合使用
python main.py -cli -debug
```

## 配置说明

### 用户配置 (`users`)

| 字段 | 类型 | 说明 |
|-----|-----|-----|
| `uid` | number | 洛谷用户 ID |
| `access_key` | string | AccessKey（从洛谷获取）|

### 图片配置 (`images`)

| 字段 | 类型 | 默认值 | 说明 |
|-----|-----|--------|-----|
| `enabled` | boolean | true | 是否启用此图片 |
| `image_path` | string | - | 图片文件路径 |
| `start_x` | number | 0 | 起始 X 坐标 |
| `start_y` | number | 0 | 起始 Y 坐标 |
| `draw_mode` | string | "random" | 绘制模式 |
| `weight` | number | 1.0 | 优先级权重（越大越优先）|
| `scan_mode` | string | "normal" | 扫描模式 |

#### 绘制模式 (`draw_mode`)

- **random**: 随机顺序绘制像素（默认，推荐）
- **horizontal**: 从左到右、从上到下逐行扫描
- **concentric**: 从中心向外扩散绘制

#### 扫描模式 (`scan_mode`)

- **normal**: 默认模式。绘制失败优先重试，检测到被覆盖放到队尾
- **strict**: 严格模式。绘制失败或被覆盖，均优先重试（插入队首）
- **loop**: 循环模式。绘制失败或被覆盖，均放到队尾重新排队

### 全局配置

| 字段 | 类型 | 默认值 | 说明 |
|-----|-----|--------|-----|
| `max_workers` | number | 4 | 并发工作线程数 |
| `multi_process` | boolean | false | 是否启用多进程模式 |
| `process_count` | number | 1 | 进程数（仅多进程模式）|
| `web_port` | number | 80 | WebUI 端口 |

## 项目结构

```
LSP2025-drawer/
├── main.py              # 主程序入口
├── tool.py              # 核心工具函数（像素处理、合并算法）
├── ping.py              # 网络测试工具
├── proxy.py             # 代理服务器
├── hand_paint.py        # 手动绘制工具（交互式绘图界面）
├── test_token.py        # Token 效率测试与敌对势力分析
├── bench_paint.py       # 性能基准测试工具
├── config.json          # 配置文件
├── requirements.txt     # 依赖列表
├── start.ps1           # Windows 启动脚本
├── scripts/            # 辅助脚本目录
├── 文档.md              # API 文档
└── readme.md           # 本文件
```

## API 文档

详细的 API 文档请参考 `文档.md`。

主要 API 包括：

- **GET** `/api/paintboard/getboard` - 获取画板当前状态
- **POST** `/api/auth/gettoken` - 获取绘图 Token
- **WebSocket** `wss://paintboard.luogu.me/api/paintboard/ws` - 实时绘图通信

## 高级特性

### 手动绘画模式

使用 `python main.py -hand` 启动交互式绘图界面。

**功能特性：**
- 🎨 32 色调色板，快速选色
- 🖱️ 鼠标点击实时绘制
- 🔄 自动同步服务器画板状态
- 👁️ 实时预览绘制效果
- ⌨️ 丰富的快捷键支持

**快捷键：**
- `空格` - 刷新画板
- `B` - 切换画笔工具
- `S` - 保存当前画板
- `↑/↓` - 调整画笔大小
- `鼠标左键` - 绘制像素
- `鼠标滚轮` - 缩放画板

### Token 效率测试

使用 `python main.py -test` 进行战术分析。

**测试原理：**
基于稳态平衡模型，通过实际测量计算：
- ✅ 我方 token 的实际绘制效率
- 🎯 敌方投入的 token 数量估算
- 📊 区域占据率与对抗强度分析
- 💡 战术建议（增援/撤退/维持当前投入）

**测试流程：**
1. 在 `config.json` 中配置测试区域
2. 运行测试模式，系统自动测量
3. 分析输出的效率和敌方 token 估算
4. 可进行多次测量以提高准确性

**测量公式：**
```
稳态时：R_m * (1-p) = R_e * p
其中：R_m = N_m * η_m / CD （我方有效绘制速率）
     R_e = N_e * η_e / CD （敌方有效绘制速率）
     p = 我方占据率
=> N_e = N_m * (η_m / η_e) * (1-p) / p
```

### 性能基准测试

使用 `python bench_paint.py` 测试系统性能。

**测试指标：**
- 📦 数据包生成速率（ops/s）
- 🔗 数据合并耗时（ms）
- 💾 内存使用效率

这有助于评估在当前硬件上的最大绘制吞吐量。

### 多账号并发

配置多个用户以提高绘制速度：

```json
{
  "users": [
    {"uid": 123456, "access_key": "key1"},
    {"uid": 789012, "access_key": "key2"}
  ]
}
```

### 优先级管理

使用 `weight` 字段控制图片绘制优先级：

```json
{
  "images": [
    {"image_path": "high_priority.png", "weight": 10.0},
    {"image_path": "low_priority.png", "weight": 1.0}
  ]
}
```

### 性能优化

1. **多线程模式**（默认）：适合 I/O 密集型任务
   ```json
   {"multi_process": false, "max_workers": 8}
   ```

2. **多进程模式**：适合 CPU 密集型任务
   ```json
   {"multi_process": true, "process_count": 4}
   ```

## 日志与监控

- `paint.log` - 主程序日志
- `last.log` - 最近一次运行日志
- `test_ping.log` - 网络测试日志

## 常见问题

### Token 无效

确保 `access_key` 配置正确，Token 会自动刷新。

### 绘制速度慢

1. 增加 `max_workers` 数量
2. 使用多账号并发
3. 选择合适的绘制模式（推荐 `random`）

### 像素被覆盖

使用 `strict` 扫描模式以优先重试被覆盖的像素。

## 开发与贡献

### 代码结构

- `main.py` - 主调度逻辑、WebSocket 处理、Web UI
- `tool.py` - 图片处理、像素映射、目标合并算法
- `hand_paint.py` - 交互式手动绘图界面（基于 pygame）
- `test_token.py` - Token 效率测试与战术分析工具
- `bench_paint.py` - 性能基准测试（测量数据包处理速度）
- 其他工具脚本 - 辅助功能

### 测试与工具

```bash
# 性能基准测试（测试数据包生成和合并速度）
python bench_paint.py

# 网络延迟测试
python ping.py

# Token 效率测试（需先配置测试区域）
python main.py -test

# 手动绘图模式（交互式界面）
python main.py -hand
```

## 许可证

本项目仅供学习交流使用，请勿用于违反洛谷服务条款的行为。

## 致谢

感谢洛谷提供的画板平台以及社区的支持。

---

**注意**: 使用本工具时请遵守洛谷社区规范，合理使用 API，避免对服务器造成过大负担。
