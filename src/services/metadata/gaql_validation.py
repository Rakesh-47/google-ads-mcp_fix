"""GAQL parsing and validation utility layer for v24 migration."""

import re
from typing import Dict, Any, List, Set, Callable, Awaitable, NamedTuple

DEPRECATION_MAPPING = {
    "metrics.average_cpv": "metrics.trueview_average_cpv",
    "metrics.video_view_rate": "metrics.video_trueview_view_rate",
    "metrics.video_views": "metrics.video_trueview_views",
    "campaign.start_date": "campaign.start_date_time",
    "campaign.end_date": "campaign.end_date_time",
    "billing_setup.start_date": "billing_setup.start_date_time",
    "billing_setup.end_date": "billing_setup.end_date_time",
    "account_budget_proposal.proposed_start_date": "account_budget_proposal.proposed_start_date_time",
    "account_budget_proposal.proposed_end_date": "account_budget_proposal.proposed_end_date_time",
}

CORE_DATE_SEGMENTS = {
    "segments.date",
    "segments.week",
    "segments.month",
    "segments.quarter",
    "segments.year",
}

# v24 Specific removed fields and their migration explanations
REMOVED_FIELDS = {
    "campaign_budget.ad_sub_network_type": "Field 'ad_sub_network_type' was removed from 'campaign_budget' in v24.",
    "ad_group_asset.click_type": "Field 'click_type' was removed from 'AdGroupAsset' in v24.",
    "campaign_asset.click_type": "Field 'click_type' was removed from 'CampaignAsset' in v24.",
    "customer_asset.click_type": "Field 'click_type' was removed from 'CustomerAsset' in v24.",
    "campaign.video_brand_safety_suitability": "Field 'video_brand_safety_suitability' was removed from 'Campaign' in v24.",
    "experiment.traffic_split_percent": "Field 'traffic_split_percent' was removed from 'experiment' in v24. Query 'experiment_arm' instead.",
    "experiment.campaigns": "Field 'campaigns' was removed from 'experiment' in v24. Query 'experiment_arm' instead.",
    "customer_client.auto_tagging_enabled": "Field 'auto_tagging_enabled' was removed from 'customer_client' in v24.",
}


class Token(NamedTuple):
    type: str  # 'BLOCK_COMMENT', 'LINE_COMMENT', 'STRING', 'SYMBOL', 'WORD', 'NUMBER', 'WHITESPACE'
    value: str
    position: int


def tokenize(query: str) -> List[Token]:
    """Tokenize a GAQL query string into lexical tokens.

    This ensures robust parsing that ignores comments and string literals.
    """
    token_specification = [
        ("BLOCK_COMMENT", r"/\*.*?\*/"),
        ("LINE_COMMENT", r"--[^\r\n]*"),
        ("STRING", r"'(?:''|[^'])*'"),  # Matches single-quoted strings, supporting escaped single quotes ''
        ("SYMBOL", r"!=|<=|>=|[(),=\[\]<>]"),
        ("WORD", r"[a-zA-Z_][a-zA-Z0-9_\.]*"),  # Matches identifiers, keywords, and field names with dots
        ("NUMBER", r"\d+"),
        ("WHITESPACE", r"\s+"),
        ("MISMATCH", r"."),  # Any other character (treated as symbol/mismatch)
    ]

    tok_regex = "|".join(f"(?P<{name}>{pattern})" for name, pattern in token_specification)
    tokens = []
    for mo in re.finditer(tok_regex, query, re.DOTALL):
        kind = mo.lastgroup
        value = mo.group()
        pos = mo.start()
        if kind == "MISMATCH":
            kind = "SYMBOL"
        tokens.append(Token(kind, value, pos))
    return tokens


