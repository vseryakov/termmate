import asyncio
import unittest
import sys
import os

# Ensure parent directory is in sys.path for genfoundry imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from genfoundry.claude_agent import ClaudeCodeAgent
from genfoundry.base_agent import AgentOptions, MessageType, AssistantMessage, TextBlock


def _collect_text(msg) -> str:
    """Extract text from an AssistantMessage, or return empty string."""
    if isinstance(msg, AssistantMessage):
        return "".join(
            block.text for block in msg.content if isinstance(block, TextBlock)
        )
    return ""


class TestClaudeCodeE2E(unittest.IsolatedAsyncioTestCase):
    """
    End-to-End tests for ClaudeCodeAgent using the real 'claude' binary.

    This test requires:
    1. 'claude' CLI installed and in PATH (or common install locations).
    2. Valid authentication (via 'claude login' or ANTHROPIC_API_KEY).
    """

    def setUp(self):
        # Remove CLAUDECODE so the claude CLI allows launching inside an existing session
        self._claudecode_saved = os.environ.pop("CLAUDECODE", None)

    def tearDown(self):
        if self._claudecode_saved is not None:
            os.environ["CLAUDECODE"] = self._claudecode_saved

    async def test_claude_interaction(self):
        """Verify that a real message can be sent and a response received."""
        opts = AgentOptions(cwd=".")
        try:
            agent = ClaudeCodeAgent(options=opts)
        except FileNotFoundError:
            self.skipTest("claude CLI not found in PATH")

        try:
            await agent.connect()

            # Simple deterministic prompt to minimize latency and variability
            await agent.send_message("Reply with exactly: Hello from Claude E2E")

            response_text = ""
            async for msg in agent.receive_messages():
                response_text += _collect_text(msg)
                if msg.type in (MessageType.STOP.value, "result"):
                    break

            self.assertIn("Hello from Claude E2E", response_text)

        except Exception as e:
            self.fail(f"Real Claude Code interaction failed: {e}")
        finally:
            await agent.disconnect()

    async def test_multi_turn_conversation(self):
        """Verify that a two-turn conversation maintains context."""
        opts = AgentOptions(cwd=".")
        try:
            agent = ClaudeCodeAgent(options=opts)
        except FileNotFoundError:
            self.skipTest("claude CLI not found in PATH")

        try:
            await agent.connect()

            # Turn 1: establish a value
            await agent.send_message("Remember the number 42. Reply with 'Remembered'.")

            turn1_text = ""
            async for msg in agent.receive_messages():
                turn1_text += _collect_text(msg)
                if msg.type in (MessageType.STOP.value, "result"):
                    break

            self.assertIn("Remembered", turn1_text)

            # Turn 2: verify context is retained
            await agent.send_message(
                "What number did I ask you to remember? Reply with just the number.")

            turn2_text = ""
            async for msg in agent.receive_messages():
                turn2_text += _collect_text(msg)
                if msg.type in (MessageType.STOP.value, "result"):
                    break

            self.assertIn("42", turn2_text)

        except Exception as e:
            self.fail(f"Multi-turn Claude Code interaction failed: {e}")
        finally:
            await agent.disconnect()


if __name__ == "__main__":
    unittest.main()
