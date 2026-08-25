from __future__ import annotations

import hashlib
import json
import math
import statistics
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings
from app.services.cache import SQLiteStore
from app.services.security import SecurityService


@dataclass(frozen=True)
class AccountSpec:
    alias: str
    role_arn: str | None


class SessionBroker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base = boto3.Session(region_name=settings.billing_region)
        self.accounts = [AccountSpec(settings.account_alias_a, None)]
        if settings.account_b_role_arn:
            self.accounts.append(AccountSpec(settings.account_alias_b, settings.account_b_role_arn))
        self._sessions: dict[str, tuple[float, boto3.Session]] = {}
        self._lock = threading.RLock()
        self._config = Config(
            retries={"max_attempts": 5, "mode": "adaptive"},
            connect_timeout=5,
            read_timeout=60,
            user_agent_extra="aws-china-finops-poc/2.0",
        )

    def session(self, account: AccountSpec) -> boto3.Session:
        if not account.role_arn:
            return self.base
        now = time.time()
        with self._lock:
            cached = self._sessions.get(account.alias)
            if cached and cached[0] > now + 300:
                return cached[1]
            sts = self.base.client("sts", region_name=self.settings.billing_region, config=self._config)
            assume_role_request = {
                "RoleArn": account.role_arn,
                "RoleSessionName": "finops-phase2-collector",
                "DurationSeconds": 3600,
            }
            if self.settings.account_b_external_id:
                assume_role_request["ExternalId"] = self.settings.account_b_external_id
            response = sts.assume_role(**assume_role_request)
            credentials = response["Credentials"]
            session = boto3.Session(
                aws_access_key_id=credentials["AccessKeyId"],
                aws_secret_access_key=credentials["SecretAccessKey"],
                aws_session_token=credentials["SessionToken"],
                region_name=self.settings.billing_region,
            )
            self._sessions[account.alias] = (credentials["Expiration"].timestamp(), session)
            return session

    def client(self, account: AccountSpec, service: str, region: str | None = None):
        return self.session(account).client(
            service,
            region_name=region or self.settings.billing_region,
            config=self._config,
        )


