# Use the official Python slim image to keep the image size small
FROM python:3.10-slim

# Set working directory inside the container
WORKDIR /app

# Copy requirements file first to leverage Docker cache
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Expose port 8001 for the callback server
EXPOSE 8001

# Command to run the bot
CMD ["python", "G3_bot.py"]
