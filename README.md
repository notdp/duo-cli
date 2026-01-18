# duo-cli

CLI for duo multi-agent PR review.

## Installation

```bash
pipx install git+https://github.com/notdp/duo-cli.git
```

## Usage

```bash
# Send message to another agent
duo-cli send orchestrator "Review complete, no issues found"

# State management
duo-cli set stage 2
duo-cli get stage

# Check status
duo-cli status
duo-cli agents
duo-cli alive opus
duo-cli logs opus -f

# Interrupt agent
duo-cli interrupt opus

# Update settings
duo-cli settings opus --auto low

# Message history
duo-cli messages
duo-cli messages --last 10

# GitHub PR comments
duo-cli comment list
duo-cli comment get <node_id>
duo-cli comment edit <node_id> "new content"
duo-cli comment delete <node_id>
```

## Commands

| Command | Description |
|---------|-------------|
| `send <agent> <msg>` | Send message to agent |
| `set <key> <value>` | Set state value |
| `get <key>` | Get state value |
| `status` | Show swarm status |
| `agents` | List all agents |
| `alive <agent>` | Check if agent is alive |
| `logs <agent>` | Show agent logs |
| `interrupt <agent>` | Interrupt agent |
| `settings <agent>` | Update agent settings |
| `messages` | Show message history |
| `comment list` | List DUO comments |
| `comment get` | Get comment by node ID |
| `comment edit` | Edit comment |
| `comment delete` | Delete comment |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DROID_REPO` | Yes | Repository (owner/repo) |
| `DROID_PR_NUMBER` | Yes | PR number |
| `DROID_AGENT_NAME` | No | Current agent name (for send) |
| `GH_TOKEN` | No | GitHub token (workflow sets this) |

## License

MIT
