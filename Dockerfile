FROM python:3.12-slim

# libfribidi0: Pillow's Raqm layout engine (Hebrew RTL on the report card)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libfribidi0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

VOLUME /app/data
EXPOSE 8000
CMD ["solya", "serve"]
