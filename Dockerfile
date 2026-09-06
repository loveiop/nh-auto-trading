FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV PYTHONUNBUFFERED=1
ENV NHPLUG_RATE_LIMIT=4

CMD sh -c 'streamlit run app.py --server.address=0.0.0.0 --server.port=${PORT:-8501} --server.headless=true'
