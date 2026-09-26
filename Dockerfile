# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app
COPY . .

EXPOSE 8080

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=12 \
  CMD ["python", "-c", "import os,sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/healthz',timeout=2).status==200 else 1)"]

# 默认启动复核页与接口；verify 服务在 Compose 中以 python verify.py 覆盖启动命令
CMD ["python", "server.py"]
