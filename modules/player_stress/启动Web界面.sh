#!/bin/bash
# KTV播放器电视端监控系统 - Web界面启动脚本

echo ""
echo "============================================"
echo "  KTV播放器电视端监控系统 - Web界面启动"
echo "  版本: V2.3"
echo "============================================"
echo ""

# 检查Python
if ! command -v python3 &> /dev/null; then
    if ! command -v python &> /dev/null; then
        echo "[错误] 未检测到Python环境"
        exit 1
    else
        PYTHON_CMD=python
    fi
else
    PYTHON_CMD=python3
fi

# 检查Streamlit
$PYTHON_CMD -c "import streamlit" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "[提示] 正在安装Streamlit..."
    $PYTHON_CMD -m pip install streamlit pandas
fi

echo ""
echo "[提示] 正在启动Web界面..."
echo "[提示] 浏览器将自动打开，如果没有自动打开，请访问:"
echo "       http://localhost:8501"
echo ""
echo "[提示] 按 Ctrl+C 可停止服务器"
echo ""

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

streamlit run streamlit_app.py
