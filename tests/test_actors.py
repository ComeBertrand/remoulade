import platform
import time
from unittest.mock import patch

import pytest

import remoulade
from remoulade import Message, Middleware
from remoulade.errors import RateLimitExceeded
from remoulade.middleware import SkipMessage

from .common import worker

_current_platform = platform.python_implementation()


def test_actors_can_be_defined(stub_broker):
    # Given that I've decorated a function with @actor
    @remoulade.actor
    def add(x, y):
        return x + y

    # I expect that function to become an instance of Actor
    assert isinstance(add, remoulade.Actor)


def test_actors_can_be_assigned_predefined_options(stub_broker):
    # Given that I have a stub broker with the retries middleware
    # If I define an actor with a max_retries number
    @remoulade.actor(max_retries=32)
    def add(x, y):
        return x + y

    # I expect the option to persist
    assert add.options["max_retries"] == 32


def test_actors_cannot_be_assigned_arbitrary_options(stub_broker):
    # Given that I have a stub broker
    # If I define an actor with a nonexistent option
    @remoulade.actor(invalid_option=32)
    def add(x, y):
        return x + y

    # I expect it to raise a ValueError when I add it to a broker
    with pytest.raises(ValueError):
        stub_broker.declare_actor(add)


def test_actors_can_be_named(stub_broker):
    # Given that I've decorated a function with @actor and named it explicitly
    @remoulade.actor(actor_name="foo")
    def add(x, y):
        return x + y

    # I expect the returned function to have that name
    assert add.actor_name == "foo"


def test_actors_can_be_assigned_custom_queues(stub_broker):
    # Given that I've decorated a function with @actor and given it an explicit queue
    @remoulade.actor(queue_name="foo")
    def foo():
        pass

    # I expect the returned function to use that queue
    assert foo.queue_name == "foo"


def test_actors_fail_given_invalid_queue_names(stub_broker):
    # If I define an actor with an invalid queue name
    # I expect a ValueError to be raised
    with pytest.raises(ValueError):
        @remoulade.actor(queue_name="$2@!@#")
        def foo():
            pass


def test_actors_can_be_called(stub_broker):
    # Given that I have an actor
    @remoulade.actor
    def add(x, y):
        return x + y

    # If I call it directly,
    # I expect it to run synchronously
    assert add(1, 2) == 3


def test_actors_can_be_sent_messages(stub_broker):
    # Given that I have an actor
    @remoulade.actor
    def add(x, y):
        return x + y

    # And this actor is declared
    stub_broker.declare_actor(add)

    # If I send it a message,
    # I expect it to enqueue a message
    enqueued_message = add.send(1, 2)
    enqueued_message_data = stub_broker.queues["default"].get(timeout=1)
    assert enqueued_message == Message.decode(enqueued_message_data)


def test_actors_no_broker():
    # Given an actor
    @remoulade.actor
    def do_work():
        return 42

    # If I send it a message
    # I expect an error explaining it has no broker
    with pytest.raises(ValueError) as e:
        do_work.send()
    assert str(e.value) == 'No broker is set, did you forget to call set_broker ?'


def test_actors_can_perform_work(stub_broker, stub_worker):
    # Given that I have a database
    database = {}

    # And an actor that can write data to that database
    @remoulade.actor
    def put(key, value):
        database[key] = value

    # And this actor is declared
    stub_broker.declare_actor(put)

    # If I send that actor many async messages
    for i in range(100):
        assert put.send("key-%s" % i, i)

    # Then join on the queue
    stub_broker.join(put.queue_name)
    stub_worker.join()

    # I expect the database to be populated
    assert len(database) == 100


def test_actors_can_perform_work_with_kwargs(stub_broker, stub_worker):
    # Given that I have a database
    results = []

    # And an actor
    @remoulade.actor
    def add(x, y):
        results.append(x + y)

    # And this actor is declared
    stub_broker.declare_actor(add)

    # If I send it a message with kwargs
    add.send(x=1, y=2)

    # Then join on the queue
    stub_broker.join(add.queue_name)
    stub_worker.join()

    # I expect the database to be populated
    assert results == [3]


