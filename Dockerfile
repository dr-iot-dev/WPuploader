FROM python:3.13.7-slim
WORKDIR /app
COPY pyproject.toml ./
COPY wpuploader ./wpuploader
RUN pip install --no-cache-dir .
RUN useradd --system --uid 10001 app && mkdir /data && chown app /data
USER 10001
ENTRYPOINT ["wpuploader"]
CMD ["watch"]
