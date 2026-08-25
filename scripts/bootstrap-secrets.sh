#!/usr/bin/env bash
set -euo pipefail

secret_dir="/etc/finops-ai"
install -d -m 0750 -o root -g root "${secret_dir}"

IFS= read -r deepseek_key
if [[ -z "${deepseek_key}" || "${deepseek_key}" != sk-* ]]; then
  echo "Invalid DeepSeek key input" >&2
  exit 1
fi
printf '%s' "${deepseek_key}" > "${secret_dir}/deepseek_api_key"

if [[ ! -s "${secret_dir}/session_secret" ]]; then
  openssl rand -hex 48 > "${secret_dir}/session_secret"
fi
if [[ ! -s "${secret_dir}/pseudonym_secret" ]]; then
  openssl rand -hex 48 > "${secret_dir}/pseudonym_secret"
fi
if [[ ! -s "${secret_dir}/admin_password" ]]; then
  openssl rand -base64 24 | tr -d '\n' > "${secret_dir}/admin_password"
fi

chmod 0600 "${secret_dir}"/*
echo "SECRETS_BOOTSTRAPPED"
