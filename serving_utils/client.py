from functools import partial
import logging
import socket
from typing import List, Union, Mapping

import asyncio
from collections import namedtuple

import grpc
from grpclib.client import Channel
from grpclib.exceptions import GRPCError
from grpclib.const import Status
try:
    import tensorflow.compat.v1 as tf
except ImportError:
    import tensorflow as tf

from .round_robin_map import RoundRobinMap

from .protos import predict_pb2, prediction_service_pb2_grpc, list_models_pb2, list_models_pb2_grpc
from .protos import prediction_service_grpc


LOGGER = logging.getLogger(__name__)


def copy_message(src, dst):
    """
    Copy the contents of a src proto message to a destination proto message via string serialization
    :param src: Source proto
    :param dst: Destination proto
    :return:
    """
    dst.ParseFromString(src.SerializeToString())
    return dst


PredictInput = namedtuple('PredictInput', ['name', 'value'])
ORIGINAL_DATA_TYPE = List[PredictInput]
NEW_DATA_TYPE = Mapping[str, 'np.ndarray']


class Connection:
    '''
    An active connection to a model serving GRPC server
    '''

    TIMEOUT_SECONDS = 5

    def __init__(
            self,
            addr: str,
            port: int,
            pem: str = None,
            channel_options: dict = None,
            loop: asyncio.AbstractEventLoop = None,
        ):

        self.addr = addr
        self.port = port

        if channel_options is None:
            channel_options = {}
        if pem is None:
            make_sync_channel = partial(grpc.insecure_channel, options=channel_options)
        else:
            creds = grpc.ssl_channel_credentials(pem)
            make_sync_channel = partial(
                grpc.secure_channel,
                credentials=creds,
                options=channel_options,
            )

        if loop is None:
            loop = asyncio.get_event_loop()

        self.sync_channel = make_sync_channel(f"{addr}:{port}")
        self.async_channel = Channel(addr, port, loop=loop)

        self.sync_stub = prediction_service_pb2_grpc.PredictionServiceStub(self.sync_channel)
        self.async_stub = prediction_service_grpc.PredictionServiceStub(self.async_channel)


class EmptyPool(Exception):
    pass


class RetryFailed(Exception):

    def __init__(self, message, errors):
        super().__init__(message)
        self.errors = errors

    def __str__(self):
        error_msgs = []
        for (i, e) in enumerate(self.errors, 1):
            error_msgs.append(f"Error {i}")
            error_msgs.append("\t" + "\t\n".join(repr(e).split("\n")))

        return super().__repr__() + '\n' + '\n'.join(error_msgs)


