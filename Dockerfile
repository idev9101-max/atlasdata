FROM python:3.12-slim
WORKDIR /app
COPY app.py .
COPY blog ./blog
ENV PORT=7860
EXPOSE 7860
CMD ["python", "app.py"]
