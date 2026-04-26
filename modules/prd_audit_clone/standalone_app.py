# -*- coding: utf-8 -*-
"""
独立运行入口：在项目根目录执行
  python -m modules.prd_audit_clone.standalone_app
或将一键包解压后，在包根目录执行同上命令 / run_clone.bat。

模板：使用 standalone_templates/base.html（仅 PRD 顶栏 + 内容区），不加载运维平台整站侧栏。
"""
import os
import sys

# 保证可 import modules.prd_audit_clone（无论从哪一工作目录启动）
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_CLONE_DIR = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from flask import Flask, jsonify, redirect

from modules.prd_audit_clone.views import prd_audit_bp


def create_app() -> Flask:
    project_root = _ROOT
    # 应用级模板仅含 PRD 独立壳；业务页在 Blueprint 的 templates/ 下
    app = Flask(
        __name__,
        template_folder=os.path.join(_CLONE_DIR, "standalone_templates"),
        static_folder=os.path.join(project_root, "static"),
    )
    app.config["SECRET_KEY"] = os.environ.get("PRD_AUDIT_CLONE_SECRET", "prd_audit_clone_dev_secret")
    app.config["JSON_AS_ASCII"] = False
    app.register_blueprint(prd_audit_bp)

    @app.route("/")
    def _home():
        return redirect("/prd_audit_clone/", code=302)

    @app.route("/healthz")
    def _healthz():
        return jsonify({"ok": True, "module": "prd_audit_clone"})

    return app


if __name__ == "__main__":
    app = create_app()
    host = os.environ.get("PRD_AUDIT_CLONE_HOST", "127.0.0.1")
    port = int(os.environ.get("PRD_AUDIT_CLONE_PORT", "5010"))
    debug = str(os.environ.get("PRD_AUDIT_CLONE_DEBUG", "1")).strip() not in {"0", "false", "False"}
    app.run(host=host, port=port, debug=debug)
