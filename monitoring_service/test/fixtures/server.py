import logging

import pytest

from monitoring_service import MonitoringService
from monitoring_service.api.rest import ServiceApi
from monitoring_service.blockchain import BlockchainMonitor
from monitoring_service.utils import register_service
from raiden_contracts.contract_manager import ContractManager
from raiden_libs.test.mocks.dummy_transport import DummyTransport
from raiden_libs.utils import private_key_to_address

log = logging.getLogger(__name__)

TEST_POLL_INTERVAL = 0.001


@pytest.fixture
def server_private_key(get_random_privkey, ethereum_tester):
    key = get_random_privkey()
    ethereum_tester.add_account(key)
    return key


@pytest.fixture
def dummy_transport():
    return DummyTransport()


@pytest.fixture
def blockchain(
        web3,
        contracts_manager: ContractManager,
):
    blockchain = BlockchainMonitor(web3, contracts_manager)
    blockchain.poll_interval = TEST_POLL_INTERVAL
    blockchain.required_confirmations = 1
    yield blockchain
    blockchain.stop()


@pytest.fixture
def monitoring_service(
        server_private_key,
        blockchain,
        dummy_transport,
        state_db_sqlite,
        web3,
        monitoring_service_contract,
        send_funds,
        contracts_manager: ContractManager,
):
    # send some eth & tokens to MS
    send_funds(private_key_to_address(server_private_key))
    register_service(
        web3,
        contracts_manager,
        monitoring_service_contract.address,
        server_private_key,
    )

    ms = MonitoringService(
        server_private_key,
        transport=dummy_transport,
        blockchain=blockchain,
        state_db=state_db_sqlite,
        monitor_contract_address=monitoring_service_contract.address,
        contract_manager=contracts_manager,
    )
    yield ms
    ms.stop()


@pytest.fixture
def rest_api(monitoring_service, blockchain, rest_host, rest_port):
    api = ServiceApi(monitoring_service, blockchain)
    api.run(rest_host, rest_port)
    return api
