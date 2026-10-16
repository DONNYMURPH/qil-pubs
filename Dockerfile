FROM python:3.12-slim

# Non-root, matching the securityContext in the CronJob manifest.
RUN useradd --create-home --uid 10001 qil

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

USER 10001
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["qil-pubs"]
CMD ["run", "--trials", "/config/trials.xlsx", \
     "--out-dir", "/data", "--state", "/data/state.json"]
