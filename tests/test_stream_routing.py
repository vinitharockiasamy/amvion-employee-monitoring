import asyncio
import unittest
from unittest.mock import AsyncMock

from agent.streamer import LiveStreamer
from server.app.api.websocket import ConnectionManager


class FakeSocket:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


class FakeCapture:
    def capture_primary(self):
        return object()


class FakeCompressor:
    def compress_for_stream(self, image, quality=55):
        return b"jpeg"


class TestStreamRouting(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = ConnectionManager()
        self.manager.set_device_status = AsyncMock()

    async def test_start_command_only_reaches_requested_device(self):
        agent_a, agent_b, dashboard = FakeSocket(), FakeSocket(), FakeSocket()
        self.manager.active_agents.update({"A": agent_a, "B": agent_b})

        started = await self.manager.start_device_stream("A", dashboard)

        self.assertTrue(started)
        self.assertEqual([m["type"] for m in agent_a.messages], ["START_STREAM"])
        self.assertEqual(agent_a.messages[0]["device_id"], "A")
        self.assertTrue(agent_a.messages[0]["stream_id"])
        self.assertEqual(agent_b.messages, [])

    async def test_disconnected_device_does_not_fall_back_to_other_agents(self):
        agent_b, dashboard = FakeSocket(), FakeSocket()
        self.manager.active_agents["B"] = agent_b

        started = await self.manager.start_device_stream("A", dashboard)

        self.assertFalse(started)
        self.assertEqual(agent_b.messages, [])
        self.assertNotIn("A", self.manager.active_streams)
        self.assertFalse(await self.manager.send_to_agent("A", {"type": "START_STREAM"}))

    async def test_start_stop_start_awaits_previous_stream_task(self):
        send_started = asyncio.Event()
        active_senders = 0
        max_active_senders = 0

        async def send_frame(_message):
            nonlocal active_senders, max_active_senders
            active_senders += 1
            max_active_senders = max(max_active_senders, active_senders)
            send_started.set()
            try:
                await asyncio.sleep(10)
            finally:
                active_senders -= 1

        streamer = LiveStreamer(FakeCapture(), FakeCompressor(), send_frame, lambda: "A")
        await streamer.start("A1", 100)
        old_task = streamer._stream_task
        await asyncio.wait_for(send_started.wait(), timeout=1)
        await streamer.stop("A1")
        self.assertTrue(old_task.done())
        self.assertFalse(streamer.is_streaming)

        send_started.clear()
        await streamer.start("A2", 100)
        new_task = streamer._stream_task
        await asyncio.wait_for(send_started.wait(), timeout=1)
        self.assertIsNot(old_task, new_task)
        self.assertTrue(old_task.done())
        self.assertEqual(max_active_senders, 1)
        await streamer.stop("A2")
        self.assertTrue(new_task.done())

    async def test_delayed_previous_session_frame_is_rejected(self):
        agent_a, dashboard = FakeSocket(), FakeSocket()
        self.manager.active_agents["A"] = agent_a
        await self.manager.start_device_stream("A", dashboard)
        first_stream = agent_a.messages[-1]["stream_id"]
        await self.manager.stop_device_stream("A")
        await self.manager.start_device_stream("A", dashboard)
        second_stream = agent_a.messages[-1]["stream_id"]

        old_accepted = await self.manager.relay_live_frame(
            "A", {"stream_id": first_stream, "frame": "old", "timestamp": 1}
        )
        new_accepted = await self.manager.relay_live_frame(
            "A", {"stream_id": second_stream, "frame": "new", "timestamp": 2}
        )

        self.assertNotEqual(first_stream, second_stream)
        self.assertFalse(old_accepted)
        self.assertTrue(new_accepted)
        self.assertEqual([m["frame"] for m in dashboard.messages], ["new"])

    async def test_frame_is_derived_from_sender_and_never_relabelled(self):
        agent_a, viewer_a, viewer_b = FakeSocket(), FakeSocket(), FakeSocket()
        self.manager.active_agents["A"] = agent_a
        self.manager.stream_viewers.update({"A": {viewer_a}, "B": {viewer_b}})
        self.manager.active_streams.update({"A": "A-session", "B": "B-session"})

        accepted = await self.manager.relay_live_frame("A", {
            "device_id": "B", "stream_id": "A-session", "frame": "A-frame", "timestamp": 3
        })

        self.assertTrue(accepted)
        self.assertEqual(viewer_a.messages[0]["device_id"], "A")
        self.assertEqual(viewer_a.messages[0]["stream_id"], "A-session")
        self.assertEqual(viewer_b.messages, [])

    async def test_rapid_switching_keeps_commands_and_frames_device_scoped(self):
        agent_a, agent_b = FakeSocket(), FakeSocket()
        viewer_a, viewer_b = FakeSocket(), FakeSocket()
        self.manager.active_agents.update({"A": agent_a, "B": agent_b})

        sequence = [("A", "start"), ("A", "stop"), ("B", "start"), ("B", "stop"),
                    ("A", "start"), ("A", "stop"), ("B", "start"), ("B", "stop"),
                    ("A", "start")]
        for device_id, action in sequence:
            if action == "start":
                dashboard = viewer_a if device_id == "A" else viewer_b
                self.assertTrue(await self.manager.start_device_stream(device_id, dashboard))
                stream_id = self.manager.active_streams[device_id]
                self.assertTrue(await self.manager.relay_live_frame(device_id, {
                    "stream_id": stream_id, "frame": f"{device_id}-frame", "timestamp": 4
                }))
                other_viewer = viewer_b if device_id == "A" else viewer_a
                self.assertFalse(any(m.get("frame") == f"{device_id}-frame" for m in other_viewer.messages))
            else:
                await self.manager.stop_device_stream(device_id)

        self.assertEqual([m["type"] for m in agent_a.messages], ["START_STREAM", "STOP_STREAM", "START_STREAM", "STOP_STREAM", "START_STREAM"])
        self.assertEqual([m["type"] for m in agent_b.messages], ["START_STREAM", "STOP_STREAM", "START_STREAM", "STOP_STREAM"])
        self.assertEqual({m["device_id"] for m in agent_a.messages}, {"A"})
        self.assertEqual({m["device_id"] for m in agent_b.messages}, {"B"})


if __name__ == "__main__":
    unittest.main()
