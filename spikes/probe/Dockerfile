FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1
# Cloud SQL Auth Proxy: lets psycopg (which the Cloud SQL Python Connector does not support) connect to localhost:5432
ADD https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.18.0/cloud-sql-proxy.linux.amd64 /usr/local/bin/cloud-sql-proxy
RUN chmod 0755 /usr/local/bin/cloud-sql-proxy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py start.sh ./
RUN chmod +x start.sh
EXPOSE 8080
CMD ["./start.sh"]
