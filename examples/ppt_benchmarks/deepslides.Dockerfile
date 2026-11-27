FROM python:3.11-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       libreoffice-impress fonts-dejavu-core fonts-liberation fonts-noto-core fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
COPY pptx_tools /tmp/pptx_tools
RUN python -m pip install --no-cache-dir /tmp/pptx_tools python-pptx==1.0.2 pillow matplotlib numpy pandas openpyxl \
    && rm -rf /tmp/pptx_tools
