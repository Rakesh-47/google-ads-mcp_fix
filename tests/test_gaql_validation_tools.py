"""Tests for GAQL parsing, validation, and auto-correction utilities."""

import pytest
from typing import Dict, Any
from unittest.mock import Mock, patch

from fastmcp import Context

from google.ads.googleads.v24.services.services.google_ads_field_service import (
    GoogleAdsFieldServiceClient,
)
from src.services.metadata.gaql_validation import (
    tokenize,
    parse_gaql_query,
    validate_gaql_rules,
    validate_gaql_query_with_metadata,
    auto_correct_gaql_query_logic,
)
from src.services.metadata.google_ads_field_service import GoogleAdsFieldService

import re


@pytest.fixture
def mock_field_service_client() -> Mock:
    """Create a mock GoogleAdsFieldServiceClient."""
    return Mock(spec=GoogleAdsFieldServiceClient)


@pytest.fixture
def google_ads_field_service(
    mock_sdk_client: Any, mock_field_service_client: Mock
) -> GoogleAdsFieldService:
    """Create a GoogleAdsFieldService instance with mocked dependencies."""
    mock_sdk_client.client.get_service.return_value = mock_field_service_client  # type: ignore

    with patch(
        "src.services.metadata.google_ads_field_service.get_sdk_client",
        return_value=mock_sdk_client,
    ):
        service = GoogleAdsFieldService()
        _ = service.client
        return service


def test_parse_gaql_query_basic() -> None:
    """Test parsing a standard GAQL query."""
    query = "SELECT campaign.id, campaign.name, metrics.clicks FROM campaign WHERE campaign.status = 'ENABLED' ORDER BY campaign.id LIMIT 100"
    parsed = parse_gaql_query(query)

    assert parsed["resource_name"] == "campaign"
    assert "campaign.id" in parsed["select_fields"]
    assert "campaign.name" in parsed["select_fields"]
    assert "metrics.clicks" in parsed["select_fields"]
    assert "campaign.status" in parsed["where_fields"]
    assert "campaign.id" in parsed["orderby_fields"]
    assert parsed["limit"] == "100"
    assert parsed["wrapped_in_parentheses"] is False


def test_parse_gaql_query_wrapped_parentheses() -> None:
    """Test parsing a query wrapped in parentheses (v20-style)."""
    query = "(SELECT campaign.id FROM campaign)"
    parsed = parse_gaql_query(query)

    assert parsed["resource_name"] == "campaign"
    assert "campaign.id" in parsed["select_fields"]
    assert parsed["wrapped_in_parentheses"] is True


def test_validate_gaql_rules_parentheses() -> None:
    """Test that wrapped parentheses generate a critical error."""
    parsed = {
        "wrapped_in_parentheses": True,
        "select_fields": ["campaign.id"],
        "where_fields": [],
        "orderby_fields": [],
        "limit": "100",
    }
    issues = validate_gaql_rules(parsed)
    assert len(issues) == 1
    assert issues[0]["rule"] == "parentheses"
    assert issues[0]["severity"] == "CRITICAL"


def test_validate_gaql_rules_segment_selection() -> None:
    """Test validation of v24 segment rules (must select segments in filters)."""
    # Case 1: Segment in WHERE but missing from SELECT (Invalid)
    parsed = {
        "wrapped_in_parentheses": False,
        "select_fields": ["campaign.id", "metrics.clicks"],
        "where_fields": ["segments.device", "campaign.status"],
        "orderby_fields": [],
        "limit": "100",
    }
    issues = validate_gaql_rules(parsed)
    assert len(issues) == 1
    assert issues[0]["rule"] == "missing_segment_select"
    assert issues[0]["severity"] == "CRITICAL"
    assert "segments.device" in issues[0]["description"]

    # Case 2: Segment in WHERE and included in SELECT (Valid)
    parsed_valid = {
        "wrapped_in_parentheses": False,
        "select_fields": ["campaign.id", "metrics.clicks", "segments.device"],
        "where_fields": ["segments.device"],
        "orderby_fields": [],
        "limit": "100",
    }
    issues_valid = validate_gaql_rules(parsed_valid)
    assert len(issues_valid) == 0

    # Case 3: Core date segment in WHERE but missing from SELECT (Valid exception)
    parsed_date = {
        "wrapped_in_parentheses": False,
        "select_fields": ["campaign.id", "metrics.clicks"],
        "where_fields": ["segments.date"],
        "orderby_fields": [],
        "limit": "100",
    }
    issues_date = validate_gaql_rules(parsed_date)
    assert len(issues_date) == 0


