import asyncio
import unittest
import sys
import os
from genfoundry.codex_agent import CodexAgent
from genfoundry.base_agent import AgentOptions, MessageType

# Ensure parent directory is in sys.path for genfoundry imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

class TestCodexE2E(unittest.IsolatedAsyncioTestCase):
    """
    End-to-End tests for CodexAgent using the real 'codex' binary.

    This test requires:
    1. 'codex' CLI installed and in PATH.
    2. Valid authentication (via 'codex login' or CODEX_API_KEY).
    """

    async def test_real_codex_interaction(self):
        """Verify that a real message can be sent and a response received."""
        # Using default options, which searches for 'codex' in PATH
        opts = AgentOptions(cwd=".")
        agent = CodexAgent(options=opts)

        try:
            await agent.connect()

            # Simple prompt to minimize latency and variability
            await agent.send_message("Say exactly 'Hello from E2E'")

            response_text = ""
            async for msg in agent.receive_messages():
                if msg.type == MessageType.TEXT.value:
                    response_text += msg.content
                if msg.type == MessageType.STOP.value:
                    break

            self.assertIn("Hello from E2E", response_text)

        except FileNotFoundError:
            self.skipTest("codex CLI not found in PATH")
        except Exception as e:
            self.fail(f"Real Codex interaction failed: {e}")
        finally:
            await agent.disconnect()

if __name__ == "__main__":
    unittest.main()
