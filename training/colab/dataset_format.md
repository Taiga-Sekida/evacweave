# Role-adapter dataset format

One JSON object per line. Each object must have exactly one `messages` array with system, user and assistant turns. The assistant turn is the supervised target.

For the current resident adapter, the assistant target is a single `MoveIntent` JSON object. Training examples should only reference roads and shelters present in the supplied synthetic scenario. The simulator remains the authority that determines whether the generated route actually succeeds.

Other roles will get their own strict output contracts before their adapters are trained; do not train shelter/red-team/repair outputs against an undocumented free-form schema.
