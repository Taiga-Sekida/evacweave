# Development boundary

Work on feature branches; do not push changes directly to main, merge, force-push or change repository visibility without instruction.
Never call paid model APIs, provision cloud resources or use personal records in routine tests.
Keep Domain, Simulation, Agent and external service boundaries separate.
Run `python -m pytest -q` before proposing a change. Do not silently update golden expectations to make a failing test pass.
Do not represent fixture providers as Gemini, or synthetic PASS as real-world safety.
