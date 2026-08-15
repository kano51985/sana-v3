import os
import unittest
from types import SimpleNamespace
from unittest import mock

from sana.models.deepseek_backend import DeepSeekBackend
from sana.models.openai_backend import OpenAIModelBackend


def _sdk_response(content: str):
    response = mock.Mock()
    response.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]
    response.model_dump_json.return_value = '{"choices":[]}'
    return response


class ModelBackendKeyTest(unittest.TestCase):
    def test_deepseek_uses_env_fallback(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"}, clear=False):
            self.assertEqual(DeepSeekBackend()._get_key(), "sk-test")

    def test_openai_uses_env_fallback(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False):
            self.assertEqual(OpenAIModelBackend()._get_key(), "sk-test")

    def test_deepseek_raises_clear_error_without_key(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "sana.models.deepseek_backend.get_user_env",
            return_value="",
        ):
            with self.assertRaisesRegex(RuntimeError, "DEEPSEEK_API_KEY"):
                DeepSeekBackend().chat("model", [{"role": "user", "content": "x"}])

    def test_deepseek_uses_sdk_and_parses_response(self):
        client = mock.Mock()
        client.chat.completions.create.return_value = _sdk_response("ok")
        with mock.patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "sk-test"},
            clear=False,
        ), mock.patch(
            "openai.OpenAI",
            return_value=client,
        ) as openai:
            result = DeepSeekBackend().chat(
                "model",
                [{"role": "user", "content": "x"}],
                timeout=5,
            )
        self.assertEqual(result.content, "ok")
        openai.assert_called_once_with(
            api_key="sk-test",
            base_url="https://api.deepseek.com/v1",
        )
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "model")
        self.assertEqual(kwargs["timeout"], 5)
        self.assertEqual(kwargs["messages"], [{"role": "user", "content": "x"}])

    def test_deepseek_forwards_only_supported_options(self):
        client = mock.Mock()
        client.chat.completions.create.return_value = _sdk_response("ok")
        with mock.patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "sk-test"},
            clear=False,
        ), mock.patch(
            "openai.OpenAI",
            return_value=client,
        ):
            DeepSeekBackend().chat(
                "model",
                [{"role": "user", "content": "x"}],
                system_prompt="system",
                temperature=0.1,
                max_tokens=32,
                timeout=5,
                unsupported="ignored",
            )
        self.assertEqual(
            client.chat.completions.create.call_args.kwargs,
            {
                "model": "model",
                "messages": [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "x"},
                ],
                "temperature": 0.1,
                "max_tokens": 32,
                "timeout": 5,
            },
        )


if __name__ == "__main__":
    unittest.main()
