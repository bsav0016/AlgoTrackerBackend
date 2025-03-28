web: gunicorn algo_tracker_backend.wsgi --log-file -
worker-websockets: python websocket_worker.py
worker-tasks: python manage.py rqworker default