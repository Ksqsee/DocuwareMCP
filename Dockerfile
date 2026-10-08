FROM python:3.13-slim
RUN pip install --no-cache-dir uv==0.11.32
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev && useradd --system --uid 1000 app && mkdir /data && chown app /data
USER app
ENV PATH=/app/.venv/bin:$PATH DW_MCP_DATA_DIR=/data
VOLUME /data
EXPOSE 8765
CMD ["docuware-mcp", "--http", "--host", "0.0.0.0"]
