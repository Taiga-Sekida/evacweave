"""Not deployed (roadmap): a lazy Gemma 4 + PEFT adapter runtime for Cloud Run GPU.

Heavy ML dependencies are imported only when the runtime is first constructed.
The service loads one base model and role adapters, and serializes adapter switches.
"""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any, Literal

from packages.domain.ai import AdapterRegistry, ModelIdentity, MoveDecisionRequest, MoveDecisionResponse
from packages.domain.models import MoveIntent
from packages.prompts.loader import load_prompt


class TransformersGemmaRuntime:
    def __init__(self, registry_path: str, *, role: str = "resident", max_new_tokens: int = 256) -> None:
        if not 32 <= max_new_tokens <= 512:
            raise ValueError("max_new_tokens must be between 32 and 512")
        raw = Path(registry_path).read_text(encoding="utf-8")
        self.registry = AdapterRegistry.model_validate_json(raw)
        if role != "resident":
            raise ValueError("current inference endpoint only supports the resident role")
        self.role: Literal["resident"] = "resident"
        self.max_new_tokens = max_new_tokens
        self._lock = Lock()
        self._load()

    def _load(self) -> None:  # pragma: no cover - needs the GPU stack (torch/transformers/peft)
        import torch
        from peft import PeftModel
        from transformers import AutoModelForMultimodalLM, AutoProcessor, BitsAndBytesConfig, pipeline

        dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        quantization = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=dtype,
            bnb_4bit_quant_storage=dtype,
        )
        base = AutoModelForMultimodalLM.from_pretrained(
            self.registry.base_model,
            device_map="auto",
            dtype=dtype,
            quantization_config=quantization,
            local_files_only=True,
        )
        self.processor = AutoProcessor.from_pretrained(
            self.registry.processor_model,
            local_files_only=True,
        )
        adapter = self.registry.for_role(self.role)
        self.model = PeftModel.from_pretrained(
            base,
            adapter.path,
            adapter_name=self.role,
            is_trainable=False,
        )
        self.pipe = pipeline(
            "text-generation",
            model=self.model,
            tokenizer=self.processor.tokenizer,
        )

    @staticmethod
    def _prompt(request: MoveDecisionRequest) -> list[dict[str, str]]:
        scenario = request.scenario
        persona = request.persona
        observation = {
            "persona": persona.model_dump(mode="json"),
            "deadline_s": scenario.deadline_s,
            "roads": [road.model_dump(mode="json") for road in scenario.roads],
            "shelters": [shelter.model_dump(mode="json") for shelter in scenario.shelters],
        }
        return [
            {
                "role": "system",
                "content": load_prompt("resident", "v1"),
            },
            {
                "role": "user",
                "content": json.dumps(observation, ensure_ascii=False, sort_keys=True),
            },
        ]

    def decide_move(self, request: MoveDecisionRequest) -> MoveDecisionResponse:
        request = MoveDecisionRequest.model_validate(request)
        adapter = self.registry.for_role("resident")
        started = perf_counter()
        messages = self._prompt(request)
        with self._lock:
            self.model.set_adapter(self.role)
            prompt = self.processor.tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
            )
            outputs: list[dict[str, Any]] = self.pipe(
                text_inputs=prompt,
                max_new_tokens=self.max_new_tokens,
                do_sample=False,
                return_full_text=False,
            )
        if len(outputs) != 1 or not isinstance(outputs[0].get("generated_text"), str):
            raise ValueError("unexpected Gemma generation result")
        text = outputs[0]["generated_text"].strip().removesuffix("<turn|>").strip()
        intent = MoveIntent.model_validate_json(text)
        if intent.actor_id != request.persona.id:
            raise ValueError("resident adapter acted as another persona")
        elapsed_ms = min(int((perf_counter() - started) * 1000), 600_000)
        return MoveDecisionResponse(
            request_id=request.request_id,
            model=ModelIdentity(
                base_model=self.registry.base_model,
                adapter_id=adapter.adapter_id,
            ),
            intent=intent,
            inference_ms=elapsed_ms,
        )
