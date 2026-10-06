FROM python:3.11-slim

WORKDIR /app
ENV HF_HOME=/tmp/hf PYTHONUNBUFFERED=1

# Prebuilt CPU wheel of llama.cpp's Python binding: no compiler needed, the build takes about a minute.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir llama-cpp-python==0.3.16 --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu

COPY app.py .
EXPOSE 7860
CMD ["python", "app.py"]
