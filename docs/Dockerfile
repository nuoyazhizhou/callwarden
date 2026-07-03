FROM python:3.11-slim

# 安装系统依赖
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
RUN pip install --no-cache-dir tree-sitter tree-sitter-languages fastmcp semgrep

# 复制代码
WORKDIR /app
COPY . /app/scripts/code_graph/

# 设置工作目录
WORKDIR /app/scripts

# 入口点
ENTRYPOINT ["python", "-m", "code_graph.cli.main"]
