# ===============================
# 1️⃣ Base image
# ===============================
FROM python:3.11-slim

# Prevent Python from buffering stdout/stderr
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Create a working directory
WORKDIR /app

# ===============================
# 2️⃣ Install system deps
# ===============================
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ curl \
 && rm -rf /var/lib/apt/lists/*

# ===============================
# 3️⃣ Copy and install requirements
# ===============================
COPY requirements.txt .

# Upgrade pip & install dependencies cleanly
RUN pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt && \
    # 🔍 Confirm versions installed
    pip show flask langchain langchain-openai openai | grep -E "Name|Version"

# ===============================
# 4️⃣ Copy the app
# ===============================
COPY . .

# ===============================
# 5️⃣ Set runtime env vars (Cloud Run can override)
# ===============================
ENV PORT=8080
ENV HOST=0.0.0.0
ENV GOOGLE_CLOUD_PROJECT=audioaidemo

# ===============================
# 6️⃣ Start command
# ===============================
CMD ["python", "app.py"]
