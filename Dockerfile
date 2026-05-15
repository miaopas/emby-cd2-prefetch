FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

# 生成 gRPC 代码（如果 proto 文件有更新）
COPY proto/clouddrive.proto /tmp/clouddrive.proto
RUN python -m grpc_tools.protoc \
    -I/tmp \
    --python_out=. \
    --grpc_python_out=. \
    /tmp/clouddrive.proto

EXPOSE 8095

CMD ["python", "main.py"]
