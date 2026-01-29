import argparse
import json
import logging
import random
import time
import os

from celery import Celery
from celery.result import AsyncResult
from celery.worker.control import revoke
from datetime import datetime, timedelta
from flask import Flask
from flask import request
from flask import send_from_directory
from flask.templating import render_template
from flask_redis import FlaskRedis
from yt_dlp.YoutubeDL import YoutubeDL

downloads_path = os.getenv('DOWNLOAD_PATH', '/downloads/')
listen_host = os.getenv('LISTEN_HOST', '0.0.0.0')
listen_port = int(os.getenv('LISTEN_PORT', 5000))
redis_host = os.getenv('REDIS_HOST', 'localhost')
redis_port = int(os.getenv('REDIS_PORT', 6379))

app = Flask(__name__)
app.config.update(
    CELERY_BROKER_URL=f'redis://{redis_host}:{redis_port}',
    CELERY_RESULT_BACKEND=f'redis://{redis_host}:{redis_port}',
    REDIS_URL=f'redis://{redis_host}:{redis_port}/1'
)
celery = Celery(
    app.name, backend='rpc://', broker=app.config['CELERY_BROKER_URL']
)
redis_store = FlaskRedis(app)
log = logging.getLogger(__name__)


class Download:
    def __init__(self, json_content):
        try:
            j = json.loads(json_content)
        except TypeError:
            j = {}
        self.id = j.get('id', '')
        self.url = j.get('url', '')
        self.title = j.get('title', '')
        self.downloaded_bytes = j.get('downloaded_bytes', '')
        self.total_bytes = j.get('total_bytes', '')
        self.status = j.get('status', '')
        self.speed = j.get('speed', '')
        self.eta = j.get('eta', '')
        self.task_id = j.get('task_id', '')
        self.last_update = j.get('last_update', '')
        self.filename = j.get('filename', '')
        self.downloads_path = j.get('downloads_path', downloads_path)

        if self.last_update != '':
            timestamp = datetime.strptime(self.last_update, '%Y-%m-%d %H:%M:%S')
            two_mins_ago = timestamp + timedelta(minutes=2)
            if two_mins_ago < datetime.now() and self.status != 'finished':
                self.stuck = True
            else:
                self.stuck = False
        else:
            self.stuck = False

    def to_json(self):
        return json.dumps(self.__dict__)

    def save(self):
        if self.id == '':
            self.id = int(time.time() * 1000) + random.randint(0, 999)
        redis_store.set(self.id, self.to_json())
        return self

    @staticmethod
    def find(id):
        return Download(redis_store.get(id))

    @staticmethod
    def find_by_url(url):
        for result in redis_store.scan_iter('*'):
            try:
                item = json.loads(redis_store.get(result))
            except TypeError:
                # Result not found
                return None

            if item.get('url') == url:
                return item

    def delete(self):
        redis_store.delete(self.id)

    def set_details(self, info):
        try:
            self.title = os.path.basename(info['tmpfilename']).replace('.part', '')
        except Exception:
            pass

        # Already completed downloads don't have `downloaded_bytes`
        if info.get('status') != 'finished':
            self.downloaded_bytes = info.get('downloaded_bytes', 0)
            self.total_bytes = info.get('total_bytes', info.get('total_bytes_estimate', 0))
        else:
            self.downloaded_bytes = self.total_bytes = info.get('total_bytes', 0)
        self.speed = info.get('_speed_str', '')
        self.status = info.get('status', 'pending')
        self.eta = info.get('_eta_str', '')
        self.last_update = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.filename = None
        self.save()

    def set_filename(self, filename):
        log.debug('Post hook tiggered: filename=%s', filename)
        self.filename = os.path.basename(filename)
        file_stats = os.stat(filename)
        self.downloaded_bytes = self.total_bytes = file_stats.st_size
        self.save()


@celery.task
def download(id):
    with app.app_context():
        d = Download.find(id)
opts = {
    'outtmpl': os.path.join(d.downloads_path, '%(title)s-%(id)s.%(ext)s'),
    'progress_hooks': [d.set_details],
    'post_hooks': [d.set_filename],
    'noplaylist': True,
    'retries': 10,
    'fragment_retries': 10,
    'concurrent_fragment_downloads': 1,
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept-Language': 'it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7',
    },
    'js_runtimes': {'deno': {}},

        y = YoutubeDL(params=opts)
        y.download([d.url])


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/downloads')
def get_downloads():
    result = []
    for i in redis_store.scan_iter('*'):
        d = Download(redis_store.get(i).decode())
        result.append(json.loads(d.to_json()))
    return json.dumps(
        sorted(result, key=lambda x: (x['status'], x['last_update']))
    )


@app.route('/add', methods=['POST'])
def add_download():
    data = json.loads(request.data.decode())
    for url in data['url'].split('\n'):
        # Check if it exists
        q = Download.find_by_url(url)
        if q is None and url != '':
            # Store URL
            d = Download(json.dumps({'url': url, 'title': url}))
            d = d.save()

            # Create task to download
            task = download.delay(d.id)
            d.task_id = task.id
            d.status = 'pending'
            d.save()

    return 'OK', 201


@app.route('/remove/<int:id>', methods=['DELETE'])
def remove_download(id):
    d = Download.find(id)
    task = AsyncResult(d.task_id)
    try:
        log.debug('Revoke task %s (%s)', id, task)
        if not task.ready():
            task.revoke(terminate=True)
            log.info('Task %s (%s) revoked', id, task)
        else:
            log.warning('Task %s not ready (%s), can not revoke it.', id, task)

    except Exception:
        log.exception('Exception occured revoking task %s (%s)', id, task)
        return 'ERROR', 500

    d.delete()
    return 'OK', 200


@app.route('/restart/<int:id>', methods=['POST'])
def restart_download(id):
    new = download.apply_async([id], countdown=5)
    existing = Download.find(id)
    existing.task_id = new.task_id
    existing.save()
    return 'OK', 200


@app.route('/download/<int:id>', methods=['GET'])
def download_file(id):
    d = Download.find(id)
    if not d.filename:
        return 'ERROR - Not yet downloaded', 400
    log.debug('Download file %s (%s)', id, d.filename)
    return send_from_directory(downloads_path, d.filename, as_attachment=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--debug', action='store_true', help='Enable debug mode')
    parser.add_argument('--dev-mode', action='store_true', help='Enable development mode')
    parser.add_argument(
        '-D', '--downloads-path', type=str,
        help=f'Downloads directory path (default: {downloads_path})',
        default=downloads_path)
    parser.add_argument(
        '-l', '--listen-host', type=str,
        help=f'Listen host (default: {listen_host})',
        default=listen_host)
    parser.add_argument(
        '-p', '--listen-port', type=int,
        help=f'Listen port (default: {listen_port})',
        default=listen_port)
    options = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if options.debug else logging.INFO)
    if not os.path.isdir(options.downloads_path):
        parser.error(f'Invalid downloads path speficied ({options.downloads_path}): not a directory')
    if not os.access(options.downloads_path, os.W_OK):
        parser.error(f'Invalid downloads path speficied ({options.downloads_path}): not writable')
    downloads_path = options.downloads_path

    logging.basicConfig(level=logging.DEBUG)
    log.debug('Starting application: listen on %s and store downloaded files in %s...', options.listen_host, downloads_path)
    app.run(host=options.listen_host, port=options.listen_port, use_reloader=options.dev_mode)
