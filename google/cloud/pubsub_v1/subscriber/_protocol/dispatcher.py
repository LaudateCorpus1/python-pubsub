# Copyright 2017, Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division

import itertools
import logging
import math
import threading
import typing
from typing import List, Optional, Sequence, Union
import warnings

from google.cloud.pubsub_v1.subscriber._protocol import helper_threads
from google.cloud.pubsub_v1.subscriber._protocol import requests
from google.pubsub_v1 import types as gapic_types

if typing.TYPE_CHECKING:  # pragma: NO COVER
    import queue
    from google.cloud.pubsub_v1.subscriber._protocol.streaming_pull_manager import (
        StreamingPullManager,
    )


RequestItem = Union[
    requests.AckRequest,
    requests.DropRequest,
    requests.LeaseRequest,
    requests.ModAckRequest,
    requests.NackRequest,
]


_LOGGER = logging.getLogger(__name__)
_CALLBACK_WORKER_NAME = "Thread-CallbackRequestDispatcher"


_MAX_BATCH_SIZE = 100
"""The maximum number of requests to process and dispatch at a time."""

_MAX_BATCH_LATENCY = 0.01
"""The maximum amount of time in seconds to wait for additional request items
before processing the next batch of requests."""

_ACK_IDS_BATCH_SIZE = 2500
"""The maximum number of ACK IDs to send in a single StreamingPullRequest.

The backend imposes a maximum request size limit of 524288 bytes (512 KiB) per
acknowledge / modifyAckDeadline request. ACK IDs have a maximum size of 164
bytes, thus we cannot send more than o 524288/176 ~= 2979 ACK IDs in a single
StreamingPullRequest message.

Accounting for some overhead, we should thus only send a maximum of 2500 ACK
IDs at a time.
"""


class Dispatcher(object):
    def __init__(self, manager: "StreamingPullManager", queue: "queue.Queue"):
        self._manager = manager
        self._queue = queue
        self._thread: Optional[threading.Thread] = None
        self._operational_lock = threading.Lock()

    def start(self) -> None:
        """Start a thread to dispatch requests queued up by callbacks.

        Spawns a thread to run :meth:`dispatch_callback`.
        """
        with self._operational_lock:
            if self._thread is not None:
                raise ValueError("Dispatcher is already running.")

            worker = helper_threads.QueueCallbackWorker(
                self._queue,
                self.dispatch_callback,
                max_items=_MAX_BATCH_SIZE,
                max_latency=_MAX_BATCH_LATENCY,
            )
            # Create and start the helper thread.
            thread = threading.Thread(name=_CALLBACK_WORKER_NAME, target=worker)
            thread.daemon = True
            thread.start()
            _LOGGER.debug("Started helper thread %s", thread.name)
            self._thread = thread

    def stop(self) -> None:
        with self._operational_lock:
            if self._thread is not None:
                # Signal the worker to stop by queueing a "poison pill"
                self._queue.put(helper_threads.STOP)
                self._thread.join()

            self._thread = None

    def dispatch_callback(self, items: Sequence[RequestItem]) -> None:
        """Map the callback request to the appropriate gRPC request.

        Args:
            items:
                Queued requests to dispatch.
        """
        lease_requests: List[requests.LeaseRequest] = []
        modack_requests: List[requests.ModAckRequest] = []
        ack_requests: List[requests.AckRequest] = []
        nack_requests: List[requests.NackRequest] = []
        drop_requests: List[requests.DropRequest] = []

        for item in items:
            if isinstance(item, requests.LeaseRequest):
                lease_requests.append(item)
            elif isinstance(item, requests.ModAckRequest):
                modack_requests.append(item)
            elif isinstance(item, requests.AckRequest):
                ack_requests.append(item)
            elif isinstance(item, requests.NackRequest):
                nack_requests.append(item)
            elif isinstance(item, requests.DropRequest):
                drop_requests.append(item)
            else:
                warnings.warn(
                    f'Skipping unknown request item of type "{type(item)}"',
                    category=RuntimeWarning,
                )

        _LOGGER.debug("Handling %d batched requests", len(items))

        if lease_requests:
            self.lease(lease_requests)

        if modack_requests:
            self.modify_ack_deadline(modack_requests)

        # Note: Drop and ack *must* be after lease. It's possible to get both
        # the lease and the ack/drop request in the same batch.
        if ack_requests:
            self.ack(ack_requests)

        if nack_requests:
            self.nack(nack_requests)

        if drop_requests:
            self.drop(drop_requests)

    def ack(self, items: Sequence[requests.AckRequest]) -> None:
        """Acknowledge the given messages.

        Args:
            items: The items to acknowledge.
        """
        # If we got timing information, add it to the histogram.
        for item in items:
            time_to_ack = item.time_to_ack
            if time_to_ack is not None:
                self._manager.ack_histogram.add(time_to_ack)

        # We must potentially split the request into multiple smaller requests
        # to avoid the server-side max request size limit.
        ack_ids = (item.ack_id for item in items)
        total_chunks = int(math.ceil(len(items) / _ACK_IDS_BATCH_SIZE))

        for _ in range(total_chunks):
            request = gapic_types.StreamingPullRequest(
                ack_ids=itertools.islice(ack_ids, _ACK_IDS_BATCH_SIZE)
            )
            self._manager.send(request)

        # Remove the message from lease management.
        self.drop(items)

    def drop(
        self,
        items: Sequence[
            Union[requests.AckRequest, requests.DropRequest, requests.NackRequest]
        ],
    ) -> None:
        """Remove the given messages from lease management.

        Args:
            items: The items to drop.
        """
        assert self._manager.leaser is not None
        self._manager.leaser.remove(items)
        ordering_keys = (k.ordering_key for k in items if k.ordering_key)
        self._manager.activate_ordering_keys(ordering_keys)
        self._manager.maybe_resume_consumer()

    def lease(self, items: Sequence[requests.LeaseRequest]) -> None:
        """Add the given messages to lease management.

        Args:
            items: The items to lease.
        """
        assert self._manager.leaser is not None
        self._manager.leaser.add(items)
        self._manager.maybe_pause_consumer()

    def modify_ack_deadline(self, items: Sequence[requests.ModAckRequest]) -> None:
        """Modify the ack deadline for the given messages.

        Args:
            items: The items to modify.
        """
        # We must potentially split the request into multiple smaller requests
        # to avoid the server-side max request size limit.
        ack_ids = (item.ack_id for item in items)
        seconds = (item.seconds for item in items)
        total_chunks = int(math.ceil(len(items) / _ACK_IDS_BATCH_SIZE))

        for _ in range(total_chunks):
            request = gapic_types.StreamingPullRequest(
                modify_deadline_ack_ids=itertools.islice(ack_ids, _ACK_IDS_BATCH_SIZE),
                modify_deadline_seconds=itertools.islice(seconds, _ACK_IDS_BATCH_SIZE),
            )
            self._manager.send(request)

    def nack(self, items: Sequence[requests.NackRequest]) -> None:
        """Explicitly deny receipt of messages.

        Args:
            items: The items to deny.
        """
        self.modify_ack_deadline(
            [requests.ModAckRequest(ack_id=item.ack_id, seconds=0) for item in items]
        )
        self.drop([requests.DropRequest(*item) for item in items])
