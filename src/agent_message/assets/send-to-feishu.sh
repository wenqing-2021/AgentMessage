#!/bin/sh
# AgentMessage: 把一个项目内文件发送到当前飞书对话。
# 用法: sh .agent-message/bin/send-to-feishu <项目内相对路径> [等待秒数]
# 脚本只写入项目内的 .agent-message/outbox，由 AgentMessage 服务完成上传与发送。
set -u

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "用法: sh .agent-message/bin/send-to-feishu <项目内相对路径> [等待秒数]" >&2
  exit 2
fi

target=$1
wait_seconds=${2:-60}

script_dir=$(CDPATH= cd "$(dirname "$0")" 2>/dev/null && pwd -P) || {
  echo "错误：无法定位脚本所在目录。" >&2
  exit 1
}
project_root=$(CDPATH= cd "$script_dir/../.." 2>/dev/null && pwd -P) || {
  echo "错误：无法定位项目根目录。" >&2
  exit 1
}

case "$target" in
  /*) candidate=$target ;;
  *) candidate=$(pwd -P)/$target ;;
esac
file_dir=$(CDPATH= cd "$(dirname "$candidate")" 2>/dev/null && pwd -P) || {
  echo "错误：文件目录不存在：$target" >&2
  exit 1
}
absolute="$file_dir/$(basename "$candidate")"
case "$absolute" in
  "$project_root"/*) relative=${absolute#"$project_root"/} ;;
  *)
    echo "错误：文件必须位于项目目录内：$target" >&2
    exit 1
    ;;
esac
if [ ! -f "$absolute" ]; then
  echo "错误：文件不存在或不是普通文件：$target" >&2
  exit 1
fi

spool="$project_root/.agent-message/outbox"
if ! mkdir -p "$spool" 2>/dev/null; then
  echo "错误：无法写入 .agent-message/outbox。" >&2
  exit 1
fi

nonce=$(date +%s 2>/dev/null || echo 0)-$$
base="$spool/$nonce"
counter=0
while [ -e "$base.req" ] || [ -e "$base.tmp" ]; do
  counter=$((counter + 1))
  base="$spool/$nonce-$counter"
done
request="$base.req"
result="$base.res"
if ! printf '%s\n' "$relative" > "$base.tmp" || ! mv "$base.tmp" "$request"; then
  echo "错误：无法写入发送请求。" >&2
  exit 1
fi

case "$wait_seconds" in
  ''|*[!0-9]*) wait_seconds=60 ;;
esac
if [ "$wait_seconds" -le 0 ]; then
  echo "已排队发送：$relative（不等待结果）"
  exit 0
fi

tick=0
limit=$((wait_seconds * 5))
while [ "$tick" -lt "$limit" ]; do
  if [ -f "$result" ]; then
    status=$(head -n 1 "$result" 2>/dev/null)
    message=$(sed -n '2,$p' "$result" 2>/dev/null)
    rm -f "$result" 2>/dev/null
    if [ "$status" = "ok" ]; then
      echo "已发送：$relative"
      exit 0
    fi
    echo "发送失败：$message" >&2
    exit 1
  fi
  sleep 0.2 2>/dev/null || sleep 1
  tick=$((tick + 1))
done
echo "超时：AgentMessage 服务未在 ${wait_seconds} 秒内处理发送请求。" >&2
exit 1