def test_validate_gaql_rules_deprecations() -> None:
    """Test validation of deprecated v20 metrics and attributes."""
    parsed = {
        "wrapped_in_parentheses": False,
        "select_fields": ["campaign.id", "metrics.average_cpv", "campaign.start_date"],
        "where_fields": [],
        "orderby_fields": [],
        "limit": "100",
    }
    issues = validate_gaql_rules(parsed)
    assert len(issues) == 2
    rules = [issue["rule"] for issue in issues]
    assert "deprecated_field" in rules
    assert "metrics.average_cpv" in issues[0]["description"] or "metrics.average_cpv" in issues[1]["description"]


@pytest.mark.asyncio
async def test_validate_gaql_query_with_metadata() -> None:
    """Test GAQL metadata validation using a mock metadata provider."""
    query = "SELECT campaign.id, metrics.clicks FROM campaign WHERE campaign.status = 'ENABLED' LIMIT 100"

    # Define mock metadata response
    async def mock_metadata_provider(field_name: str) -> Dict[str, Any]:
        if field_name == "campaign.id":
            return {"name": "campaign.id", "selectable": True, "filterable": True, "sortable": True}
        if field_name == "metrics.clicks":
            return {"name": "metrics.clicks", "selectable": True, "filterable": False, "sortable": False}
        if field_name == "campaign.status":
            return {"name": "campaign.status", "selectable": True, "filterable": True, "sortable": True}
        return {}

    result = await validate_gaql_query_with_metadata(query, mock_metadata_provider)
    assert result["valid"] is True
    assert len(result["issues"]) == 0

    # Test with unselectable field
    async def mock_metadata_unselectable(field_name: str) -> Dict[str, Any]:
        if field_name == "campaign.id":
            return {"name": "campaign.id", "selectable": False, "filterable": True, "sortable": True}
        return {"name": field_name, "selectable": True, "filterable": True}

    result_unsel = await validate_gaql_query_with_metadata(query, mock_metadata_unselectable)
    assert result_unsel["valid"] is False
    assert len(result_unsel["issues"]) == 1
    assert result_unsel["issues"][0]["rule"] == "not_selectable"


def test_auto_correct_gaql_query_logic() -> None:
    """Test auto-correction logic for GAQL queries."""
    # Input query with parentheses, deprecated fields, and missing segments from SELECT
    query = """
    (SELECT
      campaign.id,
      metrics.clicks,
      metrics.average_cpv,
      campaign.start_date
    FROM campaign
    WHERE segments.device = 'MOBILE' AND segments.date DURING LAST_30_DAYS
    ORDER BY segments.ad_network_type)
    """

    result = auto_correct_gaql_query_logic(query)

    assert result["valid_now"] is True
    corrected = result["corrected_query"]

    # Verify parentheses are stripped
    assert corrected.strip().startswith("SELECT")
    assert corrected.strip().endswith("ad_network_type")

    # Verify deprecated fields replaced
    assert "metrics.trueview_average_cpv" in corrected
    assert "campaign.start_date_time" in corrected
    assert not re.search(r"\bmetrics\.average_cpv\b", corrected)
    assert not re.search(r"\bcampaign\.start_date\b(?!\b_time\b)", corrected)

    # Verify missing segments injected (segments.device and segments.ad_network_type)
    # Note: segments.date is a core date segment, so it should NOT be injected into the SELECT clause
    parsed_corrected = parse_gaql_query(corrected)
    assert any("segments.device" in f.lower() for f in parsed_corrected["select_fields"])
    assert any("segments.ad_network_type" in f.lower() for f in parsed_corrected["select_fields"])
    assert not any("segments.date" in f.lower() for f in parsed_corrected["select_fields"])

    # Verify changes list contains details
    assert any("parentheses" in change for change in result["changes_made"])
    assert any("metrics.average_cpv" in change for change in result["changes_made"])
    assert any("segments.device" in change or "segments.ad_network_type" in change for change in result["changes_made"])