def test_actors_do_not_retry_by_default(stub_broker, stub_worker):
    # Given that I have a database
    attempts = []

    # And an actor that fails every time
    @remoulade.actor()
    def do_work():
        attempts.append(1)
        raise RuntimeError("failure")

    # And this actor is declared
    stub_broker.declare_actor(do_work)

    # When I send it a message
    do_work.send()

    # And join on the queue
    stub_broker.join(do_work.queue_name)
    stub_worker.join()

    # Then I expect 1 attempts to have occurred
    assert sum(attempts) == 1


def test_actors_retry_on_failure(stub_broker, stub_worker):
    # Given that I have a database
    failures, successes = [], []

    # And an actor that fails the first time it's called
    @remoulade.actor(max_retries=3, min_backoff=100, max_backoff=500)
    def do_work():
        if sum(failures) == 0:
            failures.append(1)
            raise RuntimeError("First failure.")
        else:
            successes.append(1)

    # And this actor is declared
    stub_broker.declare_actor(do_work)

    # If I send it a message
    do_work.send()

    # Then join on the queue
    stub_broker.join(do_work.queue_name)
    stub_worker.join()

    # I expect successes
    assert sum(successes) == 1


def test_actors_retry_a_max_number_of_times_on_failure(stub_broker, stub_worker):
    # Given that I have a database
    attempts = []

    # And an actor that fails every time
    @remoulade.actor(max_retries=3, min_backoff=100, max_backoff=500)
    def do_work():
        attempts.append(1)
        raise RuntimeError("failure")

    # And this actor is declared
    stub_broker.declare_actor(do_work)

    # When I send it a message
    do_work.send()

    # And join on the queue
    stub_broker.join(do_work.queue_name)
    stub_worker.join()

    # Then I expect 4 attempts to have occurred
    assert sum(attempts) == 4


def test_actors_retry_for_a_max_time(stub_broker, stub_worker):
    # Given that I have a database
    attempts = []

    # And an actor that fails every time
    @remoulade.actor(max_age=100, min_backoff=50, max_backoff=500)
    def do_work():
        attempts.append(1)
        raise RuntimeError("failure")

    # And this actor is declared
    stub_broker.declare_actor(do_work)

    # When I send it a message
    do_work.send()

    # And join on the queue
    stub_broker.join(do_work.queue_name)
    stub_worker.join()

    # Then I expect at least one attempt to have occurred
    assert sum(attempts) >= 1


@pytest.mark.skipif(_current_platform == "PyPy", reason="Time limits are not supported under PyPy.")
def test_actors_can_be_assigned_time_limits(stub_broker, stub_worker):
    # Given that I have a database
    attempts, successes = [], []

    # And an actor with a time limit
    @remoulade.actor(max_retries=0, time_limit=1000)
    def do_work():
        attempts.append(1)
        time.sleep(2)
        successes.append(1)

    # And this actor is declared
    stub_broker.declare_actor(do_work)

    # When I send it a message
    do_work.send()

    # And join on the queue
    stub_broker.join(do_work.queue_name)
    stub_worker.join()

    # Then I expect it to fail
    assert sum(attempts) == 1
    assert sum(successes) == 0


@pytest.mark.skipif(_current_platform == "PyPy", reason="Time limits are not supported under PyPy.")
def test_actor_messages_can_be_assigned_time_limits(stub_broker, stub_worker):
    # Given that I have a database
    attempts, successes = [], []

    # And an actor without an explicit time limit
    @remoulade.actor(max_retries=0)
    def do_work():
        attempts.append(1)
        time.sleep(2)
        successes.append(1)

    # And this actor is declared
    stub_broker.declare_actor(do_work)

    # If I send it a message with a custom time limit
    do_work.send_with_options(time_limit=1000)

    # Then join on the queue
    stub_broker.join(do_work.queue_name)
    stub_worker.join()

    # I expect it to fail
    assert sum(attempts) == 1
    assert sum(successes) == 0


