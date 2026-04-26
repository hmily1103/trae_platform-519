# PRD 审计功能检查清单

以下为 PRD 审计（含三段式分析）全链路检查结果与要点。

---

## 一、入口与路由

| 项目 | 状态 | 说明 |
|------|------|------|
| 独立页 `/prd_audit/` | ✅ | `modules/prd_audit/views.py` index，模板 `prd_audit_index.html` |
| 用例管理内 PRD 入口 | ✅ | `/test_case/` → 选择「PRD 漏洞分析」卡片 |
| 侧栏「PRD 审计」 | ✅ | `base.html` 用例管理分组下首位 |
| prd_audit 注册 | ✅ | `app.py` 在 test_case 之后注册 `prd_audit_bp` |

---

## 二、三段式分析链路（test_case）

| 阶段 | 实现位置 | 输入 | 输出 | 说明 |
|------|----------|------|------|------|
| Stage1 结构解析 | `system_model.extract_prd_structure` | prd_text, llm_config_path | PRD_STRUCTURE_SCHEMA 的 JSON | STAGE1_STRUCTURE_PROMPT + `{content}`，角色：产品/测试架构师 |
| Stage2 漏洞扫描 | `prd_rule_engine.run_stage2_defect_scan` | stage1_output, prd_text, llm_config_path | `{ defects: [] }` | 规则库 prd_scan_rules.json + LLM STAGE2_DEFECT_SCAN_PROMPT，角色：10 年测试架构师 |
| Stage3 报告生成 | `views._run_stage3_llm_report` 或 `_build_stage3_report` + `_render_stage4_markdown` | prd_content, stage1, stage2, llm_config_path | Markdown 字符串 | 若存在 `prd_audit_prompt_stage3_minimal.txt` 则 LLM 八段报告（max_tokens=16384），否则 Python 六段 |

**触发路径**：

- 流式：`POST /test_case/api/generate` 或 `POST /prd_audit/api/generate`，body `{ type: 'prd_review', content: string }`
- 非流式：`POST /test_case/api/analyze_prd` 或 `POST /prd_audit/api/analyze_prd`，body `{ prd_text }` 或 `{ content }`

---

## 三、配置与文件

| 文件/配置 | 路径 | 用途 |
|-----------|------|------|
| LLM 配置 | `modules/test_case/llm_config.json` | 全平台 PRD 与用例生成共用 |
| Stage1 prompt | `system_model.STAGE1_STRUCTURE_PROMPT` | 结构解析 |
| Stage2 prompt | `prd_rule_engine.STAGE2_DEFECT_SCAN_PROMPT` | 漏洞扫描 |
| Stage3 prompt | `modules/test_case/prd_audit_prompt_stage3_minimal.txt` | 八段报告，占位符 `{prd_content}` `{structure_json}` `{defects_json}` |
| 默认单次 prompt | `modules/test_case/prd_audit_prompt_default.txt` | 自定义提示词 / 单次调用时使用 |
| 规则库 | `modules/test_case/prd_scan_rules.json` | Stage2 规则命中 |

---

## 四、API 与委托关系

| 接口 | test_case | prd_audit | 说明 |
|------|------------|-----------|------|
| GET / | 用例管理首页 | 独立 PRD 页 | 不同模板 |
| POST .../api/generate | 实现 | 委托 test_case | 需传 type=prd_review, content |
| POST .../api/analyze_prd | 实现 | 委托 test_case | 非流式，返回完整 JSON |
| POST .../api/parse_pdf | 实现 | 委托 test_case | FormData file |
| POST .../api/parse_docx | 实现 | 委托 test_case | FormData file |
| POST .../api/export_report_docx | 实现 | 委托 test_case | JSON { content } 或 { content, stage3_json } |
| GET .../api/default_prd_prompt | 实现 | 委托 test_case | - |
| GET/POST .../api/llm_config | 实现 | 委托 test_case | - |

prd_audit 与 test_case 共用同一 request（同一 body/files），委托调用无额外参数传递。

---

## 五、前端行为（prd_audit 独立页）

| 功能 | 实现 | 说明 |
|------|------|------|
| 输入 | textarea + 上传 PDF/Word | 选择文件后 POST parse_pdf 或 parse_docx，结果填入 textarea |
| 上传反馈 | showStatus('正在解析 xxx…')，finally 里恢复 | 解析失败 alert，成功则写入输入框 |
| 开始分析 | POST /prd_audit/api/generate，body { type:'prd_review', content } | 显示「PRD 三段式分析中…」，等待完整 NDJSON 后解析 |
| 流式解析 | 按行解析 type=status/content/error | 展示区含状态行；复制/下载 .md/.docx 使用仅 content 的 body，避免带「Stage1：…」等前缀 |
| 复制 / 下载 .md / 下载 .docx | lastReport（仅 content） | 导出为纯报告正文 |
| 保存为测试用例 | 链接 /test_case/?from_prd=1#prd_review | 跳转用例管理 PRD 分析位 |
| 错误展示 | !r.ok 时 throw，catch 中 outputEl.textContent = '[请求失败] ' + message | 4xx/5xx 会显示后端 message |

---

## 六、飞书与单次调用

| 项目 | 说明 |
|------|------|
| 飞书链接 | api_generate 内若 type==prd_review 且 content 为飞书 URL，先调 feishu_client.fetch_feishu_doc_content，用拉取正文作为 content |
| 单次调用 | 前端传 custom prompt 且 prompt 非空时，不走三段式，一次 call_llm(prompt.replace('{content}', content))，流式返回同一格式 |

---

## 七、依赖与异常

| 依赖 | 说明 |
|------|------|
| test_case 模块 | prd_audit 仅做路由与页面，逻辑全在 test_case；未加载 test_case 时 prd_audit 会导入失败 |
| LLM | 未配置或 API Key 为空时，api_generate 返回 400，前端显示「API Key 未配置」等 message |
| Stage3 文件 | 若 `prd_audit_prompt_stage3_minimal.txt` 不存在或无效，自动回退到 Python _build_stage3_report + _render_stage4_markdown |
| PDF 解析 | 需 pypdf 或 PyPDF2；否则 500 并提示安装 |
| Word 导出 | 需 python-docx；否则 500 |

---

## 八、建议自测步骤

1. **上传**：在 `/prd_audit/` 点击「上传 PDF/Word」，选 PDF 或 docx → 应出现「正在解析…」且输入框有内容或明确错误提示。
2. **分析**：输入框有内容后点「开始分析」→ 应出现「PRD 三段式分析中…」，最终报告区有八段或六段报告。
3. **导出**：报告生成后点复制、.md、.docx → 内容应为报告正文（无 Stage1/2/3 状态行）。
4. **用例管理**：同一操作在 `/test_case/` 选「PRD 漏洞分析」再执行一遍，行为一致；「保存为测试用例」跳转到用例管理。
5. **错误**：清空或删掉 llm_config.json 的 api_key 再点「开始分析」→ 报告区应显示「[请求失败] API Key 未配置」或类似后端 message。

---

*检查清单版本：与当前代码一致*
