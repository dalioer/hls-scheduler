# m3u8 -> ffmpeg(MP3) -> pyatv/miniaudio -> RAOP -> HomePod mini 定时推流
# 构建：docker build -t hls2homepod .
# 运行：docker run -d --name hls2homepod --network host \
#         -v $PWD/config.yaml:/app/config.yaml:ro \
#         -e TZ=Asia/Shanghai --restart unless-stopped hls2homepod
# 注意：--network host 是必须的，AirPlay 设备靠 mDNS 发现，bridge 网络收不到广播。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    CONFIG=/app/config.yaml

# ffmpeg(含 libmp3lame) + tzdata(时间段时区) + zeroconf 依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libmp3lame0 \
        tzdata \
        ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY hls_scheduler.py config.yaml /app/

# 以进程方式常驻：主循环每秒检查配置 mtime，改配置即时生效
CMD ["python3", "-u", "hls_scheduler.py", "--config", "/app/config.yaml"]
