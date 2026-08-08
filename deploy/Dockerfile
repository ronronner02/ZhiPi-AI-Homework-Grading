# 智批π Demo 公网镜像
#
# 设计约束（比赛现场有断网预案，线上体验链接也要经得起被转发）：
#   1. 时区必须是 Asia/Shanghai —— VLM/批改的「日配额」按本地日期滚动，
#      容器默认 UTC 会让配额在北京时间早上 8 点重置，对不上运营预期；
#   2. 以非 root 运行 —— 这是个接受任意人上传图片的服务；
#   3. 依赖精确锁版本 —— 见 requirements.lock.txt，避免某天上游发版把线上打挂；
#   4. 静态资源与样例数据都在镜像内，容器不需要任何外网出口即可跑起来。

FROM python:3.13.3-slim-bookworm

# Pillow 的 wheel 自带解码器，无需再装系统图形库；只补时区数据。
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata curl \
 && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先只拷依赖清单，让这一层能被缓存复用
COPY deploy/requirements.lock.txt /app/requirements.lock.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.lock.txt

# 再拷业务代码（含 static/ 与 data/ 样例图库）
COPY demo/ /app/

# 非 root 运行
RUN useradd --create-home --shell /usr/sbin/nologin zhipi \
 && chown -R zhipi:zhipi /app
USER zhipi

# 容器内必须监听 0.0.0.0，否则宿主 / 反代访问不到
ENV ZHIPI_HOST=0.0.0.0 \
    ZHIPI_PORT=8010
EXPOSE 8010

# /healthz 不受访问口令限制，专门留给探针
HEALTHCHECK --interval=30s --timeout=4s --start-period=10s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${ZHIPI_PORT}/healthz" || exit 1

# 用 uvicorn 直接起，不经 app.py 的 __main__ 分支，方便在 compose 里追加参数
CMD ["sh", "-c", "exec python -m uvicorn app:app --host \"$ZHIPI_HOST\" --port \"$ZHIPI_PORT\" --workers 1"]
