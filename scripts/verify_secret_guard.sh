#!/usr/bin/env bash
# Phase 0 验收：pre-commit 能拦下故意写入的假密钥、.env、登录态与抓包文件。
# 在临时克隆里操作，不会污染当前仓库。首次运行需要联网安装 gitleaks 钩子。
# 用法：bash scripts/verify_secret_guard.sh
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

git clone --quiet "$repo_root" "$work/repo"
# 把工作区里尚未提交的防护配置也带过去，保证验证的是当前版本
cp "$repo_root/.pre-commit-config.yaml" "$repo_root/.gitleaks.toml" "$work/repo/"
cp "$repo_root/scripts/forbid_sensitive_files.py" "$work/repo/scripts/"
cd "$work/repo"
git config user.email "verify@example.invalid"
git config user.name "verify-secret-guard"
git add .pre-commit-config.yaml .gitleaks.toml scripts/forbid_sensitive_files.py

# 假密钥在运行时拼出来，仓库里永远不出现完整的密钥形状
rand() { LC_ALL=C tr -dc "$1" </dev/urandom | head -c "$2" || true; }
fake_pat="gh""p_$(rand 'A-Za-z0-9' 36)"
fake_uuid="$(rand 'a-f0-9' 8)-$(rand 'a-f0-9' 4)-$(rand 'a-f0-9' 4)-$(rand 'a-f0-9' 4)-$(rand 'a-f0-9' 12)"
fake_hook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=${fake_uuid}"
fake_pwd="$(rand 'A-Za-z0-9' 16)"
pwd_var="SMAIL""_PASSWORD"

failures=0
# expect_blocked <说明> <应当报警的钩子 id> <文件> <内容>
expect_blocked() {
  local label="$1" hook="$2" file="$3" content="$4"
  mkdir -p "$(dirname "$file")"
  printf '%s\n' "$content" > "$file"
  git add -f "$file"
  if uv run --project "$repo_root" --no-sync pre-commit run "$hook" --files "$file" \
      >"$work/out.txt" 2>&1; then
    echo "FAIL  $label：$hook 没有拦下"
    sed 's/^/      /' "$work/out.txt"
    failures=$((failures + 1))
  else
    echo "PASS  $label：$hook 拦下"
  fi
  git rm --quiet --cached -f "$file"
  rm -f "$file"
}

expect_blocked "GitHub token 形状的假密钥" gitleaks "leak_token.py" "TOKEN = \"${fake_pat}\""
expect_blocked "企业微信 webhook" gitleaks "notes/leak_hook.md" "推送地址：${fake_hook}"
expect_blocked "邮箱口令赋值" gitleaks "config/leak.toml" "${pwd_var} = \"${fake_pwd}\""
expect_blocked ".env 文件" forbid-sensitive-files ".env" "${pwd_var}=${fake_pwd}"
expect_blocked "Playwright 登录态" forbid-sensitive-files "storage_state.json" '{"cookies": []}'
expect_blocked "HAR 抓包" forbid-sensitive-files "recon/session.har" '{"log": {"entries": []}}'

if [ "$failures" -gt 0 ]; then
  echo "共 $failures 项没有被拦下"
  exit 1
fi
echo "全部被拦下：pre-commit 密钥防护有效"
