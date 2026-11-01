# JCI 専務理事エージェント — Cloud Run 用イメージ（マルチステージ）
# 土台のイメージは Debian の版（bookworm）まで固定し、知らないうちに OS が替わらないようにする。
# Stage 1: SPA(web) を Vite でビルド
FROM node:24-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
# ロックファイルどおりに入れる（npm install はロックファイルを書き換えて別の版を入れうる）
RUN npm ci
COPY web/ ./
RUN npm run build

# Stage 2: Python ランタイム
FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Tokyo

WORKDIR /srv
COPY app/requirements.txt ./app/requirements.txt
RUN pip install --no-cache-dir -r app/requirements.txt

COPY app ./app
# SPA ビルド成果物を同梱（管理サービスが /app で配信）
COPY --from=web /web/dist ./web/dist

# root で動かさない（アプリはファイルを書かない。コードは root の持ち物のまま読み取りだけ）
RUN useradd --system --uid 10001 --no-create-home app
USER app

ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
