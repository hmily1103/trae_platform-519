import os

def rewrite_html_file(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    start_render = content.find('    function renderMainOutline() {')
    end_render = content.find('    function renderKnowledgeCards() {', start_render)
    if start_render == -1 or end_render == -1:
        print(f'Error finding functions in {file_path}')
        return
    
    new_code = r"""    function renderMainOutline() {
        if (!contentOutlineMainWrapEl || !contentOutlineMainContentEl) return;
        renderLlmFourPillarsBlock();
        renderSharedSummaryMain();
        
        if (!hasLlmPayload) {
            contentOutlineMainWrapEl.style.display = 'none';
            return;
        }
        
        var ruleModel = (outlineEngine && outlineEngine.rule_model && typeof outlineEngine.rule_model === 'object') ? outlineEngine.rule_model : {};
        var systemModel = (outlineEngine && outlineEngine.system_model && typeof outlineEngine.system_model === 'object') ? outlineEngine.system_model : {};
        var explicitOutline = (outlineEngine && outlineEngine.explicit_outline && typeof outlineEngine.explicit_outline === 'object') ? outlineEngine.explicit_outline : {};
        if (!explicitOutline || Object.keys(explicitOutline).length === 0) {
            explicitOutline = (systemModel && systemModel.explicit_outline && typeof systemModel.explicit_outline === 'object') ? systemModel.explicit_outline : {};
        }
        
        var html = '<div class="fw-semibold mb-3 fs-5">需求认知拉齐稿（评审会版）</div>';
        
        var eBizMd = Array.isArray(explicitOutline.business_summary) ? explicitOutline.business_summary : [];
        var eRolesMd = Array.isArray(explicitOutline.roles) ? explicitOutline.roles : [];
        var eFlowMd = Array.isArray(explicitOutline.main_flow) ? explicitOutline.main_flow : [];
        var ePendingMd = Array.isArray(explicitOutline.pending_list) ? explicitOutline.pending_list : [];
        
        html += '<div class="fw-semibold mt-3 mb-2">一、系统目标</div>';
        if (eBizMd.length) {
            html += '<div class="mb-2">' + escapeHtml(eBizMd.join('；')) + '</div>';
        } else {
            html += '<div class="mb-2">根据业务优先级动态展示内容，并处理各功能之间打断、恢复、切换逻辑。</div>';
        }
        
        html += '<div class="fw-semibold mt-3 mb-2">二、本期范围</div><ul class="mb-2">';
        if (eRolesMd.length) {
            eRolesMd.forEach(function(r) { html += '<li>' + escapeHtml(String(r)) + '</li>'; });
        } else {
            html += '<li>暂无识别范围</li>';
        }
        html += '</ul>';
        
        html += '<div class="fw-semibold mt-3 mb-2">三、统一优先级（需确认）</div>';
        html += '<div class="p-2 bg-light border rounded mb-2 font-monospace text-break">';
        if (Array.isArray(ruleModel.priority_chain) && ruleModel.priority_chain.length) {
            html += escapeHtml(ruleModel.priority_chain.join(' > '));
        } else {
            html += '暂无识别的优先级';
        }
        html += '</div>';
        
        html += '<div class="fw-semibold mt-3 mb-2">四、核心流程</div>';
        if (eFlowMd.length) {
            eFlowMd.forEach(function(f) { html += '<div class="mb-1">' + escapeHtml(String(f)) + '</div>'; });
            html += '<div class="mb-2"></div>';
        } else {
            html += '<div class="fw-semibold mb-1">正常流程</div><div class="mb-2">暂无识别流程</div>';
        }
        
        html += '<div class="fw-semibold mt-3 mb-2">五、本次会议待拍板问题（最重要）</div><ol class="mb-2">';
        if (ePendingMd.length) {
            ePendingMd.forEach(function(p) { html += '<li>' + escapeHtml(String(p)) + '</li>'; });
        } else {
            html += '<li>暂无待拍板问题</li>';
        }
        html += '</ol>';
        
        html += '<div class="fw-semibold mt-3 mb-2">六、当前风险</div>';
        html += '<div class="mb-1">若不定规则直接开发：</div>';
        html += '<ul class="mb-2"><li>前后端理解不一致</li><li>测试无法验收</li><li>联调返工高</li><li>上线后展示混乱</li></ul>';
        
        html += '<div class="fw-semibold mt-3 mb-2">七、输出物</div>';
        html += '<div class="mb-1">会议结束后补齐：</div>';
        html += '<ul class="mb-2"><li>状态机图</li><li>优先级规则表</li><li>异常流程表</li><li>验收标准</li></ul>';
        
        if (contentOutlineMainMetaEl) contentOutlineMainMetaEl.style.display = 'none';
        
        contentOutlineMainContentEl.innerHTML = html;
        contentOutlineMainWrapEl.style.display = 'block';
    }

    function buildOutlineReportMarkdown() {
        var lines = ['# 需求认知拉齐稿（评审会版）', ''];
        
        var ruleModel = (outlineEngine && outlineEngine.rule_model && typeof outlineEngine.rule_model === 'object') ? outlineEngine.rule_model : {};
        var explicitOutline = (outlineEngine && outlineEngine.explicit_outline && typeof outlineEngine.explicit_outline === 'object') ? outlineEngine.explicit_outline : {};
        var systemModel = (outlineEngine && outlineEngine.system_model && typeof outlineEngine.system_model === 'object') ? outlineEngine.system_model : {};
        if (!explicitOutline || Object.keys(explicitOutline).length === 0) {
            explicitOutline = (systemModel && systemModel.explicit_outline && typeof systemModel.explicit_outline === 'object') ? systemModel.explicit_outline : {};
        }

        var eBizMd = Array.isArray(explicitOutline.business_summary) ? explicitOutline.business_summary : [];
        var eRolesMd = Array.isArray(explicitOutline.roles) ? explicitOutline.roles : [];
        var eFlowMd = Array.isArray(explicitOutline.main_flow) ? explicitOutline.main_flow : [];
        var ePendingMd = Array.isArray(explicitOutline.pending_list) ? explicitOutline.pending_list : [];
        
        lines.push('## 一、系统目标');
        if (eBizMd.length) {
            lines.push(eBizMd.join('；'));
        } else {
            lines.push('根据业务优先级动态展示内容，并处理各功能之间打断、恢复、切换逻辑。');
        }
        lines.push('');
        
        lines.push('## 二、本期范围');
        if (eRolesMd.length) {
            eRolesMd.forEach(function(r) { lines.push('* ' + String(r)); });
        } else {
            lines.push('* 暂无识别范围');
        }
        lines.push('');
        
        lines.push('## 三、统一优先级（需确认）');
        lines.push('```text');
        if (Array.isArray(ruleModel.priority_chain) && ruleModel.priority_chain.length) {
            lines.push(ruleModel.priority_chain.join(' > '));
        } else {
            lines.push('暂无识别的优先级');
        }
        lines.push('```');
        lines.push('');
        
        lines.push('## 四、核心流程');
        if (eFlowMd.length) {
            eFlowMd.forEach(function(f) { lines.push(String(f)); });
        } else {
            lines.push('### 正常流程');
            lines.push('暂无识别流程');
        }
        lines.push('');
        
        lines.push('## 五、本次会议待拍板问题（最重要）');
        if (ePendingMd.length) {
            ePendingMd.forEach(function(p, i) { lines.push(String(i+1) + '. ' + String(p)); });
        } else {
            lines.push('1. 暂无待拍板问题');
        }
        lines.push('');
        
        lines.push('## 六、当前风险');
        lines.push('若不定规则直接开发：');
        lines.push('* 前后端理解不一致');
        lines.push('* 测试无法验收');
        lines.push('* 联调返工高');
        lines.push('* 上线后展示混乱');
        lines.push('');
        
        lines.push('## 七、输出物');
        lines.push('会议结束后补齐：');
        lines.push('* 状态机图');
        lines.push('* 优先级规则表');
        lines.push('* 异常流程表');
        lines.push('* 验收标准');
        lines.push('');
        
        return lines.join('\n');
    }

"""
    
    new_content = content[:start_render] + new_code + content[end_render:]
    
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(new_content)
    print(f'Successfully updated {file_path}')

rewrite_html_file('d:/trae-code/trae_platform/modules/prd_audit/templates/prd_audit_index.html')
rewrite_html_file('d:/trae-code/trae_platform/modules/prd_audit_clone/templates/prd_audit_index.html')