def parse_gaql_query(query: str) -> Dict[str, Any]:
    """Parse a GAQL query into components using a lexical tokenizer.

    This is robust against weird spacing, string literals, and comments.

    Args:
        query: The raw GAQL query string.

    Returns:
        Dictionary of parsed query clauses and fields.
    """
    tokens = tokenize(query)

    # Filter out whitespace and comments to check if it's wrapped in parentheses
    non_ws_tokens = [
        t
        for t in tokens
        if t.type not in ("WHITESPACE", "LINE_COMMENT", "BLOCK_COMMENT")
    ]

    wrapped_in_parentheses = False
    if (
        len(non_ws_tokens) >= 2
        and non_ws_tokens[0].value == "("
        and non_ws_tokens[-1].value == ")"
    ):
        wrapped_in_parentheses = True

    # Partition query tokens into clauses by top-level keywords: SELECT, FROM, WHERE, ORDER BY, LIMIT
    clauses = {
        "SELECT": [],
        "FROM": [],
        "WHERE": [],
        "ORDER BY": [],
        "LIMIT": [],
    }

    current_clause = None
    i = 0
    while i < len(tokens):
        t = tokens[i]

        if t.type == "WORD":
            val_upper = t.value.upper()
            if val_upper == "SELECT":
                current_clause = "SELECT"
                i += 1
                continue
            elif val_upper == "FROM":
                current_clause = "FROM"
                i += 1
                continue
            elif val_upper == "WHERE":
                current_clause = "WHERE"
                i += 1
                continue
            elif val_upper == "LIMIT":
                current_clause = "LIMIT"
                i += 1
                continue
            elif (
                val_upper == "ORDER"
                and i + 2 < len(tokens)
                and tokens[i + 1].type == "WHITESPACE"
                and tokens[i + 2].type == "WORD"
                and tokens[i + 2].value.upper() == "BY"
            ):
                current_clause = "ORDER BY"
                i += 3
                continue

        if current_clause:
            clauses[current_clause].append(t)
        i += 1

    # Helper to extract fields (WORD tokens containing '.') from clause tokens
    def extract_fields(clause_tokens: List[Token]) -> List[str]:
        fields = []
        for t in clause_tokens:
            if t.type == "WORD" and "." in t.value:
                fields.append(t.value)
        # Preserve original case, but remove duplicates case-insensitively
        seen = set()
        unique_fields = []
        for f in fields:
            f_low = f.lower()
            if f_low not in seen:
                seen.add(f_low)
                unique_fields.append(f)
        return unique_fields

    select_fields = extract_fields(clauses["SELECT"])
    where_fields = extract_fields(clauses["WHERE"])
    orderby_fields = extract_fields(clauses["ORDER BY"])

    # Extract resource name (first WORD token in FROM clause)
    resource_name = ""
    for t in clauses["FROM"]:
        if t.type == "WORD":
            resource_name = t.value
            break

    # Extract limit value (first NUMBER token in LIMIT clause)
    limit_value = ""
    for t in clauses["LIMIT"]:
        if t.type == "NUMBER":
            limit_value = t.value
            break

    # Reconstruct stripped query (stripping outermost parentheses)
    start_idx = 0
    end_idx = len(tokens)
    if wrapped_in_parentheses:
        first_p = -1
        last_p = -1
        for idx, t in enumerate(tokens):
            if t.value == "(":
                first_p = idx
                break
        for idx in range(len(tokens) - 1, -1, -1):
            if tokens[idx].value == ")":
                last_p = idx
                break
        if first_p != -1 and last_p != -1:
            start_idx = first_p + 1
            end_idx = last_p

    query_stripped = "".join(t.value for t in tokens[start_idx:end_idx]).strip()

    return {
        "original_query": query,
        "query_stripped": query_stripped,
        "wrapped_in_parentheses": wrapped_in_parentheses,
        "select_fields": select_fields,
        "resource_name": resource_name,
        "where_fields": where_fields,
        "orderby_fields": orderby_fields,
        "limit": limit_value,
        "tokens": tokens,
        "start_idx": start_idx,
        "end_idx": end_idx,
        "where_clause": "".join(t.value for t in clauses["WHERE"]).strip(),
        "orderby_clause": "".join(t.value for t in clauses["ORDER BY"]).strip(),
    }


