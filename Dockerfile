FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV SERENITY_DATA_DIR=/data
ENV PORT=8501

EXPOSE 8501

CMD ["sh", "-c", "mkdir -p ${SERENITY_DATA_DIR} && streamlit run app.py --server.address 0.0.0.0 --server.port ${PORT} --server.headless true"]
