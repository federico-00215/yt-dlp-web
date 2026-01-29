FROM python:3.11
ENV DEBIAN_FRONTEND=noninteractive
ENV DEBIAN_PRIORITY=critical
MAINTAINER Benjamin Renard <brenard@zionetrix.net>

RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install --no-install-recommends -y \
        rsyslog \
        python3 \
        python3-dev \
        python3-pip \
        supervisor \
        redis-server \
        ffmpeg \
        curl && \
    curl -fsSL https://deno.land/install.sh | sh && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.deno/bin:$PATH"




ADD requirements.txt /app/
RUN pip install -r /app/requirements.txt

ADD static /app/static
ADD templates /app/templates
ADD app.py /app/

# Supervisor
ADD supervisord/* /etc/supervisor/conf.d/
RUN sed -i \
  -e 's#\[supervisord\]#[supervisord]\nnodaemon=true#' \
  /etc/supervisor/supervisord.conf
RUN mkdir -p /var/log/yt-dlp-web /var/log/redis

# Redis
RUN sed -i \
  -e 's#^daemonize .*#daemonize no#' \
  -e 's#^logfile .*#logfile ""#' \
  /etc/redis/redis.conf

RUN mkdir /downloads
RUN chmod 777 /downloads

EXPOSE 5000

CMD ["/usr/bin/supervisord"]
