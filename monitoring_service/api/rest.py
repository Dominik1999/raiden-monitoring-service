import gevent
from flask import Flask, request
from flask_restful import Api, Resource
from gevent.pywsgi import WSGIServer

from monitoring_service import MonitoringService
from monitoring_service.blockchain import BlockchainMonitor

API_PATH = '/api/1'


class MonitorRequestsResource(Resource):
    def __init__(self, monitor=None):
        super().__init__()
        assert isinstance(monitor, MonitoringService)
        self.monitor = monitor

    def get(self):
        return list(self.monitor.state_db.get_monitor_request_rows())


class BlockchainEvents(Resource):
    def __init__(self, blockchain=None):
        super().__init__()
        assert isinstance(blockchain, BlockchainMonitor)
        self.blockchain = blockchain

    def put(self):
        json_data = request.get_json()
        self.blockchain.handle_event(json_data)


class ServiceApi:
    def __init__(self, monitor, blockchain):
        self.flask_app = Flask(__name__)
        self.api = Api(self.flask_app)
        self.api.add_resource(BlockchainEvents, API_PATH + "/events",
                              resource_class_kwargs={'blockchain': blockchain})
        self.api.add_resource(MonitorRequestsResource, API_PATH + "/monitor_requests",
                              resource_class_kwargs={'monitor': monitor})

    def run(self, host, port):
        self.rest_server = WSGIServer((host, port), self.flask_app)
        self.server_greenlet = gevent.spawn(self.rest_server.serve_forever)
