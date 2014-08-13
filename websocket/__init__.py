# -*- coding: utf-8 -*-
from sqlalchemy import engine_from_config
from sqlalchemy.pool import StaticPool
from vscale.models.database import initialize_sql
from vscale.websocket.server import WebSocketServer
from vscale.config import config, init_config

__author__ = 'Victor Poluksht'


def create_app(**kwargs):

    # init_config(kwargs['config_path'])

    ## uncomment if you need it
    # engine = engine_from_config(config, 'SQLALCHEMY.', poolclass=StaticPool)
    # initialize_sql(engine)

    app = WebSocketServer(**kwargs)
    # app.engine = engine

    return app
