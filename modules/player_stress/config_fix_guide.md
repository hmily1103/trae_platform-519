# KTV播放器FPS检测修复指南

## 🎯 问题描述

在KTV双屏系统中，`:media` 进程负责视频解码，主进程负责UI渲染和视频显示。如果配置监控 `:media` 进程，可能无法获取到有效的FPS数据。

## 🔧 解决方案

### V2.3.2 智能修复

代码已经修改为**智能双进程检测**：

1. **自动尝试主进程** (`com.thunder.ktv`)
2. **备用媒体进程** (`com.thunder.ktv:media`) 
3. **自动选择数据最丰富的进程**

### 配置建议

#### 方案1: 使用主进程（推荐）✅

```json
{
    "target_app": {
        "package_name": "com.thunder.ktv",
        "main_activity": "com.thunder.ktv.MainActivity"
    }
}
```

**优势**：
- 直接监控UI渲染进程
- 帧数据丰富，FPS计算准确
- 直接反映电视端视频显示效果

#### 方案2: 保持现有配置（兼容）⚠️

```json
{
    "target_app": {
        "package_name": "com.thunder.ktv:media"
    }
}
```

**说明**：
- 新版本会自动检测数据质量
- 如果 `:media` 进程帧数不足，自动回退到主进程
- 保持向后兼容性

## 🔍 验证方法

### 1. 运行测试脚本

```bash
cd modules/player_stress
python test_fps_fix.py
```

### 2. 手动验证

```bash
# 检查主进程帧数据
adb shell dumpsys gfxinfo com.thunder.ktv

# 检查媒体进程帧数据  
adb shell dumpsys gfxinfo com.thunder.ktv:media
```

**判断标准**：
- `Total frames rendered > 10` → 数据充足 ✅
- `Total frames rendered < 10` → 数据不足 ⚠️

### 3. 查看日志输出

新版本会输出详细的FPS检测日志：

```
[FPS] main process (com.thunder.ktv): 28.5fps (帧数:1737)
[FPS] media process (com.thunder.ktv:media): 跳过 (帧数:1)
[FPS] 最佳数据源: main(com.thunder.ktv), FPS: 28.5
```

## 📊 预期效果

### 修复前
```
[实时警告] 连续多次未能采集到有效视频FPS数据！
video_fps: 0.0
```

### 修复后
```
[FPS] main process (com.thunder.ktv): 28.5fps (帧数:1737)
[FPS] 最佳数据源: main(com.thunder.ktv), FPS: 28.5
video_fps: 28.5
```

## 🚀 使用建议

1. **新部署**：直接使用主进程包名 `com.thunder.ktv`
2. **现有系统**：保持现有配置，代码会自动优化
3. **问题排查**：运行测试脚本验证FPS检测效果

## 📝 技术细节

### 智能选择逻辑

```python
# 1. 尝试主进程
main_fps = get_fps("com.thunder.ktv")

# 2. 尝试媒体进程  
media_fps = get_fps("com.thunder.ktv:media")

# 3. 选择最佳数据源
if main_fps > 0 and main_frames > 10:
    return main_fps  # 优先主进程
elif media_fps > 0:
    return media_fps  # 备用媒体进程
else:
    return 0.0  # 都失败
```

### 数据质量检查

- **帧数阈值**：`Total frames rendered >= 10`
- **FPS范围**：`0 < fps < 120`
- **自动回退**：数据不足时尝试其他进程

---

**版本**: V2.3.2  
**更新日期**: 2025-01-21  
**修复内容**: 智能双进程FPS检测