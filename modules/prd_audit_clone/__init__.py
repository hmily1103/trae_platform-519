# PRD 审计独立副本（prd_audit_clone）：与主站 prd_audit 功能同步，可单独部署。

from flask import Blueprint

prd_audit_clone_bp = Blueprint(
    "prd_audit_clone",
    __name__,
    template_folder="templates",
    url_prefix="/prd_audit_clone",
)