def test_actors_can_be_assigned_message_age_limits(stub_broker):
    # Given that I have a database
    runs = []

    # And an actor whose messages have an age limit
    @remoulade.actor(max_age=100)
    def do_work():
        runs.append(1)

    # And this actor is declared
    stub_broker.declare_actor(do_work)

    # When I send it a message
    do_work.send()

    # And wait for its age limit to pass
    time.sleep(0.1)

    # Then join on its queue
    with worker(stub_broker, worker_timeout=100) as stub_worker:
        stub_broker.join(do_work.queue_name)
        stub_worker.join()

        # I expect the message to have been skipped
        assert sum(runs) == 0


def test_actors_can_delay_messages_independent_of_each_other(stub_broker, stub_worker):
    # Given that I have a database
    results = []

    # And an actor that appends a number to the database
    @remoulade.actor
    def append(x):
        results.append(x)

    # And this actor is declared
    stub_broker.declare_actor(append)

    # If I send it a delayed message
    append.send_with_options(args=(1,), delay=1500)

    # And then another delayed message with a smaller delay
    append.send_with_options(args=(2,), delay=1000)

    # Then join on the queue
    stub_broker.join(append.queue_name)
    stub_worker.join()

    # I expect the latter message to have been run first
    assert results == [2, 1]


def test_messages_belonging_to_missing_actors_are_rejected(stub_broker, stub_worker):
    # Given that I have a broker without actors
    # If I send it a message
    message = Message(
        queue_name="some-queue",
        actor_name="some-actor",
        args=(), kwargs={},
        options={},
    )
    stub_broker.declare_queue("some-queue")
    stub_broker.enqueue(message)

    # Then join on the queue
    stub_broker.join("some-queue")
    stub_worker.join()

    # I expect the message to end up on the dead letter queue
    assert stub_broker.dead_letters == [message]


def test_before_and_after_signal_failures_are_ignored(stub_broker, stub_worker):
    # Given that I have a middleware that raises exceptions when it
    # tries to process messages.
    class BrokenMiddleware(Middleware):
        def before_process_message(self, broker, message):
            raise RuntimeError("before process message error")

        def after_process_message(self, broker, message, *, result=None, exception=None):
            raise RuntimeError("after process message error")

    # And a database
    database = []

    # And an actor that appends values to the database
    @remoulade.actor
    def append(x):
        database.append(x)

    # And this actor is declared
    stub_broker.declare_actor(append)

    # If add that middleware to my broker
    stub_broker.add_middleware(BrokenMiddleware())

    # And send my actor a message
    append.send(1)

    # Then join on the queue
    stub_broker.join(append.queue_name)
    stub_worker.join()

    # I expect the task to complete successfully
    assert database == [1]


def test_middleware_can_decide_to_skip_messages(stub_broker, stub_worker):
    # Given a middleware that skips all messages
    skipped_messages = []

    class SkipMiddleware(Middleware):
        def before_process_message(self, broker, message):
            raise SkipMessage()

        def after_skip_message(self, broker, message):
            skipped_messages.append(1)

    stub_broker.add_middleware(SkipMiddleware())

    # And an actor that keeps track of its calls
    calls = []

    @remoulade.actor
    def track_call():
        calls.append(1)

    # And this actor is declared
    stub_broker.declare_actor(track_call)

    # When I send that actor a message
    track_call.send()

    # And join on the broker and the worker
    stub_broker.join(track_call.queue_name)
    stub_worker.join()

    # Then I expect the call list to be empty
    assert sum(calls) == 0

    # And the skipped_messages list to contain one item
    assert sum(skipped_messages) == 1


