"""Data link service implementation using Google Ads SDK."""

from typing import Any, Dict, List, Optional, Callable, Awaitable

from fastmcp import Context, FastMCP
from google.ads.googleads.v24.services.services.data_link_service import (
    DataLinkServiceClient,
)

# Note: Google Ads v24 Data Links manage integrations like Webapp, Advertiser, or Third-Party Analytics. Full link setup varies by type (e.g., youtube_video_link vs. third_party_app_analytics_link).
from google.ads.googleads.errors import GoogleAdsException

from src.sdk_client import get_sdk_client
from src.utils import format_ads_error, format_customer_id, get_logger

logger = get_logger(__name__)


class DataLinkService:
    """Data link service for managing third-party data connections."""

    def __init__(self) -> None:
        """Initialize the data link service."""
        self._client: Optional[DataLinkServiceClient] = None

    @property
    def client(self) -> DataLinkServiceClient:
        """Get the data link service client."""
        if self._client is None:
            sdk_client = get_sdk_client()
            self._client = sdk_client.client.get_service(
                "DataLinkService", version="v24"
            )
        assert self._client is not None
        return self._client

    async def create_basic_data_link(
        self,
        ctx: Context,
        customer_id: str,
        data_link_name: str,
        data_link_type: str,
        external_id: str,
    ) -> Dict[str, Any]:
        """Create a basic data link (simplified implementation).

        Args:
            ctx: FastMCP context
            customer_id: The customer ID
            data_link_name: Name for the data link
            data_link_type: Type of data link (WEBAPP, ADVERTISER, etc.)
            external_id: External identifier (URL for webapp, ID for advertiser)

        Returns:
            Created data link details
        """
        try:
            customer_id = format_customer_id(customer_id)

            # Note: Full v24/v24.1 DataLink configurations require setting type-specific link details (e.g. third_party_app_analytics_link, or youtube_video containing the new v24.1 channel_id) based on the integration target.
            await ctx.log(
                level="info",
                message=f"Data link creation requested: {data_link_name} ({data_link_type}) for {external_id}",
            )

            return {
                "customer_id": customer_id,
                "data_link_name": data_link_name,
                "type": data_link_type,
                "external_id": external_id,
                "status": "Request processed - full data link creation requires additional v24/v24.1 type support",
                "note": "This is a simplified implementation. Initializing specific third-party integrations (e.g., Third-Party App Analytics, YouTube) requires setting distinct sub-attributes in the DataLink protobuf resource (such as the new v24.1 channel_id within the youtube_video field exposing the associated YouTube channel ID).",
            }

        except GoogleAdsException as e:
            error_msg = format_ads_error(e)
            await ctx.log(level="error", message=error_msg)
            raise Exception(error_msg) from e
        except Exception as e:
            error_msg = f"Failed to create data link: {str(e)}"
            await ctx.log(level="error", message=error_msg)
            raise Exception(error_msg) from e

    async def list_data_links(
        self,
        ctx: Context,
        customer_id: str,
    ) -> List[Dict[str, Any]]:
        """List data links for a customer (simplified implementation).

        Args:
            ctx: FastMCP context
            customer_id: The customer ID

        Returns:
            List of data links (simplified implementation)
        """
        try:
            customer_id = format_customer_id(customer_id)

            # Note: Listing/querying complex data links is supported via GoogleAdsService search queries using v24 GAQL for data_link fields.
            await ctx.log(
                level="info",
                message=f"Data link list requested for customer {customer_id}",
            )

            return [
                {
                    "customer_id": customer_id,
                    "status": "Request processed - data link listing requires additional v24 type support",
                    "note": "This is a simplified listing. Custom searches on the data_link resource should be executed using the search/query tools to retrieve full integration status.",
                }
            ]

        except Exception as e:
            error_msg = f"Failed to list data links: {str(e)}"
            await ctx.log(level="error", message=error_msg)
            raise Exception(error_msg) from e


def create_data_link_tools(
    service: DataLinkService,
) -> List[Callable[..., Awaitable[Any]]]:
    """Create tool functions for the data link service.

    This returns a list of tool functions that can be registered with FastMCP.
    This approach makes the tools testable by allowing service injection.
    """
    tools = []

    async def create_basic_data_link(
        ctx: Context,
        customer_id: str,
        data_link_name: str,
        data_link_type: str,
        external_id: str,
    ) -> Dict[str, Any]:
        """Create a basic data link (simplified implementation).

        Args:
            customer_id: The customer ID
            data_link_name: Name for the data link
            data_link_type: Type of data link (WEBAPP, ADVERTISER, etc.)
            external_id: External identifier (URL for webapp, ID for advertiser)

        Returns:
            Created data link details (simplified implementation)
        """
        return await service.create_basic_data_link(
            ctx=ctx,
            customer_id=customer_id,
            data_link_name=data_link_name,
            data_link_type=data_link_type,
            external_id=external_id,
        )

    async def list_data_links(
        ctx: Context,
        customer_id: str,
    ) -> List[Dict[str, Any]]:
        """List data links for a customer (simplified implementation).

        Args:
            customer_id: The customer ID

        Returns:
            List of data links (simplified implementation)
        """
        return await service.list_data_links(
            ctx=ctx,
            customer_id=customer_id,
        )

    tools.extend(
        [
            create_basic_data_link,
            list_data_links,
        ]
    )
    return tools


def register_data_link_tools(mcp: FastMCP[Any]) -> DataLinkService:
    """Register data link tools with the MCP server.

    Returns the DataLinkService instance for testing purposes.
    """
    service = DataLinkService()
    tools = create_data_link_tools(service)

    # Register each tool
    for tool in tools:
        mcp.tool(tool)

    return service
