# Git 入门指南 - 对你有什么用

## 一、Git 是什么？对你有什么用？

Git 是一个**版本管理工具**，简单说就是：**给你的代码做「存档」**。

### 对你有什么用？

| 场景 | 不用 Git | 用 Git |
|------|----------|--------|
| 改坏了代码 | 只能手动恢复或重写 | `git checkout` 一键回到之前版本 |
| 想试试新功能 | 不敢改，怕回不去 | 随时改，不行就回滚 |
| 换电脑/重装系统 | 代码可能丢 | 代码在 Git 里，随时拉回来 |
| 多人协作 | 发压缩包、U盘拷 | 推送到仓库，别人直接拉 |
| 看历史改动 | 记不清改了什么 | `git log` 一目了然 |

**一句话**：Git 让你**不怕改坏、不怕丢、不怕换电脑**。

---

## 二、本地 Git 最常用的 3 个命令

### 1. 保存当前改动（提交）

```bash
# 第一步：把改动的文件「放进暂存区」
git add .

# 第二步：写一句说明，正式保存
git commit -m "添加了日志分析 Agent 功能"
```

`git add .` = 选中所有改动的文件  
`git commit -m "xxx"` = 保存这一版，并写上备注

### 2. 查看状态

```bash
git status
```

会告诉你：哪些文件改了、有没有提交、当前在哪个分支。

### 3. 查看历史

```bash
git log --oneline
```

会列出所有「存档点」，例如：

```
a1b2c3d 添加了日志分析 Agent 功能
e4f5g6h 配置 Gemini
...
```

---

## 三、你的项目已经初始化了 Git

你的 `trae_platform` 项目已经有 Git，可以直接用。

### 日常流程（改完代码后）

```bash
cd d:\trae-code\trae_platform

# 1. 看看改了什么
git status

# 2. 全部加入暂存
git add .

# 3. 提交并写说明
git commit -m "你的改动说明，比如：修复了XX bug"
```

---

## 四、进阶：回滚到上一个版本

如果改坏了，想回到上一次提交的状态：

```bash
# 放弃所有未提交的改动（慎用！）
git checkout -- .
```

或者回到某一次提交：

```bash
git log --oneline   # 先看历史，记下想回的版本号
git checkout 版本号  # 例如 git checkout a1b2c3d
```

---

## 五、可选：推送到远程（GitHub / GitLab / 内网 Git）

如果想把代码备份到网上或给同事用：

```bash
# 1. 在 GitHub/GitLab 创建仓库，拿到地址

# 2. 添加远程仓库
git remote add origin https://github.com/你的用户名/trae_platform.git

# 3. 推送
git push -u origin master
```

同事要拉代码：

```bash
git clone https://github.com/你的用户名/trae_platform.git
cd trae_platform
pip install -r requirements.txt
python app.py
```

---

## 六、建议：先忽略这些文件

`__pycache__`、`logs`、`*.pyc` 等不需要版本管理，已通过 `.gitignore` 排除。

---

## 七、速查表

| 命令 | 作用 |
|------|------|
| `git status` | 查看当前状态 |
| `git add .` | 暂存所有改动 |
| `git commit -m "说明"` | 提交保存 |
| `git log --oneline` | 查看历史 |
| `git checkout -- .` | 放弃未提交的改动 |
| `git push` | 推送到远程（需先配置 remote） |
| `git pull` | 从远程拉取最新代码 |
