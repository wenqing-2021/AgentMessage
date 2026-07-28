"""Instructions that keep container projects on their approved execution path."""

CONTAINER_INSTRUCTIONS = (
    "This project executes inside a configured Docker container. You MUST use the container_run "
    "MCP tool exposed by agent_message_container for every project command, including shell "
    "inspection, dependency management, builds, tests, training, Git commands, and runtime "
    "checks. Pass the executable and arguments separately as container_run argv and use a "
    "project-relative cwd. Continue to read and edit files with the normal agent file tools: "
    "the WSL project directory and the container project directory are the same bind-mounted "
    "files. Never use the normal shell to infer the container's dependencies, devices, network, "
    "or runtime behavior."
)

# Resume can carry command-use habits from an older turn. Repeat the execution policy at the
# decision point even though the MCP server also exposes it as developer instructions.
CONTAINER_TURN_PREFIX = (
    f"[AgentMessage container execution policy]\n{CONTAINER_INSTRUCTIONS}\n\n"
)
