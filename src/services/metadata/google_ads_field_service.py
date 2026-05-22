"""Google Ads field service implementation using Google Ads SDK."""

from typing import Any, Dict, List, Optional, Callable, Awaitable

from fastmcp import Context, FastMCP
from google.ads.googleads.v24.services.services.google_ads_field_service import (
    GoogleAdsFieldServiceClient,
)
from google.ads.googleads.v24.services.types.google_ads_field_service import (
    GetGoogleAdsFieldRequest,
    SearchGoogleAdsFieldsRequest,
)
from google.ads.googleads.v24.resources.types.google_ads_field import GoogleAdsField
from google.ads.googleads.v24.enums.types.google_ads_field_category import (
    GoogleAdsFieldCategoryEnum,
)
from google.ads.googleads.errors import GoogleAdsException

from src.sdk_client import get_sdk_client
from src.utils import (
    resolve_enum,
    format_ads_error,
    get_logger,
    serialize_proto_message,
)
from src.services.metadata.gaql_validation import (
    validate_gaql_query_with_metadata,
    auto_correct_gaql_query_logic,
)

logger = get_logger(__name__)


class GoogleAdsFieldService:
    """Google Ads field service for discovering field metadata."""

    def __init__(self) -> None:
        """Initialize the Google Ads field service."""
        self._client: Optional[GoogleAdsFieldServiceClient] = None

    @property
    def client(self) -> GoogleAdsFieldServiceClient:
        """Get the Google Ads field service client."""
        if self._client is None:
            sdk_client = get_sdk_client()
            self._client = sdk_client.client.get_service(
                "GoogleAdsFieldService", version="v24"
            )
        assert self._client is not None
        return self._client

    async def get_field_metadata(
        self,
        ctx: Context,
        field_name: str,
    ) -> Dict[str, Any]:
        """Get metadata for a specific field.

        Args:
            ctx: FastMCP context
            field_name: The field name (e.g., "campaign.name")

        Returns:
            Field metadata including type, category, and attributes
        """
        try:
            # Create request
            request = GetGoogleAdsFieldRequest()
            request.resource_name = f"googleAdsFields/{field_name}"

            # Make the API call
            field: GoogleAdsField = self.client.get_google_ads_field(request=request)

            await ctx.log(
                level="info",
                message=f"Retrieved metadata for field: {field_name}",
            )

            return serialize_proto_message(field)

        except GoogleAdsException as e:
            error_msg = format_ads_error(e)
            await ctx.log(level="error", message=error_msg)
            raise Exception(error_msg) from e
        except Exception as e:
            error_msg = f"Failed to get field metadata: {str(e)}"
            await ctx.log(level="error", message=error_msg)
            raise Exception(error_msg) from e

    async def search_fields(
        self,
        ctx: Context,
        query: Optional[str] = None,
        category_filter: Optional[
            GoogleAdsFieldCategoryEnum.GoogleAdsFieldCategory
        ] = None,
        selectable_only: bool = False,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Search for Google Ads fields based on criteria.

        Args:
            ctx: FastMCP context
            query: Optional search query (e.g., "name LIKE '%campaign%'")
            category_filter: Optional category filter (RESOURCE, ATTRIBUTE, SEGMENT, METRIC)
            selectable_only: Only return selectable fields
            limit: Maximum number of results

        Returns:
            List of field metadata
        """
        try:
            # Build query
            conditions = []

            if query:
                conditions.append(query)

            if category_filter:
                conditions.append(f"category = '{category_filter.name}'")

            if selectable_only:
                conditions.append("selectable = true")

            # Construct final query
            search_query = " AND ".join(conditions) if conditions else "name != ''"
            search_query += f" LIMIT {limit}"

            # Create request
            request = SearchGoogleAdsFieldsRequest()
            request.query = search_query

            # Make the API call - returns a pager
            pager = self.client.search_google_ads_fields(request=request)

            # Process results
            fields = []
            for field in pager:
                fields.append(serialize_proto_message(field))

            await ctx.log(
                level="info",
                message=f"Found {len(fields)} fields matching criteria",
            )

            return fields

        except GoogleAdsException as e:
            error_msg = format_ads_error(e)
            await ctx.log(level="error", message=error_msg)
            raise Exception(error_msg) from e
        except Exception as e:
            error_msg = f"Failed to search fields: {str(e)}"
            await ctx.log(level="error", message=error_msg)
            raise Exception(error_msg) from e

    async def get_resource_fields(
        self,
        ctx: Context,
        resource_name: str,
        include_metrics: bool = True,
        include_segments: bool = True,
    ) -> Dict[str, Any]:
        """Get all available fields for a specific resource.

        Args:
            ctx: FastMCP context
            resource_name: The resource name (e.g., "campaign", "ad_group")
            include_metrics: Include metric fields
            include_segments: Include segment fields

        Returns:
            Dictionary with categorized fields for the resource
        """
        try:
            # Get resource fields
            resource_query = f"name LIKE '{resource_name}.%' AND category = 'ATTRIBUTE'"
            resource_fields = await self.search_fields(
                ctx=ctx, query=resource_query, limit=500
            )

            result = {
                "resource": resource_name,
                "attributes": resource_fields,
            }

            # Get metrics if requested
            if include_metrics:
                metric_query = "name LIKE 'metrics.%' AND category = 'METRIC'"
                metric_fields = await self.search_fields(
                    ctx=ctx, query=metric_query, limit=200
                )
                result["metrics"] = metric_fields

            # Get segments if requested
            if include_segments:
                segment_query = "name LIKE 'segments.%' AND category = 'SEGMENT'"
                segment_fields = await self.search_fields(
                    ctx=ctx, query=segment_query, limit=100
                )
                result["segments"] = segment_fields

            await ctx.log(
                level="info",
                message=f"Retrieved field information for resource: {resource_name}",
            )

            return result

        except Exception as e:
            error_msg = f"Failed to get resource fields: {str(e)}"
            await ctx.log(level="error", message=error_msg)
            raise Exception(error_msg) from e

    async def validate_query_fields(
        self,
        ctx: Context,
        resource_name: str,
        field_names: List[str],
    ) -> Dict[str, Any]:
        """Validate if fields can be selected together in a query.

        Args:
            ctx: FastMCP context
            resource_name: The main resource (e.g., "campaign")
            field_names: List of field names to validate

        Returns:
            Validation result with compatibility information
        """
        try:
            validation_result: Dict[str, Any] = {
                "resource": resource_name,
                "fields": {},
                "all_compatible": True,
                "issues": [],
            }

            # Check each field and fetch metadata
            fields_meta = {}
            for field_name in field_names:
                try:
                    field_metadata = await self.get_field_metadata(ctx, field_name)
                    fields_meta[field_name] = field_metadata
                    validation_result["fields"][field_name] = {
                        "valid": True,
                        "selectable": field_metadata.get("selectable", False),
                        "data_type": field_metadata.get("data_type", "UNKNOWN"),
                        "category": field_metadata.get("category", "UNKNOWN"),
                        "selectable_with": field_metadata.get("selectable_with", []),
                    }

                    if not field_metadata.get("selectable", False):
                        validation_result["all_compatible"] = False
                        validation_result["issues"].append(
                            f"Field '{field_name}' is not selectable"
                        )

                except Exception:
                    validation_result["fields"][field_name] = {
                        "valid": False,
                        "error": f"Field '{field_name}' not found",
                    }
                    validation_result["all_compatible"] = False
                    validation_result["issues"].append(
                        f"Field '{field_name}' is not a valid field"
                    )

            # Perform resource compatibility check
            if validation_result["all_compatible"]:
                core_res = resource_name.replace("googleAdsFields/", "").lower()
                for f_name, meta in fields_meta.items():
                    if f_name.lower().startswith(core_res + "."):
                        continue
                    if "selectable_with" in meta or "attribute_resources" in meta:
                        sel_with = [s.lower() for s in meta.get("selectable_with", [])]
                        attr_res = [r.replace("googleAdsFields/", "").lower() for r in meta.get("attribute_resources", [])]
                        if core_res not in sel_with and core_res not in attr_res:
                            validation_result["all_compatible"] = False
                            validation_result["issues"].append(
                                f"Field '{f_name}' is not compatible with resource '{resource_name}'"
                            )

            await ctx.log(
                level="info",
                message=f"Validated {len(field_names)} fields for resource: {resource_name}",
            )

            return validation_result

        except Exception as e:
            error_msg = f"Failed to validate query fields: {str(e)}"
            await ctx.log(level="error", message=error_msg)
            raise Exception(error_msg) from e

    async def validate_gaql_query(
        self,
        ctx: Context,
        query: str,
    ) -> Dict[str, Any]:
        """Validate a GAQL query against v24 rules and schema field attributes.

        Args:
            ctx: FastMCP context
            query: The GAQL query to validate

        Returns:
            Dictionary with validation status and list of issues/warnings.
        """
        try:
            async def metadata_provider(field_name: str) -> Dict[str, Any]:
                try:
                    return await self.get_field_metadata(ctx, field_name)
                except Exception:
                    return {}

            result = await validate_gaql_query_with_metadata(query, metadata_provider)
            await ctx.log(
                level="info",
                message=f"GAQL query validation completed. Valid: {result['valid']}",
            )
            return result
        except Exception as e:
            error_msg = f"Failed to validate GAQL query: {str(e)}"
            await ctx.log(level="error", message=error_msg)
            raise Exception(error_msg) from e

    async def auto_correct_gaql_query(
        self,
        ctx: Context,
        query: str,
    ) -> Dict[str, Any]:
        """Automatically correct a GAQL query to make it v24-compliant.

        Args:
            ctx: FastMCP context
            query: The GAQL query to correct

        Returns:
            Dictionary containing the corrected query and list of changes made.
        """
        try:
            result = auto_correct_gaql_query_logic(query)
            await ctx.log(
                level="info",
                message=f"GAQL query auto-corrected. Changes made: {len(result['changes_made'])}",
            )
            return result
        except Exception as e:
            error_msg = f"Failed to auto-correct GAQL query: {str(e)}"
            await ctx.log(level="error", message=error_msg)
            raise Exception(error_msg) from e


def create_google_ads_field_tools(
    service: GoogleAdsFieldService,
) -> List[Callable[..., Awaitable[Any]]]:
    """Create tool functions for the Google Ads field service.

    This returns a list of tool functions that can be registered with FastMCP.
    This approach makes the tools testable by allowing service injection.
    """
    tools = []

    async def get_field_metadata(
        ctx: Context,
        field_name: str,
    ) -> Dict[str, Any]:
        """Get metadata for a specific Google Ads field.

        Args:
            field_name: The field name (e.g., "campaign.name", "metrics.clicks")

        Returns:
            Field metadata including:
            - category: RESOURCE, ATTRIBUTE, SEGMENT, or METRIC
            - data_type: STRING, INT64, DOUBLE, BOOLEAN, etc.
            - selectable: Whether the field can be selected in queries
            - filterable: Whether the field can be used in WHERE clauses
            - sortable: Whether the field can be used in ORDER BY
        """
        return await service.get_field_metadata(
            ctx=ctx,
            field_name=field_name,
        )

    async def search_fields(
        ctx: Context,
        query: Optional[str] = None,
        category_filter: Optional[str] = None,
        selectable_only: bool = False,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Search for Google Ads fields based on criteria.

        Args:
            query: Optional search query using field query syntax
                Examples:
                - "name LIKE '%campaign%'"
                - "category = 'METRIC' AND selectable = true"
            category_filter: Filter by category - RESOURCE, ATTRIBUTE, SEGMENT, or METRIC
            selectable_only: Only return fields that can be selected in queries
            limit: Maximum number of results

        Returns:
            List of field metadata matching the criteria
        """
        # Convert string category_filter to enum if provided
        category_filter_enum = None
        if category_filter:
            category_filter_enum = resolve_enum(
                GoogleAdsFieldCategoryEnum.GoogleAdsFieldCategory,
                category_filter,
                "category_filter",
            )

        return await service.search_fields(
            ctx=ctx,
            query=query,
            category_filter=category_filter_enum,
            selectable_only=selectable_only,
            limit=limit,
        )

    async def get_resource_fields(
        ctx: Context,
        resource_name: str,
        include_metrics: bool = True,
        include_segments: bool = True,
    ) -> Dict[str, Any]:
        """Get all available fields for a specific resource.

        Args:
            resource_name: The resource name (e.g., "campaign", "ad_group", "keyword_view")
            include_metrics: Include available metrics for this resource
            include_segments: Include available segments for this resource

        Returns:
            Dictionary with categorized fields:
            - attributes: Resource-specific fields
            - metrics: Available metrics (if requested)
            - segments: Available segments (if requested)
        """
        return await service.get_resource_fields(
            ctx=ctx,
            resource_name=resource_name,
            include_metrics=include_metrics,
            include_segments=include_segments,
        )

    async def validate_query_fields(
        ctx: Context,
        resource_name: str,
        field_names: List[str],
    ) -> Dict[str, Any]:
        """Validate if fields can be selected together in a query.

        Args:
            resource_name: The main resource for the query (e.g., "campaign")
            field_names: List of field names to validate

        Returns:
            Validation result with:
            - all_compatible: Whether all fields can be selected together
            - fields: Individual field validation results
            - issues: List of compatibility issues found
        """
        return await service.validate_query_fields(
            ctx=ctx,
            resource_name=resource_name,
            field_names=field_names,
        )

    async def validate_gaql_query(
        ctx: Context,
        query: str,
    ) -> Dict[str, Any]:
        """Validate a GAQL query against Google Ads v24 validation and schema metadata rules.

        Args:
            query: The GAQL query to validate

        Returns:
            A validation report listing any rules violated, deprecated fields,
            and resource metadata issues.
        """
        return await service.validate_gaql_query(ctx=ctx, query=query)

    async def auto_correct_gaql_query(
        ctx: Context,
        query: str,
    ) -> Dict[str, Any]:
        """Automatically clean and correct a GAQL query to make it v24-compliant.

        Args:
            query: The raw GAQL query to correct

        Returns:
            A correction report containing the original query, the new corrected
            query, and list of changes made.
        """
        return await service.auto_correct_gaql_query(ctx=ctx, query=query)

    tools.extend(
        [
            get_field_metadata,
            search_fields,
            get_resource_fields,
            validate_query_fields,
            validate_gaql_query,
            auto_correct_gaql_query,
        ]
    )
    return tools


def register_google_ads_field_tools(mcp: FastMCP[Any]) -> GoogleAdsFieldService:
    """Register Google Ads field tools with the MCP server.

    Returns the GoogleAdsFieldService instance for testing purposes.
    """
    service = GoogleAdsFieldService()
    tools = create_google_ads_field_tools(service)

    # Register each tool
    for tool in tools:
        mcp.tool(tool)

    return service
