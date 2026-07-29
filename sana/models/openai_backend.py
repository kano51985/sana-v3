from sana.models.backend import ModelBackend, ModelResponse

class OpenAIModelBackend(ModelBackend):
    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1"):
        self.api_key = api_key
        self.base_url = base_url
    def chat(self, model_id: str, messages: list[dict], **kwargs) -> ModelResponse:
        from openai import OpenAI
        client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        resp = client.chat.completions.create(
            model=model_id, messages=messages,
            **{k: v for k, v in kwargs.items() if k in ("temperature", "max_tokens", "timeout")}
        )
        return ModelResponse(content=resp.choices[0].message.content or "", raw=resp.model_dump_json())
