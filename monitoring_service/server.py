import logging
import sys
import traceback
from typing import List, Set

import gevent
from eth_utils import encode_hex, is_address, is_checksum_address, is_same_address

from monitoring_service.blockchain import BlockchainMonitor
from monitoring_service.exceptions import ServiceNotRegistered, StateDBInvalid
from monitoring_service.state_db import StateDB
from monitoring_service.tasks import OnChannelClose, OnChannelSettle, StoreMonitorRequest
from monitoring_service.utils import is_service_registered
from raiden_contracts.constants import ChannelEvent
from raiden_contracts.contract_manager import ContractManager
from raiden_libs.gevent_error_handler import register_error_handler
from raiden_libs.messages import BalanceProof, Message, MonitorRequest
from raiden_libs.private_contract import PrivateContract
from raiden_libs.transport import Transport
from raiden_libs.types import Address
from raiden_libs.utils import is_channel_identifier, private_key_to_address

log = logging.getLogger(__name__)


def order_participants(p1: str, p2: str):
    return (p1, p2) if p1 < p2 else (p2, p1)


def error_handler(context, exc_info):
    log.fatal("Unhandled exception terminating the program")
    traceback.print_exception(
        etype=exc_info[0],
        value=exc_info[1],
        tb=exc_info[2],
    )
    sys.exit()


class MonitoringService(gevent.Greenlet):
    def __init__(
        self,
        private_key: str,
        state_db: StateDB,
        transport: Transport,
        blockchain: BlockchainMonitor,
        monitor_contract_address: Address,
        contract_manager: ContractManager,
    ) -> None:
        super().__init__()
        assert isinstance(private_key, str)
        assert isinstance(transport, Transport)
        assert isinstance(blockchain, BlockchainMonitor)
        assert isinstance(state_db, StateDB)
        self.private_key = private_key
        self.transport = transport
        self.blockchain = blockchain
        self.state_db = state_db
        self.stop_event = gevent.event.Event()
        assert is_checksum_address(private_key_to_address(self.private_key))
        self.transport.add_message_callback(lambda message: self.on_message_event(message))
        self.transport.privkey = lambda: self.private_key
        self.address = private_key_to_address(self.private_key)
        self.monitor_contract = PrivateContract(
            blockchain.web3.eth.contract(
                abi=contract_manager.get_contract_abi('MonitoringService'),
                address=monitor_contract_address,
            ),
        )
        self.open_channels: Set[int] = set()

        # some sanity checks
        chain_id = int(self.blockchain.web3.version.network)
        if state_db.is_initialized() is False:
            state_db.setup_db(chain_id, monitor_contract_address, self.address)
        if state_db.chain_id() != chain_id:
            raise StateDBInvalid("Chain id doesn't match!")
        if not is_same_address(state_db.server_address(), self.address):
            raise StateDBInvalid("Monitor service address doesn't match!")
        if not is_same_address(state_db.monitoring_contract_address(), monitor_contract_address):
            raise StateDBInvalid("Monitoring contract address doesn't match!")
        self.task_list: List[gevent.Greenlet] = []
        if not is_service_registered(
            self.blockchain.web3,
            contract_manager,
            monitor_contract_address,
            self.address,
        ):
            raise ServiceNotRegistered(
                "Monitoring service %s is not registered in the Monitoring smart contract (%s)" %
                (self.address, monitor_contract_address),
            )

    def _run(self):
        register_error_handler(error_handler)
        self.transport.start()
        self.blockchain.start()
        self.blockchain.add_confirmed_listener(
            ChannelEvent.OPENED,
            lambda event, tx: self.on_channel_open(event, tx),
        )
        self.blockchain.add_confirmed_listener(
            ChannelEvent.CLOSED,
            lambda event, tx: self.on_channel_close(event, tx),
        )
        self.blockchain.add_confirmed_listener(
            ChannelEvent.SETTLED,
            lambda event, tx: self.on_channel_settled(event, tx),
        )

        # this loop will wait until spawned greenlets complete
        while self.stop_event.is_set() is False:
            tasks = gevent.wait(self.task_list, timeout=5, count=1)
            if len(tasks) == 0:
                gevent.sleep(1)
                continue
            task = tasks[0]
            log.info('%s completed (%s)' % (task, task.value))
            self.task_list.remove(task)

    def stop(self):
        self.blockchain.stop()
        self.stop_event.set()

    def on_channel_open(self, event, tx):
        log.info('on channel open: event=%s tx=%s' % (event, tx))
        channel_id = event['args']['channel_identifier']
        self.open_channels.add(channel_id)

    def on_channel_close(self, event, tx):
        log.info('on channel close: event=%s tx=%s' % (event, tx))
        # check if we have balance proof for the closing
        closing_participant = event['args']['closing_participant']
        channel_id = event['args']['channel_identifier']
        tx_data = tx[1]
        tx_balance_proof = BalanceProof(
            channel_identifier=tx_data[0],
            token_network_address=event['address'],
            balance_hash=tx_data[1],
            nonce=tx_data[2],
            additional_hash=tx_data[3],
            chain_id=int(self.blockchain.web3.version.network),
            signature=encode_hex(tx_data[4]),
        )
        assert tx_balance_proof is not None
        assert is_address(closing_participant)
        assert is_channel_identifier(channel_id)

        pkey_to_mr = self.state_db.get_monitor_requests(channel_id)
        for (_, non_closing_signer), monitor_request in pkey_to_mr.items():
            if non_closing_signer == closing_participant:
                # we don't have to act on behalf of the closing participant
                continue
            # submit monitor request
            self.start_task(
                OnChannelClose(self.monitor_contract, monitor_request, self.private_key),
            )
        self.open_channels.discard(channel_id)

    def on_channel_settled(self, event, tx):
        channel_id = event['args']['channel_identifier']
        # TODO: only claim rewards if MS has submitted a BP.
        # See https://github.com/raiden-network/raiden-monitoring-service/issues/43
        for monitor_request in self.state_db.get_monitor_requests(channel_id).values():
            self.start_task(
                OnChannelSettle(monitor_request, self.monitor_contract, self.private_key),
            )
        self.state_db.delete_monitor_request(event['args']['channel_identifier'])

    def check_event(self, event, balance_proof: BalanceProof):
        return False

    def challenge_proof(self, channel_id):
        balance_proof = self.state_db.balance_proofs.get(
            channel_id, None,
        )
        log.info('challenging proof channel=%s BP=%s' % (channel_id, balance_proof))

    def on_message_event(self, message):
        """This handles messages received over the Transport"""
        assert isinstance(message, Message)
        if isinstance(message, MonitorRequest):
            self.on_monitor_request(message)
        else:
            log.warn('Ignoring unknown message type %s' % type(message))

    def on_monitor_request(
        self,
        monitor_request: MonitorRequest,
    ):
        """Called whenever a monitor proof message is received.
        This will spawn a greenlet and store its reference in an internal list.
        Return value of the greenlet is then checked in the main loop."""
        assert isinstance(monitor_request, MonitorRequest)
        channel_id = monitor_request.balance_proof.channel_identifier
        if channel_id not in self.open_channels:
            log.debug(f'Discarding MR for channel {channel_id}')
            return
        self.start_task(
            StoreMonitorRequest(self.blockchain.web3, self.state_db, monitor_request),
        )

    def start_task(self, task):
        task.start()
        self.task_list.append(task)

    @property
    def monitor_requests(self):
        return self.state_db.get_monitor_requests()

    def wait_tasks(self):
        """Wait until all internal tasks are finished"""
        while True:
            if len(self.task_list) == 0:
                return
            gevent.sleep(1)
