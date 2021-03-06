import pytest

from monitoring_service import MonitoringService
from monitoring_service.exceptions import ServiceNotRegistered, StateDBInvalid
from monitoring_service.test.mockups import StateDBMock
from monitoring_service.utils import is_service_registered, register_service
from raiden_libs.utils import private_key_to_address


def test_server_registration(
        faucet_address,
        server_private_key,
        blockchain,
        dummy_transport,
        state_db_mock,
        web3,
        standard_token_contract,
        monitoring_service_contract,
        send_funds,
        contracts_manager,
):
    """Test two scenarios - instantiating a non-registered server (this should fail),
    and registering it and instantiating again"""
    # instantiation will fail - MS is not registered
    with pytest.raises(ServiceNotRegistered):
        MonitoringService(
            server_private_key,
            transport=dummy_transport,
            blockchain=blockchain,
            state_db=state_db_mock,
            monitor_contract_address=monitoring_service_contract.address,
            contract_manager=contracts_manager,
        )

    # give some tokens to the MS
    server_address = private_key_to_address(server_private_key)
    send_funds(server_address)
    # register MS
    register_service(
        web3,
        contracts_manager,
        monitoring_service_contract.address,
        server_private_key,
    )

    # check if registration succeeded
    assert is_service_registered(
        web3,
        contracts_manager,
        monitoring_service_contract.address,
        server_address,
    )
    # now instantiation will proceed
    ms = MonitoringService(
        server_private_key,
        transport=dummy_transport,
        blockchain=blockchain,
        state_db=state_db_mock,
        monitor_contract_address=monitoring_service_contract.address,
        contract_manager=contracts_manager,
    )
    assert ms is not None


def test_server_wrong_db(
        server_private_key,
        blockchain,
        dummy_transport,
        web3,
        monitoring_service_contract,
        get_random_address,
        send_funds,
        contracts_manager,
):
    server_address = private_key_to_address(server_private_key)
    send_funds(server_address)
    register_service(
        web3,
        contracts_manager,
        monitoring_service_contract.address,
        server_private_key,
    )

    def create_server(setup_database):
        db = StateDBMock()
        setup_database(db)
        return MonitoringService(
            server_private_key,
            transport=dummy_transport,
            blockchain=blockchain,
            state_db=db,
            monitor_contract_address=monitoring_service_contract.address,
            contract_manager=contracts_manager,
        )
    with pytest.raises(StateDBInvalid):
        create_server(
            lambda db: db.setup_db(0, monitoring_service_contract.address, server_address),
        )
    with pytest.raises(StateDBInvalid):
        create_server(
            lambda db: db.setup_db(0, get_random_address(), server_address),
        )
    with pytest.raises(StateDBInvalid):
        create_server(
            lambda db: db.setup_db(0, monitoring_service_contract.address, get_random_address()),
        )
    create_server(
        lambda db: db.setup_db(1, monitoring_service_contract.address, server_address),
    )
