# LSP2025-drawer

洛谷画板 2025 自动绘图工具

## 功能特性

-  多图片并发绘制
-  多种绘制模式
-  灵活的配置系统
-  手动绘画模式（实时交互绘制）
-  Token 效率测试

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

#### 1. 默认模式

```bash
python main.py
```

#### 2. 手动模式

手动绘板模式，强制单线程：

```bash
python main.py -hand
```

启动交互式绘图界面，支持：
-  实时选色和绘制
-  鼠标点击绘制像素
-  实时画板预览与更新
-  自动同步服务器状态

#### 3. Token 测试模式

测量指定区域的敌对势力 token 数量和绘制效率：

```bash
python main.py -test
```


## 配置说明

### 用户配置 (`users`)

| 字段 | 类型 | 说明 |
|-----|-----|-----|
| `uid` | number | 洛谷用户 ID |
| `access_key` | string | AccessKey |

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


## 其他功能

### 手动绘画模式

使用 `python main.py -hand` 启动交互式绘图界面。

**功能特性：**
-  32 色调色板，快速选色
-  鼠标点击实时绘制
-  自动同步服务器画板状态
-  实时预览绘制效果

**快捷键：**
- `空格` - 刷新画板
- `B` - 切换画笔工具
- `S` - 保存当前画板
- `↑/↓` - 调整画笔大小
- `鼠标左键` - 绘制像素
- `鼠标滚轮` - 缩放画板

### Token 效率测试

使用 `python main.py -test` 进行分析。

**测试原理：**
基于稳态平衡模型，通过实际测量计算：
- ✅ 我方 token 的实际绘制效率
- 🎯 对方投入的 token 数量估算

**测试流程：**
1. 在 `config.json` 中配置测试区域
2. 运行测试模式，系统自动测量
3. 分析输出的效率和敌方 token 估算
4. 可进行多次测量以提高准确性


**配置多个用户 **

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


## 日志与监控

- `last.log` - 最近一次运行日志



## 许可证

All Rights Reserved (保留所有权利)

禁止修改，再分发

本项目仅供学习交流使用.
