FROM python:3.11-slim
WORKDIR /app
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple || \
    pip install --no-cache-dir -r requirements.txt
COPY server/ ./server/
COPY web/ ./web/
EXPOSE 8899
CMD ["python", "server/main.py"]
