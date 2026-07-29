import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Sana Agent CLI")
    parser.add_argument("--backend", choices=["local","openai","deepseek"], default="deepseek", help="LLM backend")
    parser.add_argument("--api-key", help="API key for DeepSeek/OpenAI")
    parser.add_argument("--model", help="Override model ID")
    args = parser.parse_args()

    if args.backend != "local":
        from sana.config import registry, ModelConfig
        if args.backend == "deepseek":
            if args.api_key:
                from sana.models.deepseek_backend import DeepSeekBackend
                registry.backends["deepseek"] = DeepSeekBackend(api_key=args.api_key)
            mid = args.model or "deepseek-chat"
            registry.models["chat"] = ModelConfig(model_id=mid, backend_name="deepseek", params={"temperature": 0.8})
            registry.models["perception"] = ModelConfig(model_id=mid, backend_name="deepseek", params={"temperature": 0.1})
        elif args.backend == "openai":
            if args.api_key:
                from sana.models.openai_backend import OpenAIModelBackend
                registry.backends["openai"] = OpenAIModelBackend(api_key=args.api_key)
            mid = args.model or "gpt-4o-mini"
            registry.models["chat"] = ModelConfig(model_id=mid, backend_name="openai", params={"temperature": 0.8})
            registry.models["perception"] = ModelConfig(model_id=mid, backend_name="openai", params={"temperature": 0.1})

    from sana.agent import SanaAgent
    sana = SanaAgent()
    print(f"Sana ready! [backend={args.backend}] Type 'quit' to exit.\n")
    while True:
        try:
            user = input("\n[You]: ")
        except (EOFError, KeyboardInterrupt):
            print("\nBye!"); break
        if user.lower() in ("quit","exit","q"):
            print("Bye!"); break
        if not user.strip():
            continue
        resp = sana.chat(user)
        print(f"[Sana]: {resp.get('chat', '')}")

if __name__ == "__main__":
    main()
