import json
import threading
import time
import uuid

from bus import AgentMessage, MessageType, ReliableMessageBus
from bus.team_bus import BUS


RELIABLE_BUS = ReliableMessageBus(BUS)


class ProtocolManager:
    def __init__(self):
        self._lock = threading.Lock()
        self.shutdown_requests = {}
        self.plan_requests = {}

    def handle_shutdown_request(self, name: str) -> str:
        request_id = uuid.uuid4().hex[:8]
        message = AgentMessage(
            id=request_id,
            sender="lead",
            recipient=name,
            type=MessageType.SHUTDOWN_REQUEST,
            payload={"request_id": request_id, "content": "Please shutdown gracefully."},
        )
        with self._lock:
            self.shutdown_requests[request_id] = {
                "target": name,
                "timestamp": time.time(),
                "status": "pending",
                "message_id": message.id,
            }
        RELIABLE_BUS.send(message)
        return f"Shutdown request sent for {name} with request_id {request_id}"

    def handle_plan_review(self, request_id: str, approved: bool, feedback: str = "") -> str:
        with self._lock:
            req = self.plan_requests.get(request_id)
            if not req:
                return f"Error: No such plan review request {request_id}"
            req["status"] = "approved" if approved else "rejected"
            recipient = req["sender"]

        message = AgentMessage(
            sender="lead",
            recipient=recipient,
            type=MessageType.PLAN_RESPONSE,
            correlation_id=request_id,
            payload={
                "request_id": request_id,
                "approve": approved,
                "feedback": feedback,
            },
        )
        RELIABLE_BUS.send(message)
        return f"Plan {req['status']} for '{recipient}'"

    def _check_shutdown_status(self, request_id: str) -> str:
        with self._lock:
            return json.dumps(
                self.shutdown_requests.get(request_id, {"error": "not found"}),
                ensure_ascii=False,
            )

    def handle_shutdown_response(
        self,
        sender: str,
        request_id: str,
        approved: bool,
        details: str = "",
    ) -> str:
        with self._lock:
            req = self.shutdown_requests.get(request_id)
            if not req:
                return f"Error: No such shutdown request {request_id}"
            req["status"] = "approved" if approved else "failed"
            req["details"] = details

        message = AgentMessage(
            sender=sender,
            recipient="lead",
            type=MessageType.SHUTDOWN_RESPONSE,
            correlation_id=request_id,
            payload={
                "request_id": request_id,
                "approve": approved,
                "details": details,
            },
        )
        RELIABLE_BUS.send(message)
        if approved:
            return f"Approved Shutdown response recorded for request_id {request_id}"
        return f"Shutdown failed for request_id {request_id} with reason: {details}"

    def handle_plan_request(self, sender: str, plan: str) -> str:
        request_id = uuid.uuid4().hex[:8]
        message = AgentMessage(
            id=request_id,
            sender=sender,
            recipient="lead",
            type=MessageType.PLAN_REQUEST,
            payload={"request_id": request_id, "plan": plan},
        )
        with self._lock:
            self.plan_requests[request_id] = {
                "sender": sender,
                "plan": plan,
                "timestamp": time.time(),
                "status": "pending",
                "message_id": message.id,
            }
        RELIABLE_BUS.send(message)
        return f"Plan review request {request_id} sent to lead."

    def notify_message(self, raw_message) -> AgentMessage | None:
        try:
            return RELIABLE_BUS.notify_arrival(raw_message)
        except Exception:
            return None


PROTOCOLS = ProtocolManager()
