FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Environment variables
ENV PORT=10000

# Expose the port
EXPOSE $PORT

# Start the FastAPI app with Uvicorn
CMD uvicorn bot:app --host 0.0.0.0 --port ${PORT:-10000}
