import json
import logging
from flask import Blueprint, render_template, request, jsonify
from utils.response import success_response, error_response
from utils.llm_client import call_llm

precision_test_bp = Blueprint(
    'precision_test',
    __name__,
    template_folder='templates',
    url_prefix='/'
)

logger = logging.getLogger(__name__)

@precision_test_bp.route('/')
def index():
    return render_template('precision_test_index.html')

@precision_test_bp.route('/api/analyze', methods=['POST'])
def analyze_code_diff():
    try:
        data = request.json
        if not data:
            return error_response("缺少请求数据")
            
        code_diff = data.get('code_diff', '').strip()
        project_type = data.get('project_type', 'backend') # backend or frontend
        
        if not code_diff:
            return error_response("代码 Diff 不能为空")
            
        # 构造大模型 Prompt
        prompt = f"""你是一个资深的测试架构师。
现在有一段 {project_type} 代码的 Git Diff（变更片段），你需要进行「精准回归测试推导」。

请你根据以下代码改动，推断：
1. 这段代码主要涉及了什么业务逻辑或底层组件？
2. 如果是后端，它可能影响哪些暴露出去的 API 接口？如果是前端，它可能影响哪些页面或组件交互？
3. 作为测试人员，在回归测试时应该重点关注哪些点？
4. 根据你的推断，请直接输出具体的【回归测试用例】，包括用例名称、前置条件、测试步骤和预期结果。

【代码 Diff 内容】：
```diff
{code_diff}
```

请使用 Markdown 格式输出结构化报告，包含以下章节：
### 🔧 变更逻辑分析
### 📡 爆炸半径预估（受影响的 API / UI）
### 🎯 回归测试策略建议
### 🧪 推荐回归测试用例 (表格形式展示)
"""

        # 调用大模型 (复用现有的 call_llm)
        response_text = call_llm(messages=[{"role": "user", "content": prompt}])
        
        return success_response({
            "impact_report": response_text
        })
        
    except Exception as e:
        logger.exception(f"分析代码变更失败: {e}")
        return error_response(f"系统异常: {str(e)}")
