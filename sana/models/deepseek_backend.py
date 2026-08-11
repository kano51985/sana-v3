from sana.models.backend import ModelBackend, ModelResponse
from sana.models.credentials import get_user_env

class DeepSeekBackend(ModelBackend):
    def __init__(self, api_key: str = "", base_url: str = "https://api.deepseek.com/v1"):
        self._key = api_key
        self.base_url = base_url

    def _get_key(self):
        # Always check env var live, so start.bat / Streamlit can provide the key at runtime.
        return self._key or get_user_env("DEEPSEEK_API_KEY")

    def chat(self, model_id: str, messages: list[dict], **kwargs) -> ModelResponse:
        from openai import OpenAI
        key = self._get_key()
        if not key:
            raise RuntimeError("DEEPSEEK_API_KEY is not set. Start with start.bat or enter the key in the model config panel.")
        client = OpenAI(api_key=key, base_url=self.base_url)
        params = {"model": model_id or "deepseek-chat", "messages": messages}
        for k in ("temperature", "max_tokens"):
            if k in kwargs:
                params[k] = kwargs[k]
        if kwargs.get("system_prompt"):
            params["messages"] = [{"role": "system", "content": kwargs["system_prompt"]}] + messages
        timeout = kwargs.get("timeout", 10)
        resp = client.chat.completions.create(**params, timeout=timeout)
        return ModelResponse(content=resp.choices[0].message.content or "", raw=resp.model_dump_json())
