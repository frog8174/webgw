FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir . \
 && mkdir -p "$TIKTOKEN_CACHE_DIR" \
 # 建置期預熱編碼檔快取:執行期就不需要連外下載,
 # 這對只開 NodePort、沒有出向網路的部署環境是必要的。
 && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base').encode('warm')" \
 && useradd --create-home --uid 10001 app \
 && mkdir -p /data \
 && chown -R app:app "$TIKTOKEN_CACHE_DIR" /data

USER app

VOLUME ["/data"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('GATEWAY_PORT','8080')+'/healthz', timeout=4).status==200 else 1)"

CMD ["python", "-m", "webgw"]
