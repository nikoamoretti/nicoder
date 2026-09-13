# Local Coder isolated worker. Built explicitly by the developer:
#   docker build -t local-coder-worker:1 -f docker/worker.Dockerfile docker/
# Never auto-built or auto-pulled at episode runtime.
FROM python:3.12-slim
RUN pip install --no-cache-dir "pytest==8.3.4"
USER root
