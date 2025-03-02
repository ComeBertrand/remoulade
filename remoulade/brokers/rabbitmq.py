# This file is a part of Remoulade.
#
# Copyright (C) 2017,2018 CLEARTYPE SRL <bogdan@cleartype.io>
#
# Remoulade is free software; you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at
# your option) any later version.
#
# Remoulade is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU Lesser General Public
# License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import time
from threading import Lock, local

from amqpstorm import AMQPChannelError, AMQPConnectionError, AMQPError, UriConnection

from ..broker import Broker, Consumer, MessageProxy
from ..common import current_millis, dq_name, xq_name
from ..errors import ConnectionClosed, QueueJoinTimeout
from ..logging import get_logger
from ..message import Message

#: The maximum amount of time a message can be in the dead queue.
DEAD_MESSAGE_TTL = 86400000 * 7

#: The max number of times to attempt an enqueue operation in case of
#: a connection error.
MAX_ENQUEUE_ATTEMPTS = 6


class RabbitmqBroker(Broker):
    """A broker that can be used with RabbitMQ.

    Examples:

      >>> RabbitmqBroker(url="amqp://guest:guest@127.0.0.1:5672")

    Parameters:
      confirm_delivery(bool): Wait for RabbitMQ to confirm that
        messages have been committed on every call to enqueue.
        Defaults to False.
      url(str): The optional connection URL to use to determine which Rabbit server to connect to.
        If None is provided, connection is made with 'amqp://guest:guest@localhost:5672'
      middleware(list[Middleware]): The set of middleware that apply
        to this broker.
      max_priority(int): Configure the queues with x-max-priority to
        support priority queue in RabbitMQ itself

    """

    def __init__(self, *, confirm_delivery=False, url=None, middleware=None, max_priority=None):
        super().__init__(middleware=middleware)

        if max_priority is not None and not (0 < max_priority <= 255):
            raise ValueError("max_priority must be a value between 0 and 255")

        self.url = url or ''
        self.confirm_delivery = confirm_delivery
        self.max_priority = max_priority
        self._connection = None
        self.queues = set()
        self.state = local()
        self.queues_declared = False
        # we need a Lock on self._connection as it can be modified by multiple threads
        self.lock = Lock()

    @property
    def connection(self):
        """The :class:amqpstorm.Connection` for the current
        proccess.  This property may change without notice.
        """
        with self.lock:
            if self._connection is None or self._connection.is_closed:
                self._connection = UriConnection(self.url)
            return self._connection

    @connection.deleter
    def connection(self):
        with self.lock:
            if self._connection is not None:
                try:
                    self._connection.close()
                except AMQPError:
                    pass
            self._connection = None

    @property
    def channel(self):
        """The :class:`amqpstorm.Channel` for the current thread.
        This property may change without notice.
        """
        channel = getattr(self.state, "channel", None)
        if channel is None or channel.is_closed:
            channel = self.connection.channel()
            if self.confirm_delivery:
                channel.confirm_deliveries()

            self.state.channel = channel
        return channel

    @channel.deleter
    def channel(self):
        try:
            channel = self.state.channel
            if channel:
                try:
                    channel.close()
                except AMQPError:
                    pass
            del self.state.channel
        except AttributeError:
            pass

    def close(self):
        """Close all open RabbitMQ connections.
        """

        self.logger.debug("Closing channels and connection...")
        try:
            del self.connection
        except Exception:  # pragma: no cover
            self.logger.debug("Encountered an error while connection.", exc_info=True)
        self._connection = None
        self.logger.debug("Channels and connections closed.")

    def consume(self, queue_name, prefetch=1, timeout=5000):
        """Create a new consumer for a queue.

        Parameters:
          queue_name(str): The queue to consume.
          prefetch(int): The number of messages to prefetch.
          timeout(int): The idle timeout in milliseconds.

        Returns:
          Consumer: A consumer that retrieves messages from RabbitMQ.
        """
        try:
            self._declare_rabbitmq_queues()
        except (AMQPConnectionError, AMQPChannelError) as e:
            if isinstance(e, AMQPConnectionError):
                del self.connection
            else:
                del self.channel
            raise ConnectionClosed(e) from None
        return _RabbitmqConsumer(self.connection, queue_name, prefetch, timeout)

    def _declare_rabbitmq_queues(self):
        """ Real Queue declaration to happen before enqueuing or consuming

        Raises:
          AMQPConnectionError or AMQPChannelError: If the underlying channel or connection has been closed.
        """
        for queue_name in self.queues:
            self._declare_queue(queue_name)
            self._declare_dq_queue(queue_name)
            self._declare_xq_queue(queue_name)

    def declare_queue(self, queue_name):
        """Declare a queue.  Has no effect if a queue with the given
        name already exists.

        Parameters:
          queue_name(str): The name of the new queue.

        Raises:
          ConnectionClosed: If the underlying channel or connection
            has been closed.
        """
        if queue_name not in self.queues:
            self.emit_before("declare_queue", queue_name)
            self.queues.add(queue_name)
            self.emit_after("declare_queue", queue_name)

            delayed_name = dq_name(queue_name)
            self.delay_queues.add(delayed_name)
            self.emit_after("declare_delay_queue", delayed_name)

    def _build_queue_arguments(self, queue_name):
        arguments = {
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": xq_name(queue_name),
        }
        if self.max_priority:
            arguments["x-max-priority"] = self.max_priority

        return arguments

    def _declare_queue(self, queue_name):
        arguments = self._build_queue_arguments(queue_name)
        return self.channel.queue.declare(queue=queue_name, durable=True, arguments=arguments)

    def _declare_dq_queue(self, queue_name):
        arguments = self._build_queue_arguments(queue_name)
        return self.channel.queue.declare(queue=dq_name(queue_name), durable=True, arguments=arguments)

    def _declare_xq_queue(self, queue_name):
        return self.channel.queue.declare(queue=xq_name(queue_name), durable=True, arguments={
            # This HAS to be a static value since messages are expired
            # in order inside of RabbitMQ (head-first).
            "x-message-ttl": DEAD_MESSAGE_TTL,
        })

    def enqueue(self, message, *, delay=None):
        """Enqueue a message.

        Parameters:
          message(Message): The message to enqueue.
          delay(int): The minimum amount of time, in milliseconds, to
            delay the message by.

        Raises:
          ConnectionClosed: If the underlying channel or connection
            has been closed.
        """
        queue_name = message.queue_name
        actor = self.get_actor(message.actor_name)
        properties = {
            'delivery_mode': 2,
            'priority': message.options.get("priority", actor.options.get("priority"))
        }
        if delay is not None:
            queue_name = dq_name(queue_name)
            message_eta = current_millis() + delay
            message = message.copy(
                queue_name=queue_name,
                options={
                    "eta": message_eta,
                },
            )

        attempts = 1
        while True:
            try:
                # I chose to do queue declaration only on first enqueuing, it should be sufficient but it do not
                # resolve the case of queue deletion at runtime. But we do not want the overhead of queue creation on
                # each enqueue
                if not self.queues_declared:
                    self._declare_rabbitmq_queues()
                    self.queues_declared = True
                self.logger.debug("Enqueueing message %r on queue %r.", message.message_id, queue_name)
                self.emit_before("enqueue", message, delay)
                self.channel.basic.publish(
                    exchange="",
                    routing_key=queue_name,
                    body=message.encode(),
                    properties=properties,
                )
                self.emit_after("enqueue", message, delay)
                return message

            except (AMQPConnectionError, AMQPChannelError) as e:
                # Delete the channel (and the connection if needed) so that the
                # next caller/attempt may initiate new ones of each.
                if isinstance(e, AMQPConnectionError):
                    del self.connection
                else:
                    del self.channel

                attempts += 1
                if attempts > MAX_ENQUEUE_ATTEMPTS:
                    raise ConnectionClosed(e) from None

                self.logger.debug(
                    "Retrying enqueue due to closed connection. [%d/%d]",
                    attempts, MAX_ENQUEUE_ATTEMPTS,
                )

    def get_declared_queues(self):
        """Get all declared queues.

        Returns:
          set[str]: The names of all the queues declared so far on
          this Broker.
        """
        return self.queues.copy()

    def get_queue_message_counts(self, queue_name):
        """Get the number of messages in a queue.  This method is only
        meant to be used in unit and integration tests.

        Parameters:
          queue_name(str): The queue whose message counts to get.

        Returns:
          tuple: A triple representing the number of messages in the
          queue, its delayed queue and its dead letter queue.
        """
        queue_response = self._declare_queue(queue_name)
        dq_queue_response = self._declare_dq_queue(queue_name)
        xq_queue_response = self._declare_xq_queue(queue_name)
        return (
            queue_response['message_count'],
            dq_queue_response['message_count'],
            xq_queue_response['message_count'],
        )

    def flush(self, queue_name):
        """Drop all the messages from a queue.

        Parameters:
          queue_name(str): The queue to flush.
        """
        for name in (queue_name, dq_name(queue_name), xq_name(queue_name)):
            self.channel.queue.purge(name)

    def flush_all(self):
        """Drop all messages from all declared queues.
        """
        for queue_name in self.queues:
            self.flush(queue_name)

    def join(self, queue_name, min_successes=10, idle_time=100, *, timeout=None):
        """Wait for all the messages on the given queue to be
        processed.  This method is only meant to be used in tests to
        wait for all the messages in a queue to be processed.

        Warning:
          This method doesn't wait for unacked messages so it may not
          be completely reliable.  Use the stub broker in your unit
          tests and only use this for simple integration tests.

        Parameters:
          queue_name(str): The queue to wait on.
          min_successes(int): The minimum number of times all the
            polled queues should be empty.
          idle_time(int): The number of milliseconds to wait between
            counts.
          timeout(Optional[int]): The max amount of time, in
            milliseconds, to wait on this queue.
        """
        deadline = timeout and time.monotonic() + timeout / 1000
        successes = 0
        while successes < min_successes:
            if deadline and time.monotonic() >= deadline:
                raise QueueJoinTimeout(queue_name)

            total_messages = sum(self.get_queue_message_counts(queue_name)[:-1])
            if total_messages == 0:
                successes += 1
            else:
                successes = 0

            time.sleep(idle_time / 1000)


