# stock-pipeline 실행환경 이미지 — 서버가 밀려도 이 이미지 + git 코드면 복구 끝
# build:  podman build -t stock-pipeline .
# run  :  ./run_docker.sh leader_screener.py   (코드/JDBC/시크릿은 볼륨·env-file로 주입)
FROM docker.io/library/python:3.11-slim

# jaydebeapi(JPype)가 JVM 필요 → JRE 설치
RUN apt-get update && apt-get install -y --no-install-recommends default-jre-headless \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 코드는 이미지에 굽지 않고 실행 시 -v /data/frame:/app 마운트 (git이 원본)
CMD ["python", "--version"]
