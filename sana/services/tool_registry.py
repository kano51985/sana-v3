from dataclasses import dataclass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict


class ToolRegistry:
    def __init__(self):
        self._tools = {
            "web": ToolSpec(
                name="web",
                description="联网查询实时、外部、事实或时效信息。",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            ),
            "memory": ToolSpec(
                name="memory",
                description="读取原始对话批次，用于查看更详细的过往对话。",
                input_schema={
                    "type": "object",
                    "properties": {"batch_id": {"type": "string"}},
                    "required": ["batch_id"],
                },
            ),
        }

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def descriptions(self) -> str:
        return "\n".join(
            f"- {name}: {spec.description}"
            for name, spec in self._tools.items()
        )