class _RabbitmqConsumer(Consumer):
    def __init__(self, connection, queue_name, prefetch, timeout):
        try:
            self.logger = get_logger(__name__, type(self))
            self.channel = connection.channel()
            self.channel.basic.qos(prefetch_count=prefetch)
            self.channel.basic.consume(queue=queue_name, no_ack=False)
            self.timeout = timeout
        except (AMQPConnectionError, AMQPChannelError) as e:
            raise ConnectionClosed(e) from None

    def ack(self, message):
        try:
            message.ack()
        except (AMQPConnectionError, AMQPChannelError) as e:
            raise ConnectionClosed(e) from None
        except Exception:  # pragma: no cover
            self.logger.warning("Failed to ack message.", exc_info=True)

    def nack(self, message):
        try:
            message.nack(requeue=False)
        except (AMQPConnectionError, AMQPChannelError) as e:
            raise ConnectionClosed(e) from None
        except Exception:  # pragma: no cover
            self.logger.warning("Failed to nack message.", exc_info=True)

    def requeue(self, messages):
        """RabbitMQ automatically re-enqueues unacked messages when
        consumers disconnect so this is a no-op.
        """

    def __next__(self):
        """ Return None if no value after timeout seconds """
        try:
            deadline = time.monotonic() + self.timeout / 1000
            message = None
            while message is None and time.monotonic() < deadline:
                try:
                    message = next(self.channel.build_inbound_messages(auto_decode=False, break_on_empty=True))
                except StopIteration:
                    time.sleep(0.1)

            return _RabbitmqMessage(message) if message else None
        except (AMQPConnectionError, AMQPChannelError) as e:
            raise ConnectionClosed(e) from None

    def close(self):
        try:
            self.channel.close()
        except (AMQPConnectionError, AMQPChannelError) as e:
            raise ConnectionClosed(e) from None


class _RabbitmqMessage(MessageProxy):
    def __init__(self, rabbitmq_message):
        super().__init__(Message.decode(rabbitmq_message.body))

        self._rabbitmq_message = rabbitmq_message

    def ack(self):
        self._rabbitmq_message.ack()

    def nack(self, requeue):
        self._rabbitmq_message.nack(requeue)
