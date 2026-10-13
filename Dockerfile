FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 BOT_STATE_DIR=/data T212_DEMO=true TEST_TRADE=false
WORKDIR /app
COPY requirements.lock requirements.txt ./
RUN --mount=type=bind,source=.wheelhouse,target=/wheels pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.lock && pip check
RUN useradd --uid 10001 --create-home bot && mkdir /data && chown bot:bot /data
COPY --chown=bot:bot *.py config.yaml ./
USER bot
HEALTHCHECK --interval=60s --timeout=10s --start-period=300s --retries=3 CMD ["python", "healthcheck.py"]
CMD ["python", "continuous.py"]

FROM runtime AS test
COPY --chown=bot:bot tests /app/tests
RUN python -m unittest discover -s tests -v

FROM runtime AS production
