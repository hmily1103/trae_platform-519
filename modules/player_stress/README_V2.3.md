# KTV播放器压测系统 - V2.3 电视端专项卡顿检测

## 🎯 核心改进

V2.3版本专门针对KTV双屏架构优化，**重点监控电视端（播放屏）卡顿**，不再被点歌屏（触摸屏）的UI负载干扰。

---

## 📊 三位一体电视端卡顿判定

### 维度一：硬件解码"步进"监控（最可靠）⭐

**实现位置**: `core/rk_monitor.py`

**原理**: 监控瑞芯微硬件解码器的 `total_work_count`（工作计数）增量

**判定逻辑**:
- 如果 `work_count` 在 2 秒内没有增加
- 且 `active_instances > 0`（解码器在运行）
- → **判定为解码器卡死（DECODER_STUCK）**

**优势**: 
- 这是最底层的指标，不受应用层干扰
- 直接反映硬件解码器是否在输出新帧

---

### 维度二：电视屏画面"视觉对比"（最直观）⭐

**实现位置**: `core/runner.py` + `core/image_analyzer.py`

**原理**: 自动抓取电视端截图，对比画面像素变化

**判定逻辑**:
- 连续 3 秒画面像素完全一致（相似度 > 99%）
- 且 `audio_active = True`（有声音在播放）
- → **判定为电视端画面冻结（TV_FREEZE）**

**优势**:
- 最直观的检测方式，直接看到画面是否静止
- 自动适配 Display 1 或 Display 2

---

### 维度三：Display 专项帧率获取（最精确）⭐

**实现位置**: `core/monitor.py`

**原理**: 采用三级回退策略（Tiered Strategy），确保在各种环境下都能获取FPS。

**策略逻辑**:
1. **Tier 1 (应用层)**: `gfxinfo` - 最通用，几乎所有设备支持。
2. **Tier 2 (合成层)**: `SurfaceFlinger` - 如果 gfxinfo 失败，查询合成层帧率。
3. **Tier 3 (硬件层)**: `MPP Decoder` - 如果前两者失败，直接根据解码器工作量计算 FPS（仅限 RK 平台）。

**优势**:
- **高鲁棒性**: 即使应用绕过 UI 线程（如 SurfaceView），也能通过 Tier 2/3 获取准确数据。
- **物理真理**: Tier 3 直接反映硬件解码速度，是最底层的“真理”。

---

## 🔧 自动检测功能

### Display ID 自动检测

系统会自动检测电视端 Display ID，支持以下配置：

- ✅ **只有 Display 1**: 自动使用 Display 1
- ✅ **有 Display 1 和 Display 2**: 自动选择 Display 1（最小的）
- ✅ **只有 Display 2**: 自动检测并使用 Display 2
- ✅ **检测失败**: 默认使用 Display 1

**检测方法**:
1. 通过 `dumpsys display` 扫描所有 Display ID
2. 过滤掉 Display 0（点歌屏）
3. 选择最小的非 0 Display ID

---

## 📈 报告优化

### 智能分析逻辑（优先级从高到低）

1. **电视端视频播放卡顿**（最严重）🚨
   - 解码器卡死 > 0 次 或 画面冻结 > 0 次
   - 直接判定为 **[严重警告]**

2. **视频播放卡顿**
   - 日志卡顿 > 5 次 或 视频 FPS < 15

3. **点歌屏UI渲染丢帧**（不影响视频）⚠️
   - UI Jank > 20% 且 无日志卡顿
   - **明确说明**: 仅反映点歌屏UI线程负载，不影响电视端视频质量

4. **播放流畅度整体良好**✅

### 报告中的关键指标

- `decoder_stuck_count`: 解码器卡死次数
- `tv_stutter_count`: 电视端卡顿次数
- `tv_freeze_count`: 电视端画面冻结次数
- `avg_video_fps`: 电视端平均帧率（来自 Display 1/2）

---

## 🚀 快速开始

### Windows 用户

```batch
# 双击运行
start_stress_test.bat
```

