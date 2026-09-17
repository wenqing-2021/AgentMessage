"""Instructions that route project commands through the approved bubblewrap sandbox."""

SANDBOX_INSTRUCTIONS = (
    "This project runs its commands inside an AgentMessage bubblewrap sandbox. You MUST use the "
    "sandbox_run MCP tool exposed by agent_message_sandbox for every project command, including "
    "builds, tests, training, package managers, Git writes, and runtime checks. Pass the "
    "executable and arguments as sandbox_run argv and use a project-relative cwd. Git commits "
    "and pushes only work through sandbox_run, because the normal shell intentionally keeps "
    "repository metadata read-only. The project directory, including .git, is writable inside "
    "the sandbox. Git commit identity and SSH agent forwarding may be configured globally by "
    "the operator; never copy private keys into the project or disable SSH host-key "
    "verification to fix authentication errors. Continue to use the normal shell for file "
    "edits only."
)

GPU_ADDENDUM = (
    " This project also enables GPU passthrough: run CUDA, JAX GPU, nvidia-smi, and GPU training "
    "or device checks through sandbox_run. The normal shell has no GPU device nodes, so a "
    "normal-shell CPU-only result or missing /dev/dxg does not mean the GPU is unavailable; "
    "rerun the check through sandbox_run."
)


def sandbox_instructions(gpu: bool) -> str:
    return SANDBOX_INSTRUCTIONS + (GPU_ADDENDUM if gpu else "")


def sandbox_turn_prefix(gpu: bool) -> str:
    """Repeat the policy at the current turn: resumed sessions can keep an older tool habit."""
    return f"[AgentMessage sandbox execution policy]\n{sandbox_instructions(gpu)}\n\n"
