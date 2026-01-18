# duo-cli

CLI tools for duoduo multi-agent PR review.

## Installation

```bash
# First install the SDK
pip install ~/developer/droid-agent-sdk

# Then install duo-cli
pip install ~/developer/duo-cli
```

## Usage

```bash
# Send message to another agent
duo send orchestrator "Review complete, no issues found"

# State management
duo set stage 2
duo get stage

# Check status
duo status
duo agents
duo alive opus
duo logs opus -f
```

## Commands

| Command | Description | Example |
|---------|-------------|---------|
| `send <agent> <msg>` | Send message | `duo send orchestrator "done"` |
| `set <key> <value>` | Set state | `duo set stage 2` |
| `get <key>` | Get state | `duo get stage` |
| `status` | Show swarm | `duo status` |
| `agents` | List agents | `duo agents` |
| `alive <agent>` | Check alive | `duo alive opus` |
| `logs <agent>` | Show logs | `duo logs opus -f` |

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `DROID_PR_NUMBER` | PR number | Yes |
| `DROID_AGENT_NAME` | Current agent | For `send` |
| `DROID_REPO` | Repository | No |

## License

MIT
