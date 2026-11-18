import json
import secrets
import threading
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel, StrictBool

class ControlRequest(BaseModel):
    paused: StrictBool

def install_routes(app, root):
    directory = root / 'data' / 'presenter_control'
    directory.mkdir(parents=True, exist_ok=True)
    key_file = directory / 'key.txt'
    if not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(32), encoding='utf-8')
    key = key_file.read_text(encoding='utf-8').strip()
    status_file = directory / 'state.json'
    lock = threading.Lock()
    # A backend restart always restores forwarding.
    status_file.write_text('{"paused": false}', encoding='utf-8')
    def authenticate(provided):
        if not provided or not secrets.compare_digest(provided, key):
            raise HTTPException(status_code=403, detail='Incorrect presenter key')
    @app.get('/presenter/forwarding')
    def get_control(x_presenter_key: str = Header(default='')):
        authenticate(x_presenter_key)
        with lock:
            return json.loads(status_file.read_text(encoding='utf-8'))
    @app.post('/presenter/forwarding')
    def set_control(body: ControlRequest, x_presenter_key: str = Header(default='')):
        authenticate(x_presenter_key)
        with lock:
            temp = status_file.with_suffix('.tmp')
            temp.write_text(json.dumps({'paused': body.paused}), encoding='utf-8')
            temp.replace(status_file)
        return {'paused': body.paused}
