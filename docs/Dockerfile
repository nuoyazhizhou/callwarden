FROM python:3.11-slim

# 安装系统依赖
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# 复制代码（COPY 会自动创建目标目录，无需 mkdir）
COPY . /app/callwarden/
WORKDIR /app/callwarden

# 安装 Python 依赖（通过 requirements.txt 统一管理版本）
RUN pip install --no-cache-dir -r requirements.txt

# 安装 callwarden 本身（注册 cw 命令）
RUN pip install --no-cache-dir .

# 入口点：cw 命令
ENTRYPOINT ["cw"]
