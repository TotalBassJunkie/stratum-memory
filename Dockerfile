FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# git is needed for the git-versioned filesystem vault
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
RUN pip install --upgrade pip && pip install .

RUN useradd -m -u 10001 stratum && mkdir -p /var/stratum/kb && chown -R stratum /var/stratum

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Stay root only to make Docker secrets readable by `stratum`, then the
# entrypoint drops privileges (see docker-entrypoint.sh). EXPOSE stays.
EXPOSE 8088
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "stratum.mcp.server"]