class AWSCollector:
    DIMENSIONS = {
        "SERVICE",
        "REGION",
        "USAGE_TYPE",
        "INSTANCE_TYPE",
        "RECORD_TYPE",
        "OPERATION",
        "PURCHASE_TYPE",
        "LINKED_ACCOUNT",
    }
    METRICS = {"UnblendedCost", "AmortizedCost", "NetAmortizedCost", "NetUnblendedCost", "UsageQuantity"}

    def __init__(self, settings: Settings, store: SQLiteStore, security: SecurityService):
        self.settings = settings
        self.store = store
        self.security = security
        self.broker = SessionBroker(settings)

    @staticmethod
    def _json_default(value: Any) -> str:
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return str(value)

    def _cache_key(self, name: str, params: dict[str, Any]) -> str:
        payload = json.dumps(params, sort_keys=True, default=self._json_default)
        return f"{name}:{hashlib.sha256(payload.encode()).hexdigest()}"

    def cached(self, name: str, params: dict[str, Any], ttl: int, loader: Callable[[], Any]) -> Any:
        key = self._cache_key(name, params)
        cached = self.store.get_cache(key)
        if cached is not None:
            return cached
        result = loader()
        self.store.set_cache(key, result, ttl)
        return result

    def _safe_error(self, exc: Exception) -> str:
        if isinstance(exc, ClientError):
            error = exc.response.get("Error", {})
            return f"{error.get('Code', 'AWS_ERROR')}: {error.get('Message', 'AWS request failed')}"
        return self.security.redact_text(str(exc))

    def _fanout(self, collector: str, callback: Callable[[AccountSpec], Any]) -> dict[str, Any]:
        data: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for account in self.broker.accounts:
            started = int(time.time())
            try:
                value = callback(account)
                data.append({"account_alias": account.alias, "result": self.security.sanitize(value)})
                self.store.add_run(collector, account.alias, None, "OK", "", started)
            except (ClientError, BotoCoreError, ValueError, RuntimeError) as exc:
                detail = self._safe_error(exc)
                errors.append({"account_alias": account.alias, "error": detail})
                self.store.add_run(collector, account.alias, None, "ERROR", detail, started)
        return {
            "status": "OK" if not errors else ("PARTIAL" if data else "ERROR"),
            "partial": bool(errors and data),
            "data": data,
            "errors": errors,
        }

    def accounts_status(self) -> dict[str, Any]:
        def check(account: AccountSpec) -> dict[str, Any]:
            identity = self.broker.client(account, "sts").get_caller_identity()
            return {"status": "AVAILABLE", "credential_source": "INSTANCE_PROFILE" if not account.role_arn else "STS_ASSUME_ROLE", "verified": bool(identity.get("Account"))}

        return self.cached("accounts", {}, 900, lambda: self._fanout("accounts", check))

    @staticmethod
    def default_period() -> tuple[str, str]:
        today = date.today()
        return today.replace(day=1).isoformat(), (today + timedelta(days=1)).isoformat()

    def _ce_pages(self, client, operation: str, params: dict[str, Any], result_key: str) -> list[Any]:
        method = getattr(client, operation)
        results: list[Any] = []
        token: str | None = None
        while True:
            request = dict(params)
            if token:
                request["NextPageToken"] = token
            response = method(**request)
            value = response.get(result_key, [])
            results.extend(value if isinstance(value, list) else [value])
            token = response.get("NextPageToken")
            if not token:
                break
        return results

    def cost_and_usage(
        self,
        start: str,
        end: str,
        granularity: str,
        metric: str,
        dimension: str | None = None,
    ) -> dict[str, Any]:
        if metric not in self.METRICS:
            raise ValueError("Unsupported metric")
        granularity = granularity.upper()
        if granularity not in {"DAILY", "MONTHLY"}:
            raise ValueError("Granularity must be DAILY or MONTHLY")
        if dimension and dimension not in self.DIMENSIONS - {"LINKED_ACCOUNT"}:
            raise ValueError("Unsupported dimension")
        params = {"start": start, "end": end, "granularity": granularity, "metric": metric, "dimension": dimension}

        def load() -> dict[str, Any]:
            def query(account: AccountSpec) -> Any:
                ce = self.broker.client(account, "ce", self.settings.billing_region)
                request: dict[str, Any] = {
                    "TimePeriod": {"Start": start, "End": end},
                    "Granularity": granularity,
                    "Metrics": [metric],
                }
                if dimension:
                    request["GroupBy"] = [{"Type": "DIMENSION", "Key": dimension}]
                return self._ce_pages(ce, "get_cost_and_usage", request, "ResultsByTime")

            return self._fanout("cost_and_usage", query)

        return self.cached("cost_and_usage", params, self.settings.api_cache_ttl_seconds, load)

    def cost_summary(self, start: str, end: str, metric: str = "UnblendedCost") -> dict[str, Any]:
        raw = self.cost_and_usage(start, end, "MONTHLY", metric)
        rows: list[dict[str, Any]] = []
        total = 0.0
        currency = "CNY"
        for account in raw["data"]:
            amount = 0.0
            for period in account["result"]:
                metric_value = period.get("Total", {}).get(metric, {})
                amount += float(metric_value.get("Amount") or 0)
                currency = metric_value.get("Unit") or currency
            rows.append({"account_alias": account["account_alias"], "amount": round(amount, 6)})
            total += amount
        return {**raw, "source": ["COST_EXPLORER"], "time_range": {"start": start, "end": end}, "metric": metric, "currency": currency, "total": round(total, 6), "accounts": rows, "data": rows}

    def cost_trend(self, start: str, end: str, metric: str = "UnblendedCost") -> dict[str, Any]:
        raw = self.cost_and_usage(start, end, "DAILY", metric)
        rows: list[dict[str, Any]] = []
        for account in raw["data"]:
            for period in account["result"]:
                value = period.get("Total", {}).get(metric, {})
                rows.append({
                    "date": period.get("TimePeriod", {}).get("Start"),
                    "account_alias": account["account_alias"],
                    "amount": round(float(value.get("Amount") or 0), 6),
                })
        return {**raw, "source": ["COST_EXPLORER"], "time_range": {"start": start, "end": end}, "metric": metric, "currency": "CNY", "data": rows}

    def cost_breakdown(self, start: str, end: str, dimension: str, metric: str = "UnblendedCost") -> dict[str, Any]:
        raw = self.cost_and_usage(start, end, "MONTHLY", metric, dimension)
        totals: dict[str, float] = {}
        for account in raw["data"]:
            for period in account["result"]:
                for group in period.get("Groups", []):
                    key = group.get("Keys", ["Unknown"])[0]
                    amount = float(group.get("Metrics", {}).get(metric, {}).get("Amount") or 0)
                    totals[key] = totals.get(key, 0.0) + amount
        rows = [{"name": key, "amount": round(value, 6)} for key, value in sorted(totals.items(), key=lambda item: item[1], reverse=True)]
        return {**raw, "source": ["COST_EXPLORER"], "time_range": {"start": start, "end": end}, "dimension": dimension, "metric": metric, "currency": "CNY", "data": rows}

    def cost_compare(self, start: str, end: str, previous_start: str, previous_end: str, metric: str = "UnblendedCost") -> dict[str, Any]:
        current = self.cost_summary(start, end, metric)
        previous = self.cost_summary(previous_start, previous_end, metric)
        delta = current["total"] - previous["total"]
        percent = None if previous["total"] == 0 else delta / previous["total"] * 100
        return {
            "status": "PARTIAL" if current["status"] != "OK" or previous["status"] != "OK" else "OK",
            "source": ["COST_EXPLORER"],
            "currency": "CNY",
            "current": current["total"],
            "previous": previous["total"],
            "delta": round(delta, 6),
            "delta_percent": round(percent, 4) if percent is not None else None,
            "time_range": {"start": start, "end": end},
            "previous_time_range": {"start": previous_start, "end": previous_end},
        }

    def forecast(self, start: str, end: str, metric: str = "UNBLENDED_COST") -> dict[str, Any]:
        allowed = {"UNBLENDED_COST", "AMORTIZED_COST", "NET_UNBLENDED_COST", "NET_AMORTIZED_COST"}
        if metric not in allowed:
            raise ValueError("Unsupported forecast metric")

        def load() -> dict[str, Any]:
            def query(account: AccountSpec) -> Any:
                ce = self.broker.client(account, "ce", self.settings.billing_region)
                return ce.get_cost_forecast(
                    TimePeriod={"Start": start, "End": end},
                    Metric=metric,
                    Granularity="MONTHLY",
                )

            return self._fanout("forecast", query)

        result = self.cached("forecast", {"start": start, "end": end, "metric": metric}, 21600, load)
        total = sum(float(item["result"].get("Total", {}).get("Amount") or 0) for item in result["data"])
        return {**result, "source": ["COST_EXPLORER"], "time_range": {"start": start, "end": end}, "currency": "CNY", "total": round(total, 6)}

    def cost_resources(self, start: str, end: str, service: str = "Amazon Elastic Compute Cloud - Compute") -> dict[str, Any]:
        def load() -> dict[str, Any]:
            def query(account: AccountSpec) -> Any:
                ce = self.broker.client(account, "ce", self.settings.billing_region)
                request = {
                    "TimePeriod": {"Start": start, "End": end},
                    "Granularity": "DAILY",
                    "Metrics": ["UnblendedCost"],
                    "Filter": {"Dimensions": {"Key": "SERVICE", "Values": [service]}},
                    "GroupBy": [{"Type": "DIMENSION", "Key": "RESOURCE_ID"}],
                }
                return self._ce_pages(ce, "get_cost_and_usage_with_resources", request, "ResultsByTime")

            return self._fanout("cost_resources", query)

        result = self.cached("cost_resources", {"start": start, "end": end, "service": service}, 21600, load)
        result.update({"source": ["COST_EXPLORER"], "time_range": {"start": start, "end": end}, "currency": "CNY"})
        return result

    def anomalies(self, start: str, end: str) -> dict[str, Any]:
        def load() -> dict[str, Any]:
            def query(account: AccountSpec) -> Any:
                ce = self.broker.client(account, "ce", self.settings.billing_region)
                monitors = self._ce_pages(ce, "get_anomaly_monitors", {"MaxResults": 100}, "AnomalyMonitors")
                if not monitors:
                    return {"configuration_status": "NOT_CONFIGURED", "monitors": [], "anomalies": []}
                anomalies = self._ce_pages(
                    ce,
                    "get_anomalies",
                    {"DateInterval": {"StartDate": start, "EndDate": end}, "MaxResults": 100},
                    "Anomalies",
                )
                return {"configuration_status": "CONFIGURED", "monitors": monitors, "anomalies": anomalies}

            return self._fanout("anomalies", query)

        result = self.cached("anomalies", {"start": start, "end": end}, 28800, load)
        result.update({"source": ["COST_EXPLORER_COST_ANOMALY_DETECTION"], "time_range": {"start": start, "end": end}, "currency": "CNY"})
        return result

    def co_recommendations(self, resource_type: str = "summary") -> dict[str, Any]:
        methods = {
            "summary": ("get_recommendation_summaries", "recommendationSummaries"),
            "ec2": ("get_ec2_instance_recommendations", "instanceRecommendations"),
            "asg": ("get_auto_scaling_group_recommendations", "autoScalingGroupRecommendations"),
            "ebs": ("get_ebs_volume_recommendations", "volumeRecommendations"),
            "lambda": ("get_lambda_function_recommendations", "lambdaFunctionRecommendations"),
            "ecs": ("get_ecs_service_recommendations", "ecsServiceRecommendations"),
            "licenses": ("get_license_recommendations", "licenseRecommendations"),
        }
        if resource_type not in methods:
            raise ValueError("Unsupported Compute Optimizer resource type")
        operation, result_key = methods[resource_type]

        def load() -> dict[str, Any]:
            data: list[dict[str, Any]] = []
            errors: list[dict[str, str]] = []
            for account in self.broker.accounts:
                for region in self.settings.region_list:
                    started = int(time.time())
                    try:
                        client = self.broker.client(account, "compute-optimizer", region)
                        response = getattr(client, operation)()
                        items = response.get(result_key, [])
                        while response.get("nextToken"):
                            response = getattr(client, operation)(nextToken=response["nextToken"])
                            items.extend(response.get(result_key, []))
                        data.append({"account_alias": account.alias, "region": region, "items": self.security.sanitize(items)})
                        self.store.add_run("compute_optimizer", account.alias, region, "OK", "", started)
                    except (ClientError, BotoCoreError) as exc:
                        detail = self._safe_error(exc)
                        errors.append({"account_alias": account.alias, "region": region, "error": detail})
                        self.store.add_run("compute_optimizer", account.alias, region, "ERROR", detail, started)
            return {"status": "OK" if not errors else ("PARTIAL" if data else "ERROR"), "partial": bool(errors and data), "data": data, "errors": errors}

        result = self.cached("co_recommendations", {"resource_type": resource_type}, 86400, load)
        result.update({"source": ["COMPUTE_OPTIMIZER"], "resource_type": resource_type})
        return result

    def rightsizing(self) -> dict[str, Any]:
        def load() -> dict[str, Any]:
            def query(account: AccountSpec) -> Any:
                ce = self.broker.client(account, "ce", self.settings.billing_region)
                response = ce.get_rightsizing_recommendation(Service="AmazonEC2", Configuration={"RecommendationTarget": "SAME_INSTANCE_FAMILY", "BenefitsConsidered": True})
                return response.get("RightsizingRecommendations", [])

            return self._fanout("rightsizing", query)

        result = self.cached("rightsizing", {}, 86400, load)
        result.update({"source": ["COST_EXPLORER", "COMPUTE_OPTIMIZER"], "currency": "CNY"})
        return result

    def commitments(self, kind: str, mode: str, start: str, end: str) -> dict[str, Any]:
        key = f"{kind}_{mode}"
        operations = {
            "ri_coverage": ("get_reservation_coverage", "CoveragesByTime"),
            "ri_utilization": ("get_reservation_utilization", "UtilizationsByTime"),
            "ri_recommendations": ("get_reservation_purchase_recommendation", "Recommendations"),
            "sp_coverage": ("get_savings_plans_coverage", "SavingsPlansCoverages"),
            "sp_utilization": ("get_savings_plans_utilization", "SavingsPlansUtilizationsByTime"),
            "sp_recommendations": ("get_savings_plans_purchase_recommendation", "SavingsPlansPurchaseRecommendation"),
        }
        if key not in operations:
            raise ValueError("Unsupported commitment query")
        operation, result_key = operations[key]

        def load() -> dict[str, Any]:
            def query(account: AccountSpec) -> Any:
                ce = self.broker.client(account, "ce", self.settings.billing_region)
                if mode == "recommendations" and kind == "ri":
                    params: dict[str, Any] = {"Service": "Amazon Elastic Compute Cloud - Compute"}
                elif mode == "recommendations":
                    params = {"SavingsPlansType": "COMPUTE_SP", "TermInYears": "ONE_YEAR", "PaymentOption": "NO_UPFRONT", "LookbackPeriodInDays": "THIRTY_DAYS"}
                else:
                    params = {"TimePeriod": {"Start": start, "End": end}, "Granularity": "MONTHLY"}
                response = getattr(ce, operation)(**params)
                return response.get(result_key, response)

            return self._fanout("commitments", query)

        result = self.cached("commitments", {"kind": kind, "mode": mode, "start": start, "end": end}, 86400, load)
        result.update({"source": ["COST_EXPLORER"], "currency": "CNY", "commitment_type": kind, "mode": mode})
        return result

    def pricing_products(self, service_code: str, filters: dict[str, str], max_results: int = 20) -> dict[str, Any]:
        allowed_services = {"AmazonEC2", "AmazonRDS", "AmazonS3", "AmazonCloudWatch", "AmazonECS", "AWSLambda"}
        if service_code not in allowed_services:
            raise ValueError("Unsupported service code")
        allowed_filter_keys = {"location", "instanceType", "databaseEngine", "deploymentOption", "operatingSystem", "tenancy", "capacitystatus", "productFamily", "usagetype"}
        clean_filters = {key: value for key, value in filters.items() if key in allowed_filter_keys and value}

        def load() -> dict[str, Any]:
            account = self.broker.accounts[0]
            client = self.broker.client(account, "pricing", self.settings.pricing_region)
            request = {
                "ServiceCode": service_code,
                "Filters": [{"Type": "TERM_MATCH", "Field": key, "Value": value} for key, value in clean_filters.items()],
                "MaxResults": min(max_results, 100),
            }
            response = client.get_products(**request)
            products = [json.loads(item) for item in response.get("PriceList", [])]
            return {"products": self.security.sanitize(products), "next_token": response.get("NextToken")}

        result = self.cached("pricing", {"service": service_code, "filters": clean_filters, "max": max_results}, 86400, load)
        return {"status": "OK", "source": ["AWS_CHINA_PRICE_LIST"], "currency": "CNY", "data": result["products"], "next_token": result.get("next_token")}

    def _athena_query(self, sql: str, max_wait_seconds: int = 90) -> list[dict[str, str | None]]:
        account = self.broker.accounts[0]
        client = self.broker.client(account, "athena", self.settings.athena_region)
        response = client.start_query_execution(
            QueryString=sql,
            QueryExecutionContext={"Database": self.settings.athena_database},
            WorkGroup=self.settings.athena_workgroup,
        )
        query_id = response["QueryExecutionId"]
        deadline = time.time() + max_wait_seconds
        while time.time() < deadline:
            execution = client.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]
            state = execution["Status"]["State"]
            if state == "SUCCEEDED":
                break
            if state in {"FAILED", "CANCELLED"}:
                raise RuntimeError(execution["Status"].get("StateChangeReason", state))
            time.sleep(1)
        else:
            client.stop_query_execution(QueryExecutionId=query_id)
            raise RuntimeError("Athena query timed out")
        rows: list[dict[str, str | None]] = []
        token: str | None = None
        columns: list[str] = []
        first_page = True
        while True:
            params: dict[str, Any] = {"QueryExecutionId": query_id, "MaxResults": 1000}
            if token:
                params["NextToken"] = token
            page = client.get_query_results(**params)
            if not columns:
                columns = [item["Name"] for item in page["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]]
            page_rows = page["ResultSet"]["Rows"]
            if first_page and page_rows:
                page_rows = page_rows[1:]
                first_page = False
            for row in page_rows:
                values = [cell.get("VarCharValue") for cell in row.get("Data", [])]
                rows.append(dict(zip(columns, values + [None] * (len(columns) - len(values)))))
            token = page.get("NextToken")
            if not token:
                break
        return rows

    def reconciliation(self, start: str, end: str, metric: str = "UnblendedCost") -> dict[str, Any]:
        if metric != "UnblendedCost":
            raise ValueError("Phase 2 reconciliation currently supports UnblendedCost")
        safe_start = date.fromisoformat(start).isoformat()
        safe_end = date.fromisoformat(end).isoformat()
        sql = f"""
        SELECT linked_account,
               SUM(CAST(line_item_unblended_cost AS DOUBLE)) AS amount
        FROM {self.settings.athena_view}
        WHERE DATE(line_item_usage_start_date) >= DATE '{safe_start}'
          AND DATE(line_item_usage_start_date) < DATE '{safe_end}'
        GROUP BY linked_account
        ORDER BY linked_account
        """
        cur_rows = self.cached("cur_reconciliation", {"start": start, "end": end}, 86400, lambda: self._athena_query(sql))
        ce = self.cost_summary(start, end, metric)
        ce_map = {item["account_alias"]: item["amount"] for item in ce["accounts"]}
        data: list[dict[str, Any]] = []
        for row in cur_rows:
            alias = row.get("linked_account") or "Unknown"
            cur_amount = float(row.get("amount") or 0)
            ce_amount = float(ce_map.get(alias, 0))
            difference = cur_amount - ce_amount
            percent = None if ce_amount == 0 else difference / ce_amount * 100
            data.append({"account_alias": alias, "ce_amount": round(ce_amount, 6), "cur_amount": round(cur_amount, 6), "difference": round(difference, 6), "difference_percent": round(percent, 4) if percent is not None else None})
        return {"status": "OK", "source": ["COST_EXPLORER", "CUR_ATHENA"], "time_range": {"start": start, "end": end}, "metric": metric, "currency": "CNY", "data": data}

    def cur_resource_cost_map(self, days: int = 30) -> dict[str, dict[str, float]]:
        safe_end = date.today() + timedelta(days=1)
        safe_start = safe_end - timedelta(days=days)
        sql = f"""
        SELECT linked_account,
               line_item_resource_id,
               SUM(CAST(line_item_unblended_cost AS DOUBLE)) AS amount
        FROM {self.settings.athena_view}
        WHERE DATE(line_item_usage_start_date) >= DATE '{safe_start.isoformat()}'
          AND DATE(line_item_usage_start_date) < DATE '{safe_end.isoformat()}'
          AND COALESCE(line_item_resource_id, '') <> ''
        GROUP BY linked_account, line_item_resource_id
        """
        rows = self.cached(
            "cur_resource_cost_map",
            {"start": safe_start.isoformat(), "end": safe_end.isoformat()},
            86400,
            lambda: self._athena_query(sql, 180),
        )
        result: dict[str, dict[str, float]] = {}
        for row in rows:
            alias = row.get("linked_account") or ""
            resource_id = row.get("line_item_resource_id") or ""
            if not alias or not resource_id:
                continue
            result.setdefault(alias, {})[resource_id] = float(row.get("amount") or 0)
        return result

    @staticmethod
    def _lookup_resource_cost(cost_map: dict[str, dict[str, float]], alias: str, resource_id: str) -> float | None:
        account_costs = cost_map.get(alias, {})
        if resource_id in account_costs:
            return account_costs[resource_id]
        suffixes = (f":{resource_id}", f"/{resource_id}")
        matches = [amount for key, amount in account_costs.items() if key.endswith(suffixes)]
        return sum(matches) if matches else None

    def _metric_stats(
        self,
        account: AccountSpec,
        region: str,
        namespace: str,
        dimension_name: str,
        dimension_value: str,
        metrics: dict[str, str],
        days: int,
    ) -> dict[str, Any]:
        client = self.broker.client(account, "cloudwatch", region)
        end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=days)
        queries = []
        id_to_name: dict[str, str] = {}
        for index, (metric_name, stat) in enumerate(metrics.items()):
            query_id = f"m{index}"
            id_to_name[query_id] = metric_name
            queries.append({
                "Id": query_id,
                "MetricStat": {
                    "Metric": {"Namespace": namespace, "MetricName": metric_name, "Dimensions": [{"Name": dimension_name, "Value": dimension_value}]},
                    "Period": 3600,
                    "Stat": stat,
                },
                "ReturnData": True,
            })
        response = client.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end, ScanBy="TimestampAscending")
        results = list(response.get("MetricDataResults", []))
        while response.get("NextToken"):
            response = client.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end, ScanBy="TimestampAscending", NextToken=response["NextToken"])
            results.extend(response.get("MetricDataResults", []))
        output: dict[str, Any] = {}
        expected = days * 24
        for result in results:
            values = sorted(float(value) for value in result.get("Values", []))
            name = id_to_name.get(result.get("Id", ""), result.get("Label", "metric"))
            if not values:
                output[name] = {"samples": 0, "coverage_percent": 0.0}
                continue
            p95_index = min(len(values) - 1, max(0, math.ceil(len(values) * 0.95) - 1))
            output[name] = {
                "samples": len(values),
                "coverage_percent": round(min(100.0, len(values) / expected * 100), 2),
                "min": round(values[0], 4),
                "p50": round(statistics.median(values), 4),
                "p95": round(values[p95_index], 4),
                "max": round(values[-1], 4),
                "average": round(statistics.fmean(values), 4),
            }
        return output

    @staticmethod
    def _coverage(metrics: dict[str, Any]) -> float:
        values = [float(item.get("coverage_percent", 0)) for item in metrics.values() if isinstance(item, dict)]
        return min(values) if values else 0.0

    def idle_recommendations(self, days: int = 14) -> dict[str, Any]:
        days = 30 if days >= 30 else 14

        def load() -> dict[str, Any]:
            recommendations: list[dict[str, Any]] = []
            errors: list[dict[str, str]] = []
            try:
                cost_map = self.cur_resource_cost_map(30)
            except (ClientError, BotoCoreError, RuntimeError):
                cost_map = {}
            for account in self.broker.accounts:
                for region in self.settings.region_list:
                    started = int(time.time())
                    try:
                        ec2 = self.broker.client(account, "ec2", region)
                        reservations = ec2.describe_instances(Filters=[{"Name": "instance-state-name", "Values": ["running"]}]).get("Reservations", [])
                        for reservation in reservations:
                            for instance in reservation.get("Instances", []):
                                instance_id = instance["InstanceId"]
                                metrics = self._metric_stats(account, region, "AWS/EC2", "InstanceId", instance_id, {"CPUUtilization": "Average", "NetworkIn": "Sum", "NetworkOut": "Sum", "EBSReadOps": "Sum", "EBSWriteOps": "Sum"}, days)
                                coverage = self._coverage(metrics)
                                cpu = metrics.get("CPUUtilization", {}).get("p95", 100)
                                network = metrics.get("NetworkIn", {}).get("average", float("inf")) + metrics.get("NetworkOut", {}).get("average", float("inf"))
                                ebs_ops = metrics.get("EBSReadOps", {}).get("average", float("inf")) + metrics.get("EBSWriteOps", {}).get("average", float("inf"))
                                if coverage >= 80 and cpu < 5 and network < 5 * 1024 * 1024 and ebs_ops < 100:
                                    recommendations.append(self._rule_recommendation(account.alias, region, "EC2_INSTANCE", instance_id, "IDLE_CANDIDATE", "Review stop or schedule", days, coverage, metrics, "HIGH", self._lookup_resource_cost(cost_map, account.alias, instance_id)))
                        for volume in ec2.describe_volumes().get("Volumes", []):
                            if volume.get("State") == "available" and not volume.get("Attachments"):
                                age = (datetime.now(timezone.utc) - volume["CreateTime"]).days
                                if age >= 7:
                                    recommendations.append(self._rule_recommendation(account.alias, region, "EBS_VOLUME", volume["VolumeId"], "UNATTACHED", "Review snapshot and delete", age, 100, {"state": "available", "size_gib": volume.get("Size"), "age_days": age}, "MEDIUM", self._lookup_resource_cost(cost_map, account.alias, volume["VolumeId"])))
                        for address in ec2.describe_addresses().get("Addresses", []):
                            if not address.get("AssociationId"):
                                resource_id = address.get("AllocationId") or address.get("PublicIp", "unassociated-eip")
                                recommendations.append(self._rule_recommendation(account.alias, region, "ELASTIC_IP", resource_id, "UNASSOCIATED", "Review release", days, 100, {"association": None}, "LOW", self._lookup_resource_cost(cost_map, account.alias, resource_id)))
                        for gateway in ec2.describe_nat_gateways(Filter=[{"Name": "state", "Values": ["available"]}]).get("NatGateways", []):
                            nat_id = gateway["NatGatewayId"]
                            metrics = self._metric_stats(account, region, "AWS/NATGateway", "NatGatewayId", nat_id, {"BytesInFromSource": "Sum", "BytesOutToDestination": "Sum", "ActiveConnectionCount": "Maximum"}, days)
                            coverage = self._coverage(metrics)
                            traffic = metrics.get("BytesInFromSource", {}).get("average", float("inf")) + metrics.get("BytesOutToDestination", {}).get("average", float("inf"))
                            connections = metrics.get("ActiveConnectionCount", {}).get("max", float("inf"))
                            routes = ec2.describe_route_tables(Filters=[{"Name": "route.nat-gateway-id", "Values": [nat_id]}]).get("RouteTables", [])
                            if coverage >= 80 and traffic < 1024 * 1024 and connections == 0:
                                recommendations.append(self._rule_recommendation(account.alias, region, "NAT_GATEWAY", nat_id, "IDLE_CANDIDATE", "Review routes before delete", days, coverage, {**metrics, "route_table_references": len(routes)}, "HIGH", self._lookup_resource_cost(cost_map, account.alias, nat_id)))
                        recommendations.extend(self._idle_load_balancers(account, region, days, cost_map))
                        self.store.add_run("idle_rules", account.alias, region, "OK", f"{len(recommendations)} cumulative candidates", started)
                    except (ClientError, BotoCoreError) as exc:
                        detail = self._safe_error(exc)
                        errors.append({"account_alias": account.alias, "region": region, "error": detail})
                        self.store.add_run("idle_rules", account.alias, region, "ERROR", detail, started)
            return {"status": "OK" if not errors else ("PARTIAL" if recommendations else "ERROR"), "partial": bool(errors and recommendations), "source": ["RESOURCE_INVENTORY_API", "CLOUDWATCH", "CUR_ATHENA"], "rule_version": "idle-v1", "lookback_period_days": days, "data": recommendations, "errors": errors}

        return self.cached("idle_recommendations_v2", {"days": days}, 86400, load)

    def _idle_load_balancers(self, account: AccountSpec, region: str, days: int, cost_map: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
        client = self.broker.client(account, "elbv2", region)
        paginator = client.get_paginator("describe_load_balancers")
        recommendations: list[dict[str, Any]] = []
        for page in paginator.paginate():
            for lb in page.get("LoadBalancers", []):
                arn = lb["LoadBalancerArn"]
                dimension = arn.split("loadbalancer/", 1)[-1]
                namespace = "AWS/ApplicationELB" if lb.get("Type") == "application" else "AWS/NetworkELB"
                metric_names = {"ProcessedBytes": "Sum"}
                if lb.get("Type") == "application":
                    metric_names["RequestCount"] = "Sum"
                else:
                    metric_names["ActiveFlowCount"] = "Maximum"
                metrics = self._metric_stats(account, region, namespace, "LoadBalancer", dimension, metric_names, days)
                coverage = self._coverage(metrics)
                activity = sum(float(value.get("average", 0)) for value in metrics.values())
                target_groups = client.describe_target_groups(LoadBalancerArn=arn).get("TargetGroups", [])
                if (coverage >= 80 and activity == 0) or not target_groups:
                    recommendations.append(self._rule_recommendation(account.alias, region, "LOAD_BALANCER", arn, "IDLE_CANDIDATE", "Review listeners and targets before delete", days, coverage, {**metrics, "target_group_count": len(target_groups)}, "HIGH", self._lookup_resource_cost(cost_map, account.alias, arn)))
        return recommendations

    def _rule_recommendation(self, alias: str, region: str, resource_type: str, resource_id: str, finding: str, action: str, days: int, coverage: float, evidence: dict[str, Any], risk: str, actual_monthly_cost: float | None = None) -> dict[str, Any]:
        resource_key = self.security.pseudonym(resource_id, resource_type.lower())
        full_savings_findings = {"IDLE_CANDIDATE", "UNATTACHED", "UNASSOCIATED"}
        potential_savings = actual_monthly_cost if finding in full_savings_findings and actual_monthly_cost is not None else None
        return {
            "recommendation_id": self.security.pseudonym(f"{alias}:{region}:{resource_type}:{resource_id}:{finding}", "rec"),
            "account_alias": alias,
            "region": region,
            "resource_type": resource_type,
            "resource_key": resource_key,
            "finding": finding,
            "recommended_action": action,
            "status": "CANDIDATE",
            "risk": risk,
            "actual_monthly_cost": round(actual_monthly_cost, 6) if actual_monthly_cost is not None else None,
            "potential_monthly_savings": round(potential_savings, 6) if potential_savings is not None else None,
            "currency": "CNY",
            "cost_source": "CUR_ATHENA" if actual_monthly_cost is not None else "NOT_AVAILABLE",
            "savings_assumption": "Maximum removable run-rate" if potential_savings is not None else "Requires target configuration pricing",
            "lookback_period_days": days,
            "metric_coverage_percent": round(coverage, 2),
            "rule_id": f"{resource_type.lower()}-{finding.lower()}-v1",
            "thresholds": {"minimum_metric_coverage_percent": 80},
            "evidence": self.security.sanitize(evidence),
            "sources": ["RESOURCE_INVENTORY_API", "CLOUDWATCH", "CUR_ATHENA"],
        }

    def database_recommendations(self, engine_scope: str, days: int = 14) -> dict[str, Any]:
        if engine_scope not in {"rds", "aurora"}:
            raise ValueError("engine_scope must be rds or aurora")
        days = 30 if days >= 30 else 14

        def load() -> dict[str, Any]:
            recommendations: list[dict[str, Any]] = []
            inventory_count = 0
            errors: list[dict[str, str]] = []
            try:
                cost_map = self.cur_resource_cost_map(30)
            except (ClientError, BotoCoreError, RuntimeError):
                cost_map = {}
            for account in self.broker.accounts:
                for region in self.settings.region_list:
                    started = int(time.time())
                    try:
                        rds = self.broker.client(account, "rds", region)
                        instances: list[dict[str, Any]] = []
                        marker: str | None = None
                        while True:
                            params = {"Marker": marker} if marker else {}
                            response = rds.describe_db_instances(**params)
                            instances.extend(response.get("DBInstances", []))
                            marker = response.get("Marker")
                            if not marker:
                                break
                        selected = [item for item in instances if (item.get("Engine", "").startswith("aurora")) == (engine_scope == "aurora")]
                        inventory_count += len(selected)
                        cluster_members: dict[str, list[dict[str, Any]]] = {}
                        for instance in selected:
                            identifier = instance["DBInstanceIdentifier"]
                            metrics = self._metric_stats(account, region, "AWS/RDS", "DBInstanceIdentifier", identifier, {"CPUUtilization": "Average", "DatabaseConnections": "Average", "FreeableMemory": "Minimum", "ReadIOPS": "Average", "WriteIOPS": "Average", "ReadThroughput": "Average", "WriteThroughput": "Average", "ReadLatency": "Average", "WriteLatency": "Average", "DiskQueueDepth": "Average"}, days)
                            coverage = self._coverage(metrics)
                            evidence = {"engine": instance.get("Engine"), "instance_class": instance.get("DBInstanceClass"), "multi_az": instance.get("MultiAZ"), "storage_type": instance.get("StorageType"), "allocated_storage_gib": instance.get("AllocatedStorage"), "provisioned_iops": instance.get("Iops"), "metrics": metrics}
                            if engine_scope == "aurora" and instance.get("DBClusterIdentifier"):
                                cluster_members.setdefault(instance["DBClusterIdentifier"], []).append({"instance": instance, "metrics": metrics, "coverage": coverage})
                            if coverage < 80:
                                recommendations.append(self._rule_recommendation(account.alias, region, "RDS_DB_INSTANCE" if engine_scope == "rds" else "AURORA_DB_INSTANCE", identifier, "INSUFFICIENT_DATA", "Collect more CloudWatch metrics", days, coverage, evidence, "NONE", self._lookup_resource_cost(cost_map, account.alias, identifier)))
                                continue
                            cpu = metrics.get("CPUUtilization", {}).get("p95", 100)
                            connections = metrics.get("DatabaseConnections", {}).get("p95", float("inf"))
                            io = metrics.get("ReadIOPS", {}).get("p95", float("inf")) + metrics.get("WriteIOPS", {}).get("p95", float("inf"))
                            throughput = metrics.get("ReadThroughput", {}).get("p95", float("inf")) + metrics.get("WriteThroughput", {}).get("p95", float("inf"))
                            if engine_scope == "rds" and cpu < 5 and connections < 1 and io < 5 and throughput < 1024 * 1024:
                                recommendations.append(self._rule_recommendation(account.alias, region, "RDS_DB_INSTANCE", identifier, "IDLE_CANDIDATE", "Review stop, snapshot or retirement", days, coverage, evidence, "HIGH", self._lookup_resource_cost(cost_map, account.alias, identifier)))
                            elif engine_scope == "rds" and cpu < 20 and connections < 50 and io < 200:
                                recommendations.append(self._rule_recommendation(account.alias, region, "RDS_DB_INSTANCE", identifier, "DOWNSIZE_CANDIDATE", "Evaluate one smaller DB instance class", days, coverage, evidence, "HIGH", self._lookup_resource_cost(cost_map, account.alias, identifier)))
                            if engine_scope == "rds" and instance.get("Iops", 0) and io < max(50, instance.get("Iops", 0) * 0.1):
                                recommendations.append(self._rule_recommendation(account.alias, region, "RDS_STORAGE", identifier, "OVERPROVISIONED_IOPS_CANDIDATE", "Review provisioned IOPS and throughput", days, coverage, evidence, "HIGH", self._lookup_resource_cost(cost_map, account.alias, identifier)))
                        if engine_scope == "aurora":
                            recommendations.extend(self._aurora_cluster_rules(account, region, rds, cluster_members, days))
                        self.store.add_run(f"{engine_scope}_rules", account.alias, region, "OK", f"{len(selected)} resources", started)
                    except (ClientError, BotoCoreError) as exc:
                        detail = self._safe_error(exc)
                        errors.append({"account_alias": account.alias, "region": region, "error": detail})
                        self.store.add_run(f"{engine_scope}_rules", account.alias, region, "ERROR", detail, started)
            return {"status": "OK" if not errors else ("PARTIAL" if inventory_count else "ERROR"), "partial": bool(errors and inventory_count), "source": ["RDS_INVENTORY_API", "CLOUDWATCH", "CUR_ATHENA", "AWS_CHINA_PRICE_LIST"], "rule_version": f"{engine_scope}-v1", "lookback_period_days": days, "inventory_count": inventory_count, "data": recommendations, "errors": errors}

        return self.cached("database_recommendations_v2", {"scope": engine_scope, "days": days}, 86400, load)

    def recommendation_evidence(self, recommendation_id: str) -> dict[str, Any]:
        if not recommendation_id.startswith("rec-"):
            raise ValueError("Invalid recommendation ID")
        for result in (
            self.idle_recommendations(14),
            self.database_recommendations("rds", 14),
            self.database_recommendations("aurora", 14),
        ):
            for recommendation in result.get("data", []):
                if recommendation.get("recommendation_id") == recommendation_id:
                    return {
                        "status": "OK",
                        "recommendation_id": recommendation_id,
                        "source": recommendation.get("sources", []),
                        "rule_id": recommendation.get("rule_id"),
                        "thresholds": recommendation.get("thresholds"),
                        "metric_coverage_percent": recommendation.get("metric_coverage_percent"),
                        "actual_monthly_cost": recommendation.get("actual_monthly_cost"),
                        "potential_monthly_savings": recommendation.get("potential_monthly_savings"),
                        "currency": recommendation.get("currency"),
                        "evidence": recommendation.get("evidence"),
                    }
        raise ValueError("Recommendation not found")

    def _aurora_cluster_rules(self, account: AccountSpec, region: str, rds, clusters: dict[str, list[dict[str, Any]]], days: int) -> list[dict[str, Any]]:
        recommendations: list[dict[str, Any]] = []
        cluster_details = {item["DBClusterIdentifier"]: item for item in rds.describe_db_clusters().get("DBClusters", [])}
        for cluster_id, members in clusters.items():
            coverage = min((member["coverage"] for member in members), default=0)
            all_idle = coverage >= 80 and all(
                member["metrics"].get("CPUUtilization", {}).get("p95", 100) < 5
                and member["metrics"].get("DatabaseConnections", {}).get("p95", float("inf")) < 1
                and member["metrics"].get("ReadIOPS", {}).get("p95", float("inf")) + member["metrics"].get("WriteIOPS", {}).get("p95", float("inf")) < 5
                for member in members
            )
            evidence = {"member_count": len(members), "all_members_evaluated": True, "cluster_engine_mode": cluster_details.get(cluster_id, {}).get("EngineMode"), "members": [{"role": "writer" if member["instance"].get("PromotionTier") == 0 else "reader", "metrics": member["metrics"]} for member in members]}
            if all_idle:
                recommendations.append(self._rule_recommendation(account.alias, region, "AURORA_CLUSTER", cluster_id, "IDLE_CANDIDATE", "Review cluster stop, snapshot or retirement", days, coverage, evidence, "HIGH"))
            for member in members:
                instance = member["instance"]
                if instance.get("PromotionTier", 0) > 0 and member["coverage"] >= 80 and member["metrics"].get("DatabaseConnections", {}).get("p95", float("inf")) < 1 and member["metrics"].get("ReadIOPS", {}).get("p95", float("inf")) < 5:
                    recommendations.append(self._rule_recommendation(account.alias, region, "AURORA_READER", instance["DBInstanceIdentifier"], "LOW_READER_ACTIVITY", "Review reader count and failover requirements", days, member["coverage"], {"cluster": self.security.pseudonym(cluster_id, "cluster"), "metrics": member["metrics"]}, "HIGH"))
        return recommendations

    def quicksight_embed_url(self) -> dict[str, Any]:
        if not self.settings.quicksight_user_arn:
            raise ValueError("FINOPS_QUICKSIGHT_USER_ARN is not configured")
        account = self.broker.accounts[0]
        client = self.broker.client(account, "quicksight", self.settings.quicksight_region)
        aws_account_id = self.broker.client(account, "sts").get_caller_identity()["Account"]
        response = client.generate_embed_url_for_registered_user(
            AwsAccountId=aws_account_id,
            UserArn=self.settings.quicksight_user_arn,
            SessionLifetimeInMinutes=600,
            AllowedDomains=self.settings.allowed_domains,
            ExperienceConfiguration={"Dashboard": {"InitialDashboardId": self.settings.quicksight_dashboard_id}},
        )
        return {"status": "OK", "dashboard_id": self.settings.quicksight_dashboard_id, "embed_url": response["EmbedUrl"], "expires_in_minutes": 600}
