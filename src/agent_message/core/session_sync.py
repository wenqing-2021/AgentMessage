"""Project-local, credential-free scripts for deferred session synchronization."""
from __future__ import annotations

import os
from pathlib import Path

SYNC_HINT = (
    '\n\n用户询问 Codex Chats 标题或列表时，请运行 '
    'sh .agent-message/bin/list-codex-chats 5（数量可为 1 到 50，默认 5）。'
    '它立即返回 bridge 在本轮开始时读取的当前项目未归档 Chats 标题，按创建时间从新到旧；'
    '飞书 /list 也能直接查询最新 5 个标题。请读取脚本输出并回复用户；不得因沙箱看不到宿主会话库就宣称无法查询。'
    '\n\n用户要求同步飞书与 Codex Chats 时，请执行项目内脚本：'
    'sh .agent-message/bin/sync-feishu-to-codex（默认同步当前会话，也可传飞书任务 ID），'
    '或 sh .agent-message/bin/sync-codex-to-feishu "Chats 标题"。'
    '按本项目规则通过 sandbox_run/container_run 执行。无需 uv、宿主 Codex 数据或配置路径。'
    '脚本只提交请求；提交后结束本轮，bridge 将在本轮结束后同步并单独通知飞书。'
    '不要等待同步结果，也不要把「已提交」说成「同步完成」。如果收到重名候选，请让用户选定，'
    '下一轮用候选的完整标题或 session ID 再提交。'
)


def safe_path(root: Path, relative: Path) -> Path:
    root = root.resolve()
    current = root
    for part in relative.parts:
        if part in {'.', '..'} or Path(part).is_absolute():
            raise ValueError('Invalid sync path')
        current = current / part
        if current.is_symlink():
            raise ValueError('Sync paths must not contain symlinks')
    return current


def install_sync_scripts(root: Path, token: str) -> None:
    directory = safe_path(root, Path('.agent-message/sync') / token)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o777)
    for direction in ('feishu-to-codex', 'codex-to-feishu'):
        path = safe_path(root, Path('.agent-message/bin') / ('sync-' + direction))
        path.parent.mkdir(parents=True, exist_ok=True)
        source = '''#!/bin/sh
set -eu
[ "$#" -le 1 ] || { echo '只接受一个会话标题或任务 ID。' >&2; exit 2; }
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
value=${1:-current}
'''
        if direction == 'codex-to-feishu':
            source += '''[ "$#" -eq 1 ] && [ -n "$1" ] || { echo '请输入 Chats 标题。' >&2; exit 2; }
'''
        source += f'''request=$(mktemp "$root/.agent-message/sync/{token}/sync-XXXXXXXX.tmp")
trap 'rm -f "$request"' EXIT HUP INT TERM
printf '%s\\n%s\\n' '{direction}' "$value" > "$request"
mv -- "$request" "${{request%.tmp}}.req"
echo '已提交同步请求；请结束本轮，bridge 随后在飞书通知结果。'
'''
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o755)
        with os.fdopen(fd, 'w') as handle:
            handle.write(source)


def install_chat_list(root: Path, token: str, titles: list[str], error: str | None = None) -> None:
    """Publish only this project's titles, not host paths, credentials or history."""
    snapshot = safe_path(root, Path('.agent-message/sync') / token / 'chats.txt')
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    content = ('Chats 列表读取失败：' + ' '.join(error.split()) + '\n') if error else (
        ''.join(f'{i}. {title}\n' for i, title in enumerate(titles, 1)) or '当前项目没有未归档的 Codex Chats。\n'
    )
    fd = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, 'w') as handle:
        handle.write(content)
    script = safe_path(root, Path('.agent-message/bin/list-codex-chats'))
    script.parent.mkdir(parents=True, exist_ok=True)
    source = '''#!/bin/sh
set -eu
[ "$#" -le 1 ] || { echo '用法：list-codex-chats [1-50]' >&2; exit 2; }
limit=${1:-5}
case "$limit" in ''|*[!0-9]*) echo '数量必须为 1 到 50。' >&2; exit 2;; esac
[ "$limit" -ge 1 ] && [ "$limit" -le 50 ] || { echo '数量必须为 1 到 50。' >&2; exit 2; }
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
'''
    source += f'head -n "$limit" "$root/.agent-message/sync/{token}/chats.txt"\n'
    source += f'exit {1 if error else 0}\n'
    fd = os.open(script, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o755)
    with os.fdopen(fd, 'w') as handle:
        handle.write(source)
