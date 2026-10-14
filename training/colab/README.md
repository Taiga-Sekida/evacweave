# Colab QLoRA training

The notebook in this directory is the training path for role adapters. It follows the current Google Gemma QLoRA pattern: load a Gemma 4 base model in 4-bit, freeze the base weights, train PEFT/LoRA adapters, and save only the adapter artifacts.

Recommended first target for this project is `google/gemma-4-12B` with the instruction processor `google/gemma-4-12B-it`. Use A100 when available; L4 can be used for smaller experiments. The notebook does not publish anything automatically and does not call Vertex AI.

Expected JSONL rows use conversational messages:

```json
{"messages":[{"role":"system","content":"..."},{"role":"user","content":"..."},{"role":"assistant","content":"{\"action\":\"move\",...}"}]}
```

Keep training data synthetic or properly licensed. Do not put real resident records, credentials, or private operational data into the notebook. Each run writes an `adapter_manifest.json` next to the adapter weights so serving can record the role, base model, dataset label, and training parameters.

After training, stage the base model, processor, and selected role adapters under `/models` in the GPU serving image or equivalent read-only artifact mount. Model weights are intentionally excluded from Git.
