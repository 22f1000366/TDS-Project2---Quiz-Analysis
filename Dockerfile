# -------------------------------------------------------------
# 🔥 Base image
# -------------------------------------------------------------
FROM python:3.10-slim

# -------------------------------------------------------------
# 🔧 Install system dependencies needed for Playwright Chromium
# -------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    gnupg \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpangocairo-1.0-0 \
    libpango-1.0-0 \
    libgtk-3-0 \
    libxi6 \
    fonts-liberation \
    libappindicator3-1 \
    libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

# -------------------------------------------------------------
# 📁 Create working directory
# -------------------------------------------------------------
WORKDIR /app

# -------------------------------------------------------------
# 📦 Install Python dependencies
# -------------------------------------------------------------
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# -------------------------------------------------------------
# 🖥 Install Playwright Chromium Browser
# -------------------------------------------------------------
RUN playwright install chromium

# -------------------------------------------------------------
# 📂 Copy app files
# -------------------------------------------------------------
COPY . .

# -------------------------------------------------------------
# 🌍 Expose port (HuggingFace uses 7860)
# -------------------------------------------------------------
EXPOSE 7860

# -------------------------------------------------------------
# 🚀 Start app using uvicorn
# -------------------------------------------------------------
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
