import platform

import pytest

import remoulade
import remoulade.broker
from remoulade.brokers.rabbitmq import RabbitmqBroker
from remoulade.brokers.stub import StubBroker
from remoulade.middleware import Middleware

CURRENT_OS = platform.system()
skip_on_windows = pytest.mark.skipif(CURRENT_OS == "Windows", reason="test skipped on Windows")


class EmptyMiddleware(Middleware):
    pass


def test_broker_uses_rabbitmq_if_not_set():
    # Given that no global broker is set
    remoulade.broker.global_broker = None

    # If I try to get the global broker
    # I expect a ValueError to be raised
    with pytest.raises(ValueError) as e:
        remoulade.get_broker()

    assert str(e.value) == 'Broker not found, are you sure you called set_broker(broker) ?'


def test_change_broker(stub_broker):
    # Given that some actors
    @remoulade.actor
    def add(x, y):
        return x + y

    @remoulade.actor
    def modulo(x, y):
        return x % y

    # And these actors are declared
    stub_broker.declare_actor(add)
    stub_broker.declare_actor(modulo)

    # Given a new broker
    new_broker = StubBroker()

    remoulade.change_broker(new_broker)

    # I expect them to have the same actors
    assert stub_broker.actors == new_broker.actors


def test_declare_actors(stub_broker):
    # Given that some actors
    @remoulade.actor
    def add(x, y):
        return x + y

    @remoulade.actor
    def modulo(x, y):
        return x % y

    actors = [add, modulo]
    remoulade.declare_actors(actors)

    assert set(stub_broker.actors.values()) == set(actors)


def test_declare_actors_no_broker():
    remoulade.broker.global_broker = None
    with pytest.raises(ValueError):
        remoulade.declare_actors([])


@skip_on_windows
def test_broker_middleware_can_be_added_before_other_middleware(stub_broker):
    from remoulade.middleware import AgeLimit

    # Given that I have a custom middleware
    empty_middleware = EmptyMiddleware()

    # If I add it before the AgeLimit middleware
    stub_broker.add_middleware(empty_middleware, before=AgeLimit)

    # I expect it to be the first middleware
    assert stub_broker.middleware[0] == empty_middleware


@skip_on_windows
def test_broker_middleware_can_be_added_after_other_middleware(stub_broker):
    from remoulade.middleware import AgeLimit

    # Given that I have a custom middleware
    empty_middleware = EmptyMiddleware()

    # If I add it after the AgeLimit middleware
    stub_broker.add_middleware(empty_middleware, after=AgeLimit)

    # I expect it to be the second middleware
    assert stub_broker.middleware[1] == empty_middleware


def test_broker_middleware_can_fail_to_be_added_before_or_after_missing_middleware(stub_broker):
    # Given that I have a custom middleware
    empty_middleware = EmptyMiddleware()

    # If I add it after a middleware that isn't registered
    # I expect a ValueError to be raised
    with pytest.raises(ValueError):
        stub_broker.add_middleware(empty_middleware, after=EmptyMiddleware)


@skip_on_windows
def test_broker_middleware_cannot_be_added_both_before_and_after(stub_broker):
    from remoulade.middleware import AgeLimit

    # Given that I have a custom middleware
    empty_middleware = EmptyMiddleware()

    # If I add it with both before and after parameters
    # I expect an AssertionError to be raised
    with pytest.raises(AssertionError):
        stub_broker.add_middleware(empty_middleware, before=AgeLimit, after=AgeLimit)


def test_can_instantiate_brokers_without_middleware():
    # Given that I have an empty list of middleware
    # When I pass that to the RMQ Broker
    broker = RabbitmqBroker(middleware=[])

    # Then I should get back a broker with not middleware
    assert not broker.middleware
