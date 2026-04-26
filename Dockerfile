# Trae Platform - 设备与服务器测试运维平台
FROM python:3.10-slim

WORKDIR /app

# 安装 ADB（Android Debug Bridge）
RUN apt-get update && apt-get install -y --no-install-recommends \
    android-tools-adb \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 数据目录（Runtime、报告、日志）
RUN mkdir -p /app/data/runtime /app/reports /app/logs

ENV FLASK_APP=app.py
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

CMD ["python", "app.py"]
