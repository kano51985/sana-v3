import os
import json
import unittest
from unittest import mock

import requests

from sana.models.deepseek_backend import DeepSeekBackend
from sana.models.openai_backend import OpenAIModelBackend


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        return self.payload


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

    def test_deepseek_uses_requests_and_parses_response(self):
        response = FakeResponse({"choices": [{"message": {"content": "ok"}}]})
        with mock.patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "sk-test"},
            clear=False,
        ), mock.patch(
            "sana.models.deepseek_backend.requests.post",
            return_value=response,
        ) as post:
            result = DeepSeekBackend().chat(
                "model",
                [{"role": "user", "content": "x"}],
                timeout=5,
            )
        self.assertEqual(result.content, "ok")
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["json"]["model"], "model")
        self.assertEqual(kwargs["timeout"], 5)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-test")

    def test_deepseek_retries_connection_error_then_succeeds(self):
        response = FakeResponse({"choices": [{"message": {"content": "ok"}}]})
        with mock.patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "sk-test"},
            clear=False,
        ), mock.patch(
            "sana.models.deepseek_backend.requests.post",
            side_effect=[requests.ConnectionError("bad"), response],
        ) as post, mock.patch(
            "sana.models.deepseek_backend.time.sleep",
        ):
            result = DeepSeekBackend().chat(
                "model",
                [{"role": "user", "content": "x"}],
                timeout=5,
            )
        self.assertEqual(post.call_count, 2)
        self.assertEqual(result.content, "ok")


if __name__ == "__main__":
    unittest.main()
