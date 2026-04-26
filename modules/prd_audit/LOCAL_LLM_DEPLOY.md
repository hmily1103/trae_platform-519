# 本地免费大模型部署（Windows）——用于 PRD 审计 / 学习MVP

目标：在本机部署开源模型（不按调用计费），让 PRD 审计在开启 `使用大模型` 时调用本地模型，同时不影响你离线批量训练与学习闭环。

本项目的 LLM 客户端支持 **OpenAI 兼容接口**，因此最省事的本地部署方式是：

- **Ollama（推荐）**：开箱即用，提供 OpenAI 兼容 `.../v1/chat/completions`
- **LM Studio（备选）**：有 GUI，适合不想敲命令的同学，也提供 OpenAI 兼容接口

---

## 1. 方案 A：Ollama（推荐）

### 1.1 安装 Ollama

1. 访问官网下载安装（Windows）：https://ollama.com/
2. 安装后一般会自动启动服务（后台监听端口 `11434`）

### 1.2 拉取一个中文模型（免费）

建议从 7B 开始（更容易跑得动）：

- `qwen2.5:7b`（中文综合推荐，速度/效果平衡）

在 PowerShell 里执行：

```powershell
ollama pull qwen2.5:7b
```

如果你机器显存/内存较充足，可再试：

```powershell
ollama pull qwen2.5:14b
```

### 1.3 验证本地服务可用

#### 1）看模型列表

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:11434/api/tags
```

#### 2）验证 OpenAI 兼容接口

```powershell
$body = @{
  model = "qwen2.5:7b"
  messages = @(
    @{ role = "user"; content = "用一句话总结：PRD需要包含哪些关键要素？" }
  )
} | ConvertTo-Json -Depth 10

Invoke-WebRequest -UseBasicParsing `
  -Uri "http://127.0.0.1:11434/v1/chat/completions" `
  -Method POST `
  -ContentType "application/json" `
  -Body $body
```

能返回 JSON 即表示通了。

---

## 2. 方案 B：LM Studio（备选）

### 2.1 安装与启动

1. 安装 LM Studio（Windows）：https://lmstudio.ai/
2. 在 LM Studio 里下载一个 Instruct 模型（建议 Qwen2.5 7B Instruct）
3. 打开 “Local Server”，启动 OpenAI Compatible Server

默认监听通常是：

- `http://127.0.0.1:1234/v1`

### 2.2 验证

同样用 `/v1/chat/completions` 试一次请求即可。

---

## 3. 接入本项目（PRD 审计）

本项目的 LLM 配置入口在：

- PRD 审计页 -> LLM 配置（会写入 `modules/test_case/llm_config.json`）
- 或直接编辑 `modules/test_case/llm_config.json`

### 3.1 推荐配置（Ollama）

在页面配置里填：

- llm_provider：`openai`
- base_url：`http://127.0.0.1:11434/v1`
- model：`qwen2.5:7b`
- api_key：随便填一个非空字符串即可，例如 `local`

说明：

- Ollama 本地接口不校验 key，但本项目会校验 “api_key 不能为空”，所以填一个占位即可。

### 3.2 推荐配置（LM Studio）

- llm_provider：`openai`
- base_url：`http://127.0.0.1:1234/v1`
- model：使用你在 LM Studio 里加载的模型名
- api_key：`local`

---

## 4. 怎么使用（不花钱但有 LLM 能力）

1. 先按上面部署好 Ollama/LM Studio
2. 在 PRD 审计页配置好 LLM（base_url 指向本地）
3. 分析 PRD 时勾选 `使用大模型`

你会得到：

- L1/L2/L3 均可生成（LLM 优先；LLM 不可用时会回退到本地）
- 学习MVP 看板里会逐步出现 `hybrid` 或 `llm_only` 轨道样本

---

## 5. 常见问题

### Q1：为什么还是提示 “本地规则体检版，无大模型推理”？

说明 LLM 调用没成功，常见原因：

- base_url 填错（Ollama 是 `11434`，LM Studio 常是 `1234`）
- 模型名填错（Ollama 需要先 `ollama pull`，并且 model 要写对）
- 本机服务没启动（先访问 `.../api/tags` 确认服务在）

### Q2：我想批量训练多个 PRD，但不想启 LLM

用离线批量脚本（不调用大模型）：

```powershell
python -m modules.prd_audit.batch_train_offline --input-dir <DIR> --glob "**/*.txt"
```

---

