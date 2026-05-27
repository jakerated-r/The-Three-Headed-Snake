# macOS Install

```bash
git clone https://github.com/jakerated-r/The-Three-Headed-Snake.git
cd The-Three-Headed-Snake
chmod +x scripts/*.sh
```

Start the broker:

```bash
bash scripts/start-broker.sh
```

In another terminal:

```bash
curl http://127.0.0.1:17874/health
bash scripts/start-orchestrator.sh
bash scripts/chat.sh --send "Wake the room." --to Codex
```

Open the Terminal.app room:

```bash
bash scripts/open-chat-macos.sh
```

Install the orchestrator as a LaunchAgent:

```bash
bash scripts/install-launch-agent-macos.sh
```

Uninstall:

```bash
bash scripts/uninstall-launch-agent-macos.sh
```