def validate_gaql_rules(parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Validate parsed query against local v24 rules and return list of issues.

    Args:
        parsed: Dict containing parsed query elements from parse_gaql_query.

    Returns:
        List of issues found, each with rule, severity, and description.
    """
    issues = []

    # 1. Parentheses Error (invalid root syntax rule)
    if parsed["wrapped_in_parentheses"]:
        issues.append(
            {
                "rule": "parentheses",
                "severity": "CRITICAL",
                "description": "Query is wrapped in outer parentheses. In v24, queries must start directly with SELECT to prevent syntax errors.",
            }
        )

    # 2. Segment Selection Rule (v24 segmentation rule)
    select_fields_lower = [f.lower() for f in parsed["select_fields"]]
    referenced_segments = set()

    for f in parsed["where_fields"] + parsed["orderby_fields"]:
        if f.lower().startswith("segments."):
            referenced_segments.add(f)

    for seg in referenced_segments:
        seg_lower = seg.lower()
        # Date segments are specifically exempt
        if seg_lower in CORE_DATE_SEGMENTS:
            continue
        if seg_lower not in select_fields_lower:
            issues.append(
                {
                    "rule": "missing_segment_select",
                    "severity": "CRITICAL",
                    "description": f"Segment field '{seg}' is used in WHERE/ORDER BY but is missing from SELECT. In v24, all segments used in filters or ordering must be explicitly selected.",
                }
            )

    # 3. Deprecated Fields Warning
    all_referenced_fields = set(
        parsed["select_fields"] + parsed["where_fields"] + parsed["orderby_fields"]
    )
    for field in all_referenced_fields:
        field_lower = field.lower()
        if field_lower in DEPRECATION_MAPPING:
            replacement = DEPRECATION_MAPPING[field_lower]
            issues.append(
                {
                    "rule": "deprecated_field",
                    "severity": "CRITICAL",
                    "description": f"Field '{field}' is deprecated in v24 and returns null or is rejected. Use '{replacement}' instead.",
                }
            )
        elif field_lower in REMOVED_FIELDS:
            explanation = REMOVED_FIELDS[field_lower]
            issues.append(
                {
                    "rule": "removed_field",
                    "severity": "CRITICAL",
                    "description": f"{explanation} In v24, this query will fail syntax or schema validation.",
                }
            )

    # 4. Cardinality Explosion warning
    non_date_segments = [
        f
        for f in parsed["select_fields"]
        if f.lower().startswith("segments.") and f.lower() not in CORE_DATE_SEGMENTS
    ]
    if len(non_date_segments) > 2:
        issues.append(
            {
                "rule": "cardinality_explosion",
                "severity": "WARNING",
                "description": f"Selecting multiple segment fields ({', '.join(non_date_segments)}) can cause a cardinality explosion, significantly increasing the row count returned.",
            }
        )

    # 5. Missing LIMIT Warning
    if not parsed.get("limit"):
        issues.append(
            {
                "rule": "missing_limit",
                "severity": "WARNING",
                "description": "Query does not specify a LIMIT clause. It may pull an unsafe, excessively large result set from the Google Ads API.",
            }
        )

    return issues


def _clean_resource_name(name: str) -> str:
    """Helper to strip googleAdsFields/ prefix and lowercase resource names."""
    if name.lower().startswith("googleadsfields/"):
        name = name[16:]
    return name.strip().lower()


async def validate_gaql_query_with_metadata(
    query: str, metadata_provider: Callable[[str], Awaitable[Dict[str, Any]]]
) -> Dict[str, Any]:
    """Validate GAQL query against local rules and field compatibility metadata.

    Args:
        query: The raw GAQL query.
        metadata_provider: Coroutine that fetches metadata for a given field.

    Returns:
        Dictionary of validation results (valid: bool, issues: List[Dict]).
    """
    # 1. Parse query using robust token-based parser
    parsed = parse_gaql_query(query)

    # 2. Local rules validation
    issues = validate_gaql_rules(parsed)

    # 3. Resource validation
    resource_name = parsed["resource_name"]
    if not resource_name:
        issues.append(
            {
                "rule": "missing_resource",
                "severity": "CRITICAL",
                "description": "Could not identify resource in FROM clause.",
            }
        )
        return {"valid": False, "issues": issues, "parsed_query": parsed}

    # 4. Fetch metadata for all referenced fields and validate
    all_referenced_fields = set(
        parsed["select_fields"] + parsed["where_fields"] + parsed["orderby_fields"]
    )

    # Keep a local store of fetched metadata for compatibility checks
    field_metadata: Dict[str, Dict[str, Any]] = {}

    for field in all_referenced_fields:
        # Ignore fields that are deprecated (already flagged as CRITICAL)
        if field.lower() in DEPRECATION_MAPPING:
            continue

        try:
            metadata = await metadata_provider(field)
            if not metadata or not metadata.get("name"):
                issues.append(
                    {
                        "rule": "invalid_field",
                        "severity": "HIGH",
                        "description": f"Field '{field}' was not found in the Google Ads API schema.",
                    }
                )
                continue

            field_metadata[field] = metadata

            # Verify selectability
            if field in parsed["select_fields"] and not metadata.get(
                "selectable", False
            ):
                issues.append(
                    {
                        "rule": "not_selectable",
                        "severity": "HIGH",
                        "description": f"Field '{field}' is selected but is marked as not selectable in the schema.",
                    }
                )

            # Verify filterability
            if field in parsed["where_fields"] and not metadata.get(
                "filterable", False
            ):
                issues.append(
                    {
                        "rule": "not_filterable",
                        "severity": "HIGH",
                        "description": f"Field '{field}' is filtered in WHERE but is marked as not filterable in the schema.",
                    }
                )

            # Verify sortability
            if field in parsed["orderby_fields"] and not metadata.get(
                "sortable", False
            ):
                issues.append(
                    {
                        "rule": "not_sortable",
                        "severity": "HIGH",
                        "description": f"Field '{field}' is sorted in ORDER BY but is marked as not sortable in the schema.",
                    }
                )

        except Exception as e:
            issues.append(
                {
                    "rule": "metadata_check_failed",
                    "severity": "MEDIUM",
                    "description": f"Could not verify field metadata for '{field}': {str(e)}",
                }
            )

    # 5. Resource-Scoped Validation (hard metadata-driven compatibility check)
    core_res_clean = _clean_resource_name(resource_name)
    for field, meta in field_metadata.items():
        # Attributes of the FROM resource are implicitly compatible
        if field.lower().startswith(core_res_clean + "."):
            continue

        # If compatibility metadata is present, check against the FROM resource
        # Using both attribute_resources and selectable_with
        has_comp_metadata = ("selectable_with" in meta) or ("attribute_resources" in meta)
        if has_comp_metadata:
            selectable_with = [s.lower() for s in meta.get("selectable_with", [])]
            attribute_resources = [_clean_resource_name(r) for r in meta.get("attribute_resources", [])]

            # The field is compatible with FROM resource if the resource is in attribute_resources or selectable_with
            is_compatible = (
                core_res_clean in selectable_with or
                core_res_clean in attribute_resources
            )
            if not is_compatible:
                issues.append(
                    {
                        "rule": "incompatible_resource",
                        "severity": "HIGH",
                        "description": f"Field '{field}' is not compatible with the FROM resource '{resource_name}' according to compatibility metadata.",
                    }
                )

    # 6. Cross-Field Compatibility Validation (using selectable_with as primary signal)
    fields_list = list(field_metadata.keys())
    for idx, f1 in enumerate(fields_list):
        meta_f1 = field_metadata[f1]
        
        # Only validate if compatibility metadata is present
        if "selectable_with" not in meta_f1:
            continue
            
        f1_sel_with = [s.lower() for s in meta_f1.get("selectable_with", [])]
        if not f1_sel_with:
            continue

        for f2 in fields_list[idx + 1:]:
            meta_f2 = field_metadata[f2]
            
            f2_lower = f2.lower()
            f2_prefix = f2_lower.split(".")[0] if "." in f2_lower else ""
            
            # Check if f2 (or its resource prefix/category prefix) is in f1_sel_with
            is_f2_compatible = (
                f2_lower in f1_sel_with or
                (f2_prefix and f2_prefix in f1_sel_with)
            )

            # Bidirectional check: check f1 against f2's selectable_with if present
            is_f1_compatible = True
            if "selectable_with" in meta_f2:
                f2_sel_with = [s.lower() for s in meta_f2.get("selectable_with", [])]
                if f2_sel_with:
                    f1_lower = f1.lower()
                    f1_prefix = f1_lower.split(".")[0] if "." in f1_lower else ""
                    is_f1_compatible = (
                        f1_lower in f2_sel_with or
                        (f1_prefix and f1_prefix in f2_sel_with)
                    )

            if not is_f2_compatible or not is_f1_compatible:
                issues.append(
                    {
                        "rule": "incompatible_fields",
                        "severity": "HIGH",
                        "description": f"Fields '{f1}' and '{f2}' cannot be selected together according to compatibility metadata (selectable_with).",
                    }
                )

    has_critical_or_high = any(
        issue["severity"] in ("CRITICAL", "HIGH") for issue in issues
    )
    valid = not has_critical_or_high

    # Compute premium Query Complexity Score
    num_fields = len(parsed["select_fields"])
    num_filters = len(parsed["where_fields"])
    num_sorting = len(parsed["orderby_fields"])
    num_segments = len(
        [
            f
            for f in parsed["select_fields"]
            if f.lower().startswith("segments.")
            and f.lower() not in CORE_DATE_SEGMENTS
        ]
    )
    # Formula: fields (1pt each) + segments (2pt join penalty each) + filters (1pt each) + sorting (1pt each)
    complexity_score = num_fields + (num_segments * 2) + num_filters + num_sorting

    return {
        "valid": valid,
        "issues": issues,
        "complexity_score": complexity_score,
        "parsed_query": {
            "resource": resource_name,
            "select_fields": parsed["select_fields"],
            "where_fields": parsed["where_fields"],
            "orderby_fields": parsed["orderby_fields"],
            "limit": parsed["limit"],
        },
    }


def auto_correct_gaql_query_logic(query: str) -> Dict[str, Any]:
    """Automatically correct a GAQL query to modern v24 compliance.

    Args:
        query: The raw query to clean.

    Returns:
        Dict containing original_query, corrected_query, and changes_made list.
    """
    parsed = parse_gaql_query(query)
    tokens = parsed["tokens"]
    start_idx = parsed["start_idx"]
    end_idx = parsed["end_idx"]
    wrapped_in_parentheses = parsed["wrapped_in_parentheses"]

    changes_made = []

    if wrapped_in_parentheses:
        changes_made.append("Removed outer parentheses surrounding query.")

    # 1. Replace deprecated fields by updating WORD tokens safely
    new_tokens = list(tokens[start_idx:end_idx])
    for idx, t in enumerate(new_tokens):
        if t.type == "WORD":
            val_lower = t.value.lower()
            if val_lower in DEPRECATION_MAPPING:
                replacement = DEPRECATION_MAPPING[val_lower]
                new_tokens[idx] = Token(t.type, replacement, t.position)
                changes_made.append(
                    f"Replaced deprecated field '{t.value}' with '{replacement}'."
                )

    # 2. Inject missing segments into SELECT clause safely
    # Extract all segments from WHERE and ORDER BY
    referenced_segments = set()
    for f in parsed["where_fields"] + parsed["orderby_fields"]:
        if f.lower().startswith("segments."):
            referenced_segments.add(f)

    # Filter out core date segments
    missing_segments = []
    select_fields_lower = [f.lower() for f in parsed["select_fields"]]
    for seg in referenced_segments:
        seg_lower = seg.lower()
        if seg_lower in CORE_DATE_SEGMENTS:
            continue
        if seg_lower not in select_fields_lower:
            missing_segments.append(seg)

    if missing_segments:
        # Find the FROM keyword token
        from_idx = -1
        for idx, t in enumerate(new_tokens):
            if t.type == "WORD" and t.value.upper() == "FROM":
                from_idx = idx
                break

        if from_idx != -1:
            # Detect formatting styling (multi-line vs single-line)
            has_newline = False
            for idx in range(from_idx):
                if "\n" in new_tokens[idx].value:
                    has_newline = True
                    break

            insert_tokens = []
            for seg in missing_segments:
                insert_tokens.append(Token("SYMBOL", ",", -1))
                if has_newline:
                    insert_tokens.append(Token("WHITESPACE", "\n  ", -1))
                else:
                    insert_tokens.append(Token("WHITESPACE", " ", -1))
                insert_tokens.append(Token("WORD", seg, -1))

            # Preserve trailing whitespace/comments right before FROM
            ws_before_from = []
            target_idx = from_idx
            while target_idx > 0 and new_tokens[target_idx - 1].type in (
                "WHITESPACE",
                "LINE_COMMENT",
                "BLOCK_COMMENT",
            ):
                ws_before_from.insert(0, new_tokens[target_idx - 1])
                target_idx -= 1

            # Remove those from their original positions
            del new_tokens[target_idx:from_idx]

            # Insert our new select segment fields before the whitespace
            new_tokens = (
                new_tokens[:target_idx]
                + insert_tokens
                + ws_before_from
                + new_tokens[target_idx:]
            )
            changes_made.append(
                f"Injected missing segment field(s) into SELECT: {', '.join(missing_segments)}"
            )

    corrected_query = "".join(t.value for t in new_tokens).strip()

    return {
        "original_query": query,
        "corrected_query": corrected_query,
        "changes_made": changes_made,
        "valid_now": True,
    }
