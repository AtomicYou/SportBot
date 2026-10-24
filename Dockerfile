FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright TZ=Europe/Moscow
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && useradd --uid 10001 --create-home bot \
    && mkdir -p /app/data \
    && chown -R bot:bot /app /ms-playwright
COPY --chown=bot:bot . .
USER bot
EXPOSE 8082
CMD ["python", "bot.py"]
