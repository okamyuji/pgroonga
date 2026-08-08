FROM python:3.14-alpine

# psycopgのbinary版を使うのでlibpqのビルドは不要
RUN pip install --no-cache-dir "psycopg[binary]==3.2.12" "redis==6.4.0"

WORKDIR /bench
COPY bench.py /bench/bench.py

CMD ["python", "/bench/bench.py"]
