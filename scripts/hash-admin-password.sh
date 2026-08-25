#!/usr/bin/env bash
set -euo pipefail

secret_dir="/etc/finops-ai"
docker run --rm -i finops-aws-china:2.0.0 \
  python -c 'import sys; from argon2 import PasswordHasher; print(PasswordHasher().hash(sys.stdin.read().strip()))' \
  < "${secret_dir}/admin_password" \
  > "${secret_dir}/admin_password_hash"
chmod 0600 "${secret_dir}/admin_password_hash"
echo "ADMIN_HASH_CREATED"
