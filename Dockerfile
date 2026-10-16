# T5.1 Docker 镜像：内含模型与 LibreOffice，支持内网离线安装 wheels
# 基础镜像取自内网镜像仓库（按需替换 FROM）
FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HANLP_HOME=/opt/models/hanlp

# LibreOffice headless（旧格式转换）；字体用于 PDF/图片中文渲染
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer libreoffice-calc \
        fonts-noto-cjk fontconfig \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 离线安装：预先把 wheels/ 与 requirements.txt 拷入镜像
COPY requirements.txt /app/requirements.txt
COPY wheels/ /wheels/
RUN pip install --no-index --find-links /wheels -r /app/requirements.txt || \
    pip install --no-index --find-links /wheels -r /app/requirements.txt

# 模型目录（PaddleOCR/rapidocr、HanLP、spaCy 离线模型）
COPY models/ /opt/models/

COPY . /app/

ENV REDACT_CONFIG=/app/config.yaml
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')"

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]
