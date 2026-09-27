FROM python:3.13-slim

# Create a non-root user
RUN useradd --create-home --shell /bin/bash confer

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir .[ics]

VOLUME /data
ENV CONFER_HOME=/data

EXPOSE 3067

USER confer

ENTRYPOINT ["confer"]
CMD ["serve", "--host", "0.0.0.0", "--port", "3067"]
