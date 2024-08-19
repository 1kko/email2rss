# Use an official Python runtime as a parent image
#FROM python:3.12-slim
FROM ubuntu:24.04

# Set the working directory in the container to /app
WORKDIR /app

# Update the system package
RUN apt update && apt upgrade -y

# Upgrade pip
RUN python -m pip install --upgrade pip

# Install Poetry
RUN pip install poetry

# Add the current directory contents into the container at /app
COPY pyproject.toml poetry.lock /app/

# Use Poetry to install dependencies
RUN poetry config virtualenvs.create false \
    && poetry install --no-interaction --no-ansi --no-root

COPY *.py /app/

RUN chmod +x start.py

# Make port 8000 available to the world outside this container
EXPOSE 3011

# Run start.py when the container launches
ENTRYPOINT ["./start.py"]
# CMD ["/usr/bin/bash"]