### Linux/Mac 用户

```bash
# 添加执行权限
chmod +x start_stress_test.sh

# 运行
./start_stress_test.sh
```

### 手动启动

```bash
# 切换到项目根目录
cd trae_platform

# 启动服务器
python app.py

# 浏览器访问
http://localhost:5000/player_stress/
```

---

## ⚙️ 环境要求

### 必需

- ✅ Python 3.7+
- ✅ ADB 已添加到 PATH
- ✅ 设备已通过 ADB 连接

### 推荐（用于完整功能）

- ✅ **Root 权限**: 用于硬件解码监控
  - 需要访问 `/sys/kernel/debug/mpp_service/stats`
  - 如果没有 Root，硬件解码监控将无法使用

### 检查 Root 权限

```bash
adb shell "su -c 'ls /sys/kernel/debug/mpp_service/stats'"
```

如果返回文件列表，说明 Root 权限可用。

---

## 🔍 验证 Display 配置

### 检查可用 Display

```bash
adb shell "dumpsys display | grep -i 'Display id='"
```

### 检查 SurfaceFlinger Surface

```bash
adb shell "dumpsys SurfaceFlinger --list"
```

查找包含以下关键字的 Surface：
- `SecondaryDisplay`
- `ExternalDisplay`
- `HDMI`
- `TV`

---

## 📝 使用说明

1. **启动系统**: 运行启动脚本或手动启动 Flask 服务器

2. **选择设备**: 在 Web 界面中选择要测试的设备

3. **配置参数**:
   - **包名**: 例如 `com.thunder.ktv:media`
   - **测试模式**: `monitor_only`（纯监控）或 `loop_playback`（循环播放）
   - **测试时长**: 建议至少 10 分钟

4. **开始测试**: 点击"开始压测"按钮

5. **查看报告**: 测试结束后，在 `reports/` 目录查看生成的报告

---

## 🐛 故障排查

### 问题1: 无法获取视频FPS

**可能原因**:
- 应用未启动或进程不存在
- gfxinfo 数据不可用（需要应用运行一段时间）
- 设备不支持 gfxinfo 统计

**解决方案**:
- 确保应用正在运行
- 等待一段时间让 gfxinfo 数据积累
- 查看日志中的详细错误信息

### 问题2: 硬件解码监控不可用

**可能原因**:
- 设备未开启 Root 权限
- MPP 节点路径不存在

**解决方案**:
- 开启 Root 权限
- 检查设备是否支持瑞芯微 MPP

### 问题3: 无法检测到电视端 Display

**可能原因**:
- Display ID 不是 1 或 2
- SurfaceFlinger 查询失败

**解决方案**:
- 手动运行 `adb shell dumpsys display` 查看实际 Display ID
- 检查设备是否支持多屏输出

---

## 📚 技术细节

### 代码结构

```
trae_platform/modules/player_stress/
├── core/
│   ├── monitor.py          # 性能监控核心（含Display检测）
│   ├── rk_monitor.py       # 硬件解码监控（work_count增量检测）
│   ├── runner.py           # 测试运行器（画面冻结检测）
│   └── image_analyzer.py   # 画面分析器（Display自动检测）
├── start_stress_test.bat   # Windows启动脚本
├── start_stress_test.sh    # Linux/Mac启动脚本
└── README_V2.3.md          # 本文档
```

### 关键方法

- `monitor.py::_detect_tv_display_id()`: 自动检测电视端 Display ID
- `rk_monitor.py::get_mpp_stats()`: 获取硬件解码统计（含增量检测）
- `runner.py::tv_screenshot_history`: 画面冻结检测逻辑
- `image_analyzer.py::_detect_tv_displays()`: 自动检测所有电视端 Display

---

## 📞 支持

如有问题或建议，请查看：
- 代码注释中的详细说明
- 日志输出中的调试信息
- 报告中的智能分析结论

---

**版本**: V2.3  
**更新日期**: 2025-01-08  
**作者**: AI Assistant
