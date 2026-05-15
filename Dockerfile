# Novel_Processor Dockerfile
# Extends the Hermes terminal image with all project dependencies pre-installed
FROM nikolaik/python-nodejs:python3.11-nodejs20

# Install Playwright system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnspr4 \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Install Playwright browsers
RUN playwright install chromium

# Set working directory
WORKDIR /workspace

# Default command
CMD ["bash"]
