# -*- coding: utf-8 -*-
from websocket import create_app

# app = create_app(config_path='settings.yaml').run('0.0.0.0', port=5002)

app = create_app(config_path='settings.yaml').run('0.0.0.0', port=5002)