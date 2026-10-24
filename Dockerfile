FROM python:3.12-slim

# Pillow is optional (the app degrades to sending raw bytes without it) but
# recommended for downscaling large phone photos before they hit the API.
RUN pip install --no-cache-dir Pillow==12.3.0

WORKDIR /app
COPY . .

ENV MACROLOG_HOST=0.0.0.0 \
    MACROLOG_PORT=8765 \
    MACROLOG_DATA=/data

VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD python3 -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ['MACROLOG_PORT']+'/api/state',timeout=4)" || exit 1

CMD ["python3", "server.py"]
