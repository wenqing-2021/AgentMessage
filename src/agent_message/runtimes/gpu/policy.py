"""Instructions that route GPU work through the approved sandbox."""

GPU_INSTRUCTIONS = (
    "This project has an AgentMessage GPU sandbox. You MUST use the gpu_run MCP tool exposed by "
    "agent_message_bwrap_gpu for every command that needs CUDA, a GPU, JAX GPU, "
    "nvidia-smi, GPU training, or GPU device detection. Pass the executable and arguments as "
    "gpu_run argv and a project-relative cwd. Never run such commands with the normal shell: "
    "that shell intentionally has no GPU device nodes. If a normal-shell command reports CPU-only "
    "or missing /dev/dxg, do not conclude that GPU is unavailable; rerun the check through "
    "gpu_run. Continue to use the normal shell for file edits and CPU-only commands."
)

# Resumed agent sessions can retain an earlier tool-use habit. Repeating the policy in the
# current user turn makes the newly attached MCP tool visible at the point where a choice is made.
GPU_TURN_PREFIX = f"[AgentMessage GPU execution policy]\n{GPU_INSTRUCTIONS}\n\n"
