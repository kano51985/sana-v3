import requests
from sana.models.backend import ModelBackend, ModelResponse

class LocalModelBackend(ModelBackend):
    def __init__(self, base_url: str = "http://localhost:1234/api/v1/chat"):
        self.base_url = base_url
    def chat(self, model_id: str, messages: list[dict], **kwargs) -> ModelResponse:
        payload = {"model": model_id, "input": messages[-1]["content"]}
        if kwargs.get("system_prompt"):
            payload["system_prompt"] = kwargs["system_prompt"]
        timeout = kwargs.get("timeout", 30)
        resp = requests.post(self.base_url, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        output = data.get("output", [])
        full = ""
        if len(output) > 1 and output[1].get("content"):
            full = output[1]["content"]
        elif len(output) > 0 and output[0].get("content"):
            full = output[0]["content"]
        return ModelResponse(content=full, raw=full)
