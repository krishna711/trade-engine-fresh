import asyncio
from typing import Dict, List, Callable, Any, Optional
from collections import defaultdict
import datetime
import json

class LocalEventBus:
    """
    Ultra-low latency in-process and cross-process asynchronous event bus
    for inter-component communication between Feed Manager, Order Manager,
    Strategy Manager, and Web Dashboard across independent terminal processes.
    """

    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self._queues: Dict[str, List[asyncio.Queue]] = defaultdict(list)
        self._ipc_clients: List[asyncio.StreamWriter] = []
        self._ipc_server = None
        self._ipc_client_task: Optional[asyncio.Task] = None
        self._is_ipc_server = False

    def subscribe(self, topic: str, callback: Callable):
        """Register an async or sync callback for a topic"""
        self._subscribers[topic].append(callback)

    def unsubscribe(self, topic: str, callback: Callable):
        if callback in self._subscribers[topic]:
            self._subscribers[topic].remove(callback)

    def create_subscription_queue(self, topic: str, maxsize: int = 5000) -> asyncio.Queue:
        """Creates an asyncio queue for asynchronous workers to pull events directly"""
        q = asyncio.Queue(maxsize=maxsize)
        self._queues[topic].append(q)
        return q

    async def publish(self, topic: str, data: Any, _from_ipc: bool = False):
        """
        Publishes event data to all listeners and queues registered for the topic.
        If this process is the IPC server, broadcasts to all connected client processes.
        """
        # 1. Dispatch to local callback listeners
        callbacks = self._subscribers.get(topic, [])
        for cb in callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    asyncio.create_task(cb(data))
                else:
                    cb(data)
            except Exception as e:
                print(f"[EventBus] Error in subscriber callback for '{topic}': {e}")

        # 2. Dispatch to local queues
        queues = self._queues.get(topic, [])
        for q in queues:
            try:
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(data)
            except Exception as e:
                print(f"[EventBus] Error putting to queue for '{topic}': {e}")

        # 3. If IPC server and event originated locally, broadcast across processes
        if self._is_ipc_server and not _from_ipc and self._ipc_clients:
            msg_bytes = (json.dumps({"topic": topic, "data": data}) + "\n").encode()
            for writer in list(self._ipc_clients):
                try:
                    writer.write(msg_bytes)
                except Exception:
                    if writer in self._ipc_clients:
                        self._ipc_clients.remove(writer)
        elif not self._is_ipc_server and not _from_ipc and getattr(self, "_ipc_client_writer", None):
            try:
                msg_bytes = (json.dumps({"topic": topic, "data": data}) + "\n").encode()
                self._ipc_client_writer.write(msg_bytes)
            except Exception:
                pass

    async def start_ipc_server(self, host: str = "127.0.0.1", port: int = 8765):
        """Starts a local TCP broadcast server for cross-terminal microservice IPC"""
        if self._ipc_server:
            return
        self._is_ipc_server = True

        async def _handle_ipc_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            self._ipc_clients.append(writer)
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        packet = json.loads(line.decode().strip())
                        topic = packet.get("topic")
                        data = packet.get("data")
                        if topic and data is not None:
                            await self.publish(topic, data, _from_ipc=True)
                    except Exception:
                        pass
            except Exception:
                pass
            finally:
                if writer in self._ipc_clients:
                    self._ipc_clients.remove(writer)

        try:
            self._ipc_server = await asyncio.start_server(_handle_ipc_client, host, port)
            print(f"[EventBus IPC] Broadcast Server listening on {host}:{port}")
        except Exception as e:
            print(f"[EventBus IPC] Server start note: {e}")

    async def start_ipc_client(self, host: str = "127.0.0.1", port: int = 8765):
        """Connects to the IPC broadcast server (e.g. Feed Manager) with auto-reconnect"""
        while True:
            try:
                reader, writer = await asyncio.open_connection(host, port)
                self._ipc_client_writer = writer
                print(f"[EventBus IPC] Connected to live IPC event broadcast at {host}:{port}")
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        packet = json.loads(line.decode().strip())
                        topic = packet.get("topic")
                        data = packet.get("data")
                        if topic and data is not None:
                            await self.publish(topic, data, _from_ipc=True)
                    except Exception:
                        pass
                self._ipc_client_writer = None
                writer.close()
                await writer.wait_closed()
            except Exception:
                self._ipc_client_writer = None
                # Retry connection periodically
                await asyncio.sleep(2.0)

# Global singleton event bus
event_bus = LocalEventBus()