class Client:

    def __init__(
            self,
            host: str,
            port: int,
            n_trys: int = 3,
            pem: str = None,
            channel_options: dict = None,
            loop: asyncio.AbstractEventLoop = None,
            logger: logging.Logger = None,
        ):
        """Client to tensorflow_model_server or pyserving

        Includes round-robin load balancing. Separate GRPC connections (channels)
        will be made to each IP address returned by the name resolution request for `host`.

        Args:
            host (str) : hostname of your serving
            port (int) : port of your serving
            n_trys (int) : number of times to try predict/async_predict before giving up
            pem: credentials of grpc
            channel_options: An optional list of key-value pairs (channel args in gRPC runtime)
            loop: asyncio event loop
        """
        self._pem = pem
        if channel_options is None:
            channel_options = {}
        self._channel_options = channel_options

        if loop is None:
            loop = asyncio.get_event_loop()

        self._host = host
        self._port = port

        self._pool = RoundRobinMap()
        self._loop = loop

        self._setup_connections()
        self.n_trys = n_trys

        self.logger = logger or LOGGER

    def _setup_connections(self):
        host = self._host

        _, _, current_addrs = socket.gethostbyname_ex(host)
        current_addrs = set(current_addrs)
        original_addrs = set(self._pool.keys())
        if original_addrs == current_addrs:
            return

        missing = original_addrs - current_addrs
        for address in missing:
            del self._pool[address]

        new_addrs = current_addrs - original_addrs
        for address in new_addrs:
            self._pool[address] = Connection(
                address,
                self._port,
                self._pem,
                self._channel_options,
                self._loop,
            )

    @staticmethod
    def _predict_request(
            data: Union[ORIGINAL_DATA_TYPE, NEW_DATA_TYPE],
            model_name: str,
            output_names=None,
            model_signature_name=None,
        ):
        req = predict_pb2.PredictRequest()
        req.model_spec.name = model_name
        if model_signature_name is not None:
            req.model_spec.signature_name = model_signature_name

        for datum in data:
            if isinstance(datum, PredictInput):
                # old way to pass input
                name = datum.name
                value = datum.value
            else:
                name = datum
                value = data[datum]
            copy_message(tf.make_tensor_proto(value), req.inputs[name])
        if output_names is not None:
            for output_name in output_names:
                req.output_filter.append(output_name)
        return req

    @staticmethod
    def parse_predict_response(response):
        results = {}
        for key in response.outputs:
            tensor_proto = response.outputs[key]
            nd_array = tf.make_ndarray(tensor_proto)
            results[key] = nd_array
        return results

    def list_models(self):
        try:
            _, conn = next(iter(self._pool))
        except StopIteration:
            raise EmptyPool("no connections")
        stub = list_models_pb2_grpc.ListModelsStub(conn.sync_channel)
        response = stub.ListModels(list_models_pb2.ListModelsRequest())
        return response.models

    def get_round_robin_stub(self, is_async_stub=False):
        try:
            _, conn = next(iter(self._pool))
        except StopIteration:
            raise EmptyPool("no connections")
        if is_async_stub:
            return conn.async_stub
        else:
            return conn.sync_stub

    def predict(
            self,
            data: List[PredictInput],
            output_names: List[str] = None,
            model_name: str = 'default',
            model_signature_name: str = None,
        ):

        self._setup_connections()

        request = self._predict_request(
            data=data,
            output_names=output_names,
            model_name=model_name,
            model_signature_name=model_signature_name,
        )
        errors = []
        for _ in range(self.n_trys):

            try:
                stub = self.get_round_robin_stub(is_async_stub=False)
                response = stub.Predict(request)
            except EmptyPool as e:
                self.logger.warning("serving_utils.Client -- empty pool")
                self._setup_connections()
                errors.append(e)
            except grpc.RpcError as e:
                if e.code() == grpc.StatusCode.NOT_FOUND and "Model" in e.details():
                    raise
                self.logger.exception(e)
                self._setup_connections()
                errors.append(e)
            except Exception as e:
                self.logger.exception(e)
                self._setup_connections()
                errors.append(e)
            else:
                break
        else:
            raise RetryFailed(f"Failed after {self.n_trys} tries", errors=errors)
        return self.parse_predict_response(response)

    async def async_predict(
            self,
            data: List[PredictInput],
            output_names: List[str] = None,
            model_name: str = 'default',
            model_signature_name: str = None,
        ):

        self._setup_connections()

        request = self._predict_request(
            data=data,
            output_names=output_names,
            model_name=model_name,
            model_signature_name=model_signature_name,
        )
        errors = []
        for _ in range(self.n_trys):

            try:
                stub = self.get_round_robin_stub(is_async_stub=True)
                response = await stub.Predict(request)
            except asyncio.CancelledError:
                raise
            except EmptyPool as e:
                self.logger.warning("serving_utils.Client -- empty pool")
                self._setup_connections()
                errors.append(e)
            except GRPCError as e:
                if e.status == Status.NOT_FOUND and "Model" in e.message:  # noqao: B306
                    raise
                self.logger.exception(e)
                self._setup_connections()
                errors.append(e)
            except Exception as e:
                self.logger.exception(e)
                self._setup_connections()
                errors.append(e)
            else:
                break
        else:
            raise RetryFailed(f"Failed after {self.n_trys} tries", errors=errors)

        return self.parse_predict_response(response)
