# -*- coding: utf-8 -*-
import json
import sys
import logging
import os
import socket
import signal
import time

import asyncio
import aiohttp
import aiohttp.server
from aiohttp import websocket

import asyncio_redis


__author__ = 'Victor Poluksht'

log = logging.getLogger('websocket')


class HttpRequestHandler(aiohttp.server.ServerHttpProtocol):
    clients = None  # list of all active connections
    parent = None  # supervisor, we use it as broadcaster to all workers

    def __init__(self, *args, parent=None, **kwargs):
        super(HttpRequestHandler, self).__init__(*args, **kwargs)
        self.parent = parent

    @asyncio.coroutine
    def handle_request(self, message, payload):

        websock_chan_id = message.headers.get('websock-chan-id')

        upgrade = 'websocket' in message.headers.get('UPGRADE', '').lower()

        if upgrade:
            status, headers, parser, writer = websocket.do_handshake(
                message.method, message.headers, self.transport)

            resp = aiohttp.Response(
                self.writer, status, http_version=message.version)
            resp.add_headers(*headers)
            resp.send_headers()

            connection = yield from asyncio_redis.Connection.create(host='127.0.0.1', port=6379)
            subscriber = yield from connection.start_subscribe()
            yield from subscriber.subscribe([str(websock_chan_id)])

            while True:
                try:
                    data = yield from subscriber.next_published()
                    print(data)
                    writer.send(data.value.encode())
                except Exception as e:
                    print(e)
                    break

            self.clients[websock_chan_id].remove(writer)


class ChildProcess:
    def __init__(self, up_read, down_write, sock):
        self.up_read = up_read
        self.down_write = down_write
        self.sock = sock
        self.loop = None

    def stop(self):
        self.loop.stop()
        sys.exit(0)

    def start(self):
        # start server
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.add_signal_handler(signal.SIGINT, self.stop)
        asyncio.Task(self.heartbeat())
        asyncio.get_event_loop().run_forever()

    @asyncio.coroutine
    def start_server(self, writer):
        socks = yield from self.loop.create_server(
            lambda: HttpRequestHandler(
                debug=True, keep_alive=75,
                parent=writer),
            sock=self.sock)
        print('Starting worker process {} on {}'.format(os.getpid(), socks.sockets[0].getsockname()))

    @asyncio.coroutine
    def heartbeat(self):
        # setup pipes
        read_transport, read_proto = yield from self.loop.connect_read_pipe(
            aiohttp.StreamProtocol, os.fdopen(self.up_read, 'rb'))
        write_transport, _ = yield from self.loop.connect_write_pipe(
            aiohttp.StreamProtocol, os.fdopen(self.down_write, 'wb'))

        reader = read_proto.reader.set_parser(websocket.WebSocketParser)
        writer = websocket.WebSocketWriter(write_transport)

        asyncio.Task(self.start_server(writer))

        while True:
            try:
                msg = yield from reader.read()
            except:
                print('Supervisor is dead, {} stopping...'.format(os.getpid()))
                self.loop.stop()
                break

            if msg.tp == websocket.MSG_PING:
                writer.pong()

        read_transport.close()
        write_transport.close()


class Worker:
    _started = False

    def __init__(self, sv, loop):
        self.sv = sv
        self.loop = loop
        self.sock = sv.sock
        self.pid = 0
        self.ping = None
        self.writer = None
        self.rtransport = None
        self.wtransport = None
        self.chat_task = None
        self.heartbeat_task = None
        self.clients = {}
        self.start()

    def start(self):
        assert not self._started
        self._started = True

        up_read, up_write = os.pipe()
        down_read, down_write = os.pipe()
        sock = self.sock

        pid = os.fork()
        if pid:
            # parent
            os.close(up_read)
            os.close(down_write)
            asyncio.async(self.connect(pid, up_write, down_read))
        else:
            # child
            os.close(up_write)
            os.close(down_read)
            asyncio.set_event_loop(None)
            process = ChildProcess(up_read, down_write, sock)
            process.start()

    def stop(self):
        self.loop.stop()
        # asyncio.set_event_loop(None)

    @asyncio.coroutine
    def heartbeat(self, writer):
        while True:
            yield from asyncio.sleep(15)

            if (time.monotonic() - self.ping) < 30:
                writer.ping()
            else:
                print('Restart unresponsive worker process: {}'.format(
                    self.pid))
                self.kill()
                self.start()
                return

    @asyncio.coroutine
    def chat(self, reader):
        while True:
            try:
                msg = yield from reader.read()
            except:
                print('Restart unresponsive worker process: {}'.format(
                    self.pid))
                self.kill()
                self.start()
                return

            if msg.tp == websocket.MSG_PONG:
                self.ping = time.monotonic()

    @asyncio.coroutine
    def connect(self, pid, up_write, down_read):
        read_transport, proto = yield from self.loop.connect_read_pipe(
            aiohttp.StreamProtocol, os.fdopen(down_read, 'rb'))
        write_transport, _ = yield from self.loop.connect_write_pipe(
            aiohttp.StreamProtocol, os.fdopen(up_write, 'wb'))

        reader = proto.reader.set_parser(websocket.WebSocketParser)
        writer = websocket.WebSocketWriter(write_transport)

        # store info
        self.pid = pid
        self.ping = time.monotonic()
        self.writer = writer
        self.rtransport = read_transport
        self.wtransport = write_transport
        self.chat_task = asyncio.async(self.chat(reader))
        self.heartbeat_task = asyncio.async(self.heartbeat(writer))

    def kill(self):
        self._started = False
        self.chat_task.cancel()
        self.heartbeat_task.cancel()
        self.rtransport.close()
        self.wtransport.close()
        os.kill(self.pid, signal.SIGTERM)


class WebSocketServer:
    def __init__(self, **kwargs):
        self.loop = asyncio.get_event_loop()
        self.wc = kwargs.get('workers', 2)
        self.host = None
        self.port = None
        self.sock = None
        self.engine = None
        self.clients = {}
        self.workers = []

    def stop(self):
        self.loop.stop()
        for worker in self.workers:
            worker.stop()
        sys.exit(0)

    def run(self, host, port=None):
        # bind socket
        self.host = host
        self.port = port or 5002
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(1024)
        self.sock.setblocking(False)

        for x in range(self.wc):
            self.workers.append(Worker(self, self.loop))

        self.loop.add_signal_handler(signal.SIGINT, self.stop)
        self.loop.add_signal_handler(signal.SIGTERM, self.stop)
        self.loop.run_forever()
