# PRD 审计独立模块：可单独分享的 PRD 分析能力，可选与用例管理联动保存用例

from flask import Blueprint

prd_audit_bp = Blueprint(
    'prd_audit',
    __name__,
    template_folder='templates',
    url_prefix='/prd_audit'
)
