import os
import sys

# Ensure current directory is in sys.path for utils imports
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from flask import Flask
from views import precision_test_bp

app = Flask(__name__)
app.register_blueprint(precision_test_bp)

if __name__ == '__main__':
    print("============================================================")
    print("代码级精准回归测试工具 - 独立版启动")
    print("============================================================")
    print("访问地址: http://127.0.0.1:5001")
    print("============================================================")
    # Using port 5001 to avoid conflict with the main platform
    app.run(host='0.0.0.0', port=5001, debug=True)
