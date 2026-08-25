#!/usr/bin/env bash
set -euo pipefail

: "${CUR_REPORT_NAME:?Set CUR_REPORT_NAME}"
: "${CUR_BUCKET:?Set CUR_BUCKET}"

cur_region="${CUR_REGION:-cn-northwest-1}"
cur_prefix="${CUR_PREFIX:-cur}"

definition_file="$(mktemp)"
trap 'rm -f "${definition_file}"' EXIT

sed \
  -e "s|__REPORT_NAME__|${CUR_REPORT_NAME}|g" \
  -e "s|__BUCKET__|${CUR_BUCKET}|g" \
  -e "s|__PREFIX__|${cur_prefix}|g" \
  -e "s|__REGION__|${cur_region}|g" \
  scripts/cur-report-definition.template.json > "${definition_file}"

aws cur put-report-definition \
  --region "${cur_region}" \
  --report-definition "file://${definition_file}"

aws cur describe-report-definitions \
  --region "${cur_region}" \
  --query "ReportDefinitions[?ReportName=='${CUR_REPORT_NAME}'].{Name:ReportName,Bucket:S3Bucket,Prefix:S3Prefix,Status:ReportStatus}"