def test_workers_can_be_paused(stub_broker, stub_worker):
    # Given a paused worker
    stub_worker.pause()

    # And an actor that keeps track of its calls
    calls = []

    @remoulade.actor
    def track_call():
        calls.append(1)

    # And this actor is declared
    stub_broker.declare_actor(track_call)

    # When I send that actor a message
    track_call.send()

    # And wait for 100ms
    time.sleep(0.1)

    # Then no calls should be made
    assert calls == []

    # When I resume the worker and join on it
    stub_worker.resume()
    stub_broker.join(track_call.queue_name)
    stub_worker.join()

    # Then one call should be made
    assert calls == [1]


def test_actors_can_prioritize_work(stub_broker):
    with worker(stub_broker, worker_timeout=100, worker_threads=1) as stub_worker:
        # Given that I a paused worker
        stub_worker.pause()

        # And actors with different priorities
        calls = []

        @remoulade.actor(priority=10)
        def hi():
            calls.append("hi")

        @remoulade.actor(priority=0)
        def lo():
            calls.append("lo")

        # And these actors are declared
        for actor in [hi, lo]:
            stub_broker.declare_actor(actor)

        # When I send both actors a nubmer of messages
        for _ in range(10):
            lo.send()
            hi.send()

        # Then resume the worker and join on the queue
        stub_worker.resume()
        stub_broker.join(lo.queue_name)
        stub_worker.join()

        # Then the high priority actor should run first
        assert calls == ["hi"] * 10 + ["lo"] * 10


def test_actors_can_conditionally_retry(stub_broker, stub_worker):
    # Given that I have a retry predicate
    def should_retry(retry_count, exception):
        return retry_count < 3 and isinstance(exception, RuntimeError)

    # And an actor that raises different types of errors
    attempts = []

    @remoulade.actor(retry_when=should_retry, max_retries=0, min_backoff=100, max_backoff=100)
    def raises_errors(raise_runtime_error):
        attempts.append(1)
        if raise_runtime_error:
            raise RuntimeError("Runtime error")
        raise ValueError("Value error")

    # And this actor is declared
    stub_broker.declare_actor(raises_errors)

    # When I send that actor a message that makes it raise a value error
    raises_errors.send(False)

    # And wait for it
    stub_broker.join(raises_errors.queue_name)
    stub_worker.join()

    # Then I expect the actor not to retry
    assert sum(attempts) == 1

    # When I send that actor a message that makes it raise a runtime error
    attempts[:] = []
    raises_errors.send(True)

    # And wait for it
    stub_broker.join(raises_errors.queue_name)
    stub_worker.join()

    # Then I expect the actor to retry 3 times
    assert sum(attempts) == 4


def test_can_call_str_on_actors():
    # Given that I have an actor
    @remoulade.actor
    def test():
        pass

    # When I call str on it
    # Then I should get back its representation as a string
    assert str(test) == "Actor(test)"


def test_can_call_repr_on_actors():
    # Given that I have an actor
    @remoulade.actor
    def test():
        pass

    # When I call repr on it
    # Then I should get back its representation
    assert repr(test) == "Actor(%(fn)r, queue_name='default', actor_name='test')" % vars(test)


def test_workers_log_rate_limit_exceeded_errors_differently(stub_broker, stub_worker):
    # Given that I've mocked the logging class
    with patch("logging.Logger.warning") as warning_mock:
        # And I have an actor that raises RateLimitExceeded
        @remoulade.actor(max_retries=0)
        def raise_rate_limit_exceeded():
            raise RateLimitExceeded("exceeded")

        # And this actor is declared
        stub_broker.declare_actor(raise_rate_limit_exceeded)

        # When I send that actor a message
        raise_rate_limit_exceeded.send()

        # And wait for the message to get processed
        stub_broker.join(raise_rate_limit_exceeded.queue_name)
        stub_worker.join()

        # Then warning mock should be called with a special message
        warning_messages = [args[0] for _, args, _ in warning_mock.mock_calls]
        assert "Rate limit exceeded in message %s: %s." in warning_messages
