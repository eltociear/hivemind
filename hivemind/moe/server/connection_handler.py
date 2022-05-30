import asyncio
import multiprocessing as mp
from typing import AsyncIterator, Dict, Iterable, List, Tuple, Union

import torch

from hivemind.compression import deserialize_torch_tensor, serialize_torch_tensor
from hivemind.dht import DHT
from hivemind.moe.server.expert_backend import ExpertBackend
from hivemind.moe.server.task_pool import TaskPool
from hivemind.p2p import P2PContext, ServicerBase
from hivemind.p2p.p2p_daemon import DEFAULT_MAX_MSG_SIZE
from hivemind.proto import runtime_pb2
from hivemind.utils import MPFuture, MSGPackSerializer, as_aiter, get_logger, nested_flatten
from hivemind.utils.asyncio import switch_to_uvloop
from hivemind.utils.grpc import gather_from_rpc, split_for_streaming
from hivemind.utils.tensor_descr import BatchTensorDescriptor

logger = get_logger(__name__)


class ConnectionHandler(mp.context.ForkProcess, ServicerBase):
    """
    A process that accepts incoming requests to experts and submits them into the corresponding TaskPool.

    :note: ConnectionHandler is designed so as to allow using multiple handler processes for the same port.
    :param listen_on: network interface, e.g. "0.0.0.0:1337" or "localhost:*" (* means pick any port) or "[::]:7654"
    :param experts: a dict [UID -> ExpertBackend] with all active experts
    """

    def __init__(self, dht: DHT, experts: Dict[str, ExpertBackend]):
        super().__init__()
        self.dht, self.experts = dht, experts

        self.ready = MPFuture()

    def run(self):
        torch.set_num_threads(1)
        loop = switch_to_uvloop()

        async def _run():
            try:
                self._p2p = await self.dht.replicate_p2p()
                await self.add_p2p_handlers(self._p2p, balanced=True)

                await asyncio.Future()

            except Exception as e:
                self.ready.set_exception(e)
                return

        self.ready.set_result(None)

        try:
            loop.run_until_complete(_run())
        except KeyboardInterrupt:
            logger.debug("Caught KeyboardInterrupt, shutting down")

    async def rpc_info(self, request: runtime_pb2.ExpertUID, context: P2PContext) -> runtime_pb2.ExpertInfo:
        return runtime_pb2.ExpertInfo(serialized_info=MSGPackSerializer.dumps(self.experts[request.uid].get_info()))

    class _RequestUnpacker:

        __slots__ = ("uid",)

        def __init__(self):
            self.uid = None

        def __call__(self, request: runtime_pb2.ExpertRequest) -> Iterable[runtime_pb2.Tensor]:
            if self.uid is None:
                self.uid = request.uid
            else:
                assert self.uid == request.uid, "Expert uids differ in one request"

            return request.tensors

    async def _gather_inputs(
        self, requests: AsyncIterator[runtime_pb2.ExpertRequest], context: P2PContext
    ) -> Tuple[str, List[torch.Tensor]]:
        unpacker = self._RequestUnpacker()
        inputs = await gather_from_rpc(requests, unpacker, deserialize_torch_tensor)
        return unpacker.uid, inputs

    async def _process_inputs(
        self,
        inputs: List[torch.Tensor],
        pool: TaskPool,
        schema: Union[BatchTensorDescriptor, Tuple[BatchTensorDescriptor, ...]],
    ) -> List[runtime_pb2.Tensor]:
        return [
            serialize_torch_tensor(t, p.compression, allow_inplace=True)
            for t, p in zip(await pool.submit_task(*inputs), nested_flatten(schema))
        ]

    async def rpc_forward(self, request: runtime_pb2.ExpertRequest, context: P2PContext) -> runtime_pb2.ExpertResponse:
        inputs = [deserialize_torch_tensor(tensor) for tensor in request.tensors]
        expert = self.experts[request.uid]
        return runtime_pb2.ExpertResponse(
            tensors=await self._process_inputs(inputs, expert.forward_pool, expert.outputs_schema)
        )

    async def rpc_forward_stream(
        self, requests: AsyncIterator[runtime_pb2.ExpertRequest], context: P2PContext
    ) -> AsyncIterator[runtime_pb2.ExpertRequest]:
        uid, inputs = await self._gather_inputs(requests, context)
        expert = self.experts[uid]
        output_split = [
            p
            for t in await self._process_inputs(inputs, expert.forward_pool, expert.outputs_schema)
            for p in split_for_streaming(t, DEFAULT_MAX_MSG_SIZE // 2)
        ]

        async for part in as_aiter(*output_split):
            yield runtime_pb2.ExpertResponse(tensors=[part])

    async def rpc_backward(
        self, request: runtime_pb2.ExpertRequest, context: P2PContext
    ) -> runtime_pb2.ExpertResponse:
        inputs_and_grads = [deserialize_torch_tensor(tensor) for tensor in request.tensors]
        expert = self.experts[request.uid]
        return runtime_pb2.ExpertResponse(
            tensors=await self._process_inputs(inputs_and_grads, expert.backward_pool, expert.grad_inputs_schema)
        )

    async def rpc_backward_stream(
        self, requests: AsyncIterator[runtime_pb2.ExpertRequest], context: P2PContext
    ) -> AsyncIterator[runtime_pb2.ExpertResponse]:
        uid, inputs_and_grads = await self._gather_inputs(requests, context)
        expert = self.experts[uid]
        output_split = [
            p
            for t in await self._process_inputs(inputs_and_grads, expert.backward_pool, expert.grad_inputs_schema)
            for p in split_for_streaming(t, DEFAULT_MAX_MSG_SIZE // 2)
        ]

        async for part in as_aiter(*output_split):
            yield runtime_pb2.ExpertResponse(tensors=[part])