@pytest.mark.asyncio
async def test_field_service_validate_integration(google_ads_field_service: GoogleAdsFieldService, mock_ctx: Context) -> None:
    """Test that GoogleAdsFieldService validate_gaql_query integration executes correctly."""
    query = "SELECT campaign.id FROM campaign LIMIT 100"

    mock_field_metadata = {
        "name": "campaign.id",
        "selectable": True,
        "filterable": True,
        "sortable": True
    }

    with patch.object(
        google_ads_field_service, "get_field_metadata", return_value=mock_field_metadata
    ):
        result = await google_ads_field_service.validate_gaql_query(mock_ctx, query)

        assert result["valid"] is True
        assert len(result["issues"]) == 0
        assert result["parsed_query"]["resource"] == "campaign"


@pytest.mark.asyncio
async def test_field_service_correct_integration(google_ads_field_service: GoogleAdsFieldService, mock_ctx: Context) -> None:
    """Test that GoogleAdsFieldService auto_correct_gaql_query integration executes correctly."""
    query = "(SELECT campaign.start_date FROM campaign)"

    result = await google_ads_field_service.auto_correct_gaql_query(mock_ctx, query)

    assert result["valid_now"] is True
    assert "campaign.start_date_time" in result["corrected_query"]
    assert "Removed outer parentheses surrounding query." in result["changes_made"]


# --- Advanced Lexical, Compatibility, Scoping & Auto-Correction Safety Tests ---

def test_tokenizer_edge_cases() -> None:
    """Test tokenizer correctly parses comments, spacing, and string literals containing keywords."""
    query = """
    -- This is a line comment containing SELECT and FROM
    SELECT
      campaign.id, /* inline comment FROM campaign */
      metrics.clicks
    FROM campaign
    WHERE campaign.name = 'FROM TEST LITERAL' -- comment at the end
      AND campaign.status = 'SELECT'
    """
    
    tokens = tokenize(query)
    
    # Verify string literal tokens
    strings = [t.value for t in tokens if t.type == "STRING"]
    assert "'FROM TEST LITERAL'" in strings
    assert "'SELECT'" in strings
    
    # Verify comments tokens are matched
    line_comments = [t.value for t in tokens if t.type == "LINE_COMMENT"]
    assert "-- This is a line comment containing SELECT and FROM" in line_comments
    
    # Verify robust token-based parser correctly extracts fields and resource ignoring literal keywords
    parsed = parse_gaql_query(query)
    assert parsed["resource_name"] == "campaign"
    assert len(parsed["select_fields"]) == 2
    assert "campaign.id" in parsed["select_fields"]
    assert "metrics.clicks" in parsed["select_fields"]
    assert "campaign.name" in parsed["where_fields"]
    assert "campaign.status" in parsed["where_fields"]


@pytest.mark.asyncio
async def test_resource_scoped_validation() -> None:
    """Test that validating fields against incompatible target resource fails validation."""
    query = "SELECT campaign.id, segments.keyword.info.text FROM campaign"

    async def mock_metadata_provider(field_name: str) -> Dict[str, Any]:
        if field_name == "campaign.id":
            return {
                "name": "campaign.id",
                "selectable": True,
                "selectable_with": ["campaign"],
                "attribute_resources": ["Campaign"]
            }
        if field_name == "segments.keyword.info.text":
            return {
                "name": "segments.keyword.info.text",
                "selectable": True,
                "selectable_with": ["ad_group", "segments"],
                "attribute_resources": ["AdGroup"]
            }
        return {}

    result = await validate_gaql_query_with_metadata(query, mock_metadata_provider)
    assert result["valid"] is False
    
    issues = result["issues"]
    incompatible_res_issues = [i for i in issues if i["rule"] == "incompatible_resource"]
    assert len(incompatible_res_issues) == 1
    assert "segments.keyword.info.text" in incompatible_res_issues[0]["description"]
    assert "campaign" in incompatible_res_issues[0]["description"]


@pytest.mark.asyncio
async def test_cross_field_compatibility_selectable_with() -> None:
    """Test that validating incompatible fields together fails validation."""
    query = "SELECT metrics.conversions, segments.keyword.info.text FROM campaign"

    async def mock_metadata_provider(field_name: str) -> Dict[str, Any]:
        if field_name == "metrics.conversions":
            return {
                "name": "metrics.conversions",
                "selectable": True,
                "selectable_with": ["campaign", "metrics", "segments.date"],
                "attribute_resources": ["Campaign"]
            }
        if field_name == "segments.keyword.info.text":
            return {
                "name": "segments.keyword.info.text",
                "selectable": True,
                "selectable_with": ["campaign", "ad_group", "segments"],
                "attribute_resources": ["Campaign", "AdGroup"]
            }
        return {}

    result = await validate_gaql_query_with_metadata(query, mock_metadata_provider)
    assert result["valid"] is False
    
    issues = result["issues"]
    incompatible_field_issues = [i for i in issues if i["rule"] == "incompatible_fields"]
    assert len(incompatible_field_issues) == 1
    assert "metrics.conversions" in incompatible_field_issues[0]["description"]
    assert "segments.keyword.info.text" in incompatible_field_issues[0]["description"]


