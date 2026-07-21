import json
import unittest

from zellij_tab_namer.llm import LLMConfig, compress_label, config_from_env
from zellij_tab_namer.naming import NamerConfig, label_for_pane


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeOpener:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {
            "choices": [{"message": {"content": "skills/agents monorepo"}}]
        }
        self.error = error
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return FakeResponse(self.payload)


class LLMCompressionTests(unittest.TestCase):
    def test_compresses_label_with_openai_compatible_request(self):
        opener = FakeOpener()
        config = LLMConfig(
            base_url="http://localhost:11434/v1",
            api_key="ollama",
            model="llama3.2",
            timeout_seconds=0.5,
        )

        label = compress_label(
            "create monorepo for skills and agents",
            max_chars=24,
            config=config,
            opener=opener,
        )

        self.assertEqual(label, "skills/agents monorepo")
        request, timeout = opener.requests[0]
        self.assertEqual(timeout, 0.5)
        self.assertEqual(
            request.full_url, "http://localhost:11434/v1/chat/completions"
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer ollama")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "llama3.2")
        combined_messages = json.dumps(body["messages"])
        self.assertIn("create monorepo for skills and agents", combined_messages)
        self.assertIn("24", combined_messages)
        self.assertNotIn("scrollback", combined_messages.lower())
        self.assertNotIn("pane_text", combined_messages.lower())

    def test_unconfigured_endpoint_returns_none(self):
        self.assertIsNone(
            compress_label(
                "create monorepo for skills and agents",
                max_chars=24,
                config=LLMConfig(base_url="", api_key=None, model="llama3.2"),
                opener=FakeOpener(),
            )
        )

    def test_overlong_failed_or_malformed_model_output_returns_none(self):
        overlong = FakeOpener(
            payload={
                "choices": [
                    {
                        "message": {
                            "content": "this label is far too long for the configured tab"
                        }
                    }
                ]
            }
        )
        malformed = FakeOpener(payload={"choices": []})
        failed = FakeOpener(error=TimeoutError("slow model"))
        config = LLMConfig(
            base_url="http://localhost:11434/v1",
            api_key=None,
            model="llama3.2",
            timeout_seconds=0.2,
        )

        self.assertIsNone(
            compress_label("create monorepo for skills and agents", 12, config, overlong)
        )
        self.assertIsNone(
            compress_label(
                "create monorepo for skills and agents",
                24,
                config,
                malformed,
            )
        )
        self.assertIsNone(
            compress_label("create monorepo for skills and agents", 24, config, failed)
        )

    def test_naming_uses_compressor_for_long_titles(self):
        def compressor(label, max_chars):
            self.assertEqual(label, "create monorepo for skills and agents")
            self.assertEqual(max_chars, 24)
            return "skills/agents monorepo"

        label, reason = label_for_pane(
            {
                "title": "create monorepo for skills and agents",
                "pane_command": "claude",
                "pane_cwd": "/Users/eduardoleal/Turno/platform-hub",
            },
            NamerConfig(max_chars=24),
            compressor=compressor,
        )

        self.assertEqual(label, "skills/agents monorepo")
        self.assertEqual(reason, "title")

    def test_config_from_env(self):
        env = {
            "ZELLIJ_TAB_NAMER_BASE_URL": "http://localhost:11434/v1/",
            "ZELLIJ_TAB_NAMER_API_KEY": "ollama",
            "ZELLIJ_TAB_NAMER_MODEL": "llama3.2",
            "ZELLIJ_TAB_NAMER_TIMEOUT": "1.25",
        }

        config = config_from_env(env)

        self.assertEqual(config.base_url, "http://localhost:11434/v1")
        self.assertEqual(config.api_key, "ollama")
        self.assertEqual(config.model, "llama3.2")
        self.assertEqual(config.timeout_seconds, 1.25)


if __name__ == "__main__":
    unittest.main()
