# SaaS platform image: builds the React SPA and serves it from FastAPI.
FROM node:20-alpine AS web
WORKDIR /build
COPY web/package.json web/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY web/ .
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY saas ./saas
COPY scripts ./scripts
COPY --from=web /build/dist ./web/dist
RUN mkdir -p storage
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "saas.main:app", "--host", "0.0.0.0", "--port", "8000"]