def test_auto_correct_safety() -> None:
    """Test that auto-correct replaces deprecated field names but does NOT touch them inside strings or comments."""
    query = """
    SELECT
      campaign.start_date,
      'campaign.start_date' AS string_lit,
      -- This comment refers to campaign.start_date
      metrics.average_cpv
    FROM campaign
    WHERE campaign.name = 'campaign.start_date'
    """
    
    result = auto_correct_gaql_query_logic(query)
    corrected = result["corrected_query"]
    
    # The actual select field campaign.start_date should be replaced
    assert "campaign.start_date_time" in corrected
    
    # The string literal 'campaign.start_date' should NOT be replaced
    assert "'campaign.start_date'" in corrected
    
    # The comment containing campaign.start_date should NOT be replaced
    assert "-- This comment refers to campaign.start_date" in corrected
    
    # metrics.average_cpv should be replaced
    assert "metrics.trueview_average_cpv" in corrected


def test_cardinality_explosion_warning() -> None:
    """Test that selecting too many segments triggers a cardinality explosion warning."""
    parsed = {
        "wrapped_in_parentheses": False,
        "select_fields": ["campaign.id", "segments.device", "segments.ad_network_type", "segments.slot"],
        "where_fields": [],
        "orderby_fields": [],
        "limit": "100"
    }
    issues = validate_gaql_rules(parsed)
    cardinality_issues = [i for i in issues if i["rule"] == "cardinality_explosion"]
    assert len(cardinality_issues) == 1
    assert cardinality_issues[0]["severity"] == "WARNING"
    assert "cardinality explosion" in cardinality_issues[0]["description"]


def test_missing_limit_warning() -> None:
    """Test that a query without a LIMIT clause triggers a warning."""
    parsed = {
        "wrapped_in_parentheses": False,
        "select_fields": ["campaign.id"],
        "where_fields": [],
        "orderby_fields": [],
        "limit": ""
    }
    issues = validate_gaql_rules(parsed)
    limit_issues = [i for i in issues if i["rule"] == "missing_limit"]
    assert len(limit_issues) == 1
    assert limit_issues[0]["severity"] == "WARNING"


@pytest.mark.asyncio
async def test_complexity_scoring() -> None:
    """Test that query complexity score is computed correctly."""
    query = "SELECT campaign.id, segments.device, metrics.clicks FROM campaign WHERE campaign.status = 'ENABLED' ORDER BY campaign.id"
    
    async def mock_metadata_provider(field_name: str) -> Dict[str, Any]:
        return {"name": field_name, "selectable": True, "filterable": True, "sortable": True}
        
    result = await validate_gaql_query_with_metadata(query, mock_metadata_provider)
    assert result["valid"] is True
    # Complexity: 3 fields (campaign.id, segments.device, metrics.clicks) 
    # + 1 non-date segment * 2 (segments.device)
    # + 1 filter (campaign.status)
    # + 1 sort (campaign.id)
    # Total = 3 + 2 + 1 + 1 = 7
    assert result["complexity_score"] == 7


def test_validate_gaql_rules_removed_fields() -> None:
    """Test validation flags v24-removed fields as CRITICAL issues."""
    parsed = {
        "wrapped_in_parentheses": False,
        "select_fields": ["campaign_budget.ad_sub_network_type", "campaign.video_brand_safety_suitability"],
        "where_fields": [],
        "orderby_fields": [],
        "limit": "100"
    }
    issues = validate_gaql_rules(parsed)
    removed_issues = [i for i in issues if i["rule"] == "removed_field"]
    assert len(removed_issues) == 2
    assert all(i["severity"] == "CRITICAL" for i in removed_issues)
    assert any("ad_sub_network_type" in i["description"] for i in removed_issues)
    assert any("video_brand_safety_suitability" in i["description"] for i in removed_issues)


