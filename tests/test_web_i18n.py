"""Static checks for the web page strings: every key the page uses exists in the English file,
and the page keeps to the content security policy (no inline script or style)."""
import ast
import json
import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"
APP = (WEB / "app.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
EN = json.loads((WEB / "i18n" / "en-US.json").read_text(encoding="utf-8"))
CODE = "\n".join(line for line in APP.splitlines() if not line.lstrip().startswith("//"))
NAMESPACES = {k.split(".", 1)[0] for k in EN}


def has(key):
    return key in EN or f"{key}.other" in EN


def html_keys():
    keys = re.findall(r'data-i18n="([^"]+)"', HTML)
    for attr in re.findall(r'data-i18n-attr="([^"]+)"', HTML):
        keys += [pair.split(":", 1)[1].strip() for pair in attr.split(";")]
    return keys


def t_keys():
    return re.findall(r'\bt\(\s*"([^"]+)"', CODE)


def literal_keys():
    """Keys kept in maps or passed to helpers (busyButton, QUOTA_GROUPS): string literals in a string namespace."""
    return [k for k in re.findall(r'"([a-zA-Z]+\.[a-zA-Z][a-zA-Z0-9]*)"', CODE) if k.split(".", 1)[0] in NAMESPACES]


def test_every_t_key_exists():
    keys = t_keys()
    assert len(keys) > 250
    assert [k for k in keys if not has(k)] == []


def test_every_static_key_exists():
    keys = html_keys()
    assert len(keys) > 100
    assert [k for k in keys if not has(k)] == []


def test_keys_in_maps_exist():
    assert [k for k in literal_keys() if not has(k)] == []


def test_no_unused_strings():
    used = set(t_keys()) | set(html_keys()) | set(literal_keys())
    base = {re.sub(r"\.(one|other)$", "", k) for k in EN}
    assert sorted(base - used) == []


def test_plural_pairs_are_complete():
    ones = [k for k in EN if k.endswith(".one")]
    assert ones
    assert [k for k in ones if f"{k[:-4]}.other" not in EN] == []


def test_strings_use_named_placeholders_only():
    for key, text in EN.items():
        assert isinstance(text, str), key
        # {name} placeholders only: no positional {0} and no leftover template syntax.
        assert not re.search(r"\{\d+\}|\$\{|%s", text), key


@pytest.mark.parametrize("pattern", [r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML", r"\.style\b",
                                     r"setAttribute\(\s*[\"']style", r"\bconfirm\(", r"\balert\("])
def test_app_keeps_to_the_csp(pattern):
    assert not re.search(pattern, APP)


def test_html_has_no_inline_script_or_style():
    assert "style=" not in HTML
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", HTML)
    assert "<style" not in HTML


def test_language_list_matches_the_string_files():
    locales = re.search(r"const LOCALES = \[([^\]]*)\]", APP).group(1)
    listed = re.findall(r'"([a-zA-Z-]+)"', locales)
    files = sorted(p.stem for p in (WEB / "i18n").glob("*.json"))
    assert sorted(listed) == files
    lang_select = re.search(r'<select id="lang">(.*?)</select>', HTML, re.S).group(1)
    assert re.findall(r'<option value="([a-zA-Z-]+)"', lang_select) == ["en-US", "zh-TW", "zh-CN"]


# ------------------------------------------------------------ every locale file against the English file
LOCALES = re.findall(r'"([a-zA-Z-]+)"', re.search(r"const LOCALES = \[([^\]]*)\]", APP).group(1))
# Plural categories from Intl.PluralRules(locale).resolvedOptions().pluralCategories, for the strings this page has:
# English has one and other; Chinese has only other, so its .one entries are duplicates the page never picks.
PLURAL_CATEGORIES = {"en-US": {"one", "other"}, "zh-TW": {"other"}, "zh-CN": {"other"}}
PLURAL_RE = re.compile(r"^(.*)\.(zero|one|two|few|many|other)$")


def locale_strings(code):
    return json.loads((WEB / "i18n" / f"{code}.json").read_text(encoding="utf-8"))


def placeholders(text):
    return sorted(set(re.findall(r"\{(\w+)\}", text)))


def plural_groups(strings):
    groups = {}
    for key in strings:
        m = PLURAL_RE.match(key)
        if m:
            groups.setdefault(m.group(1), set()).add(m.group(2))
    return groups


def test_three_locales_ship():
    assert LOCALES == ["en-US", "zh-TW", "zh-CN"]
    assert set(PLURAL_CATEGORIES) == set(LOCALES)


@pytest.mark.parametrize("code", ["en-US", "zh-TW", "zh-CN"])
def test_locale_file_is_valid_json_with_no_empty_values(code):
    raw = (WEB / "i18n" / f"{code}.json").read_text(encoding="utf-8")
    pairs = json.loads(raw, object_pairs_hook=lambda kv: kv)
    keys = [k for k, _ in pairs]
    assert len(keys) == len(set(keys)), "duplicate keys"
    assert all(isinstance(v, str) and v.strip() for _, v in pairs), [k for k, v in pairs if not (isinstance(v, str) and v.strip())]


@pytest.mark.parametrize("code", ["zh-TW", "zh-CN"])
def test_locale_has_the_english_key_set(code):
    strings = locale_strings(code)
    assert sorted(set(EN) - set(strings)) == [], "missing"
    assert sorted(set(strings) - set(EN)) == [], "extra"


@pytest.mark.parametrize("code", ["zh-TW", "zh-CN"])
def test_locale_keeps_the_placeholders_of_each_key(code):
    strings = locale_strings(code)
    assert [k for k in EN if k in strings and placeholders(strings[k]) != placeholders(EN[k])] == []


@pytest.mark.parametrize("code", ["en-US", "zh-TW", "zh-CN"])
def test_plural_forms_match_the_locale(code):
    groups = plural_groups(locale_strings(code))
    assert groups
    needed = PLURAL_CATEGORIES[code]
    # Every category the locale uses exists, and .other always does.
    assert [b for b, forms in groups.items() if not (needed | {"other"}) <= forms] == []
    # Only categories the locale has, plus English's .one kept as a duplicate in the shared key set.
    assert [b for b, forms in groups.items() if not forms <= (needed | {"one", "other"})] == []


@pytest.mark.parametrize("code", ["zh-TW", "zh-CN"])
def test_plural_count_placeholder_kept(code):
    strings = locale_strings(code)
    for base, forms in plural_groups(EN).items():
        if "{count}" in EN[f"{base}.other"]:
            assert "{count}" in strings[f"{base}.other"], base


# ------------------------------------------------------------ API codes the page maps to its own text
API = (WEB.parent / "lambda" / "api.py").read_text(encoding="utf-8")
CB = (WEB.parent / "lambda" / "capacity_blocks.py").read_text(encoding="utf-8")


def js_map_keys(name):
    body = re.search(rf"const {name} = \{{(.*?)\n\}};", APP, re.S).group(1)
    return set(re.findall(r'(?:^|[\s,{])"?([a-z][a-z-]*)"?\s*:\s*\(', body, re.M))


def api_error_codes():
    """The code argument of every HttpError(...) and error_body(...) call in api.py."""
    codes = set()
    for node in ast.walk(ast.parse(API)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("HttpError", "error_body"):
            pos = 2 if node.func.id == "HttpError" else 1
            if len(node.args) > pos and isinstance(node.args[pos], ast.Constant):
                codes.add(node.args[pos].value)
    return codes


# API codes that have no page text yet; add a code here only while its page text is pending.
PENDING_PAGE_TEXT = set()


def test_every_api_error_code_has_page_text():
    codes = api_error_codes()
    assert len(codes) > 25
    assert sorted(codes - js_map_keys("ERROR_TEXT") - PENDING_PAGE_TEXT) == []
    # A pending code is a real API code.
    assert PENDING_PAGE_TEXT <= codes


def test_every_region_error_code_has_page_text():
    fn = API[API.index("def region_error_code"):API.index("def region_failure")]
    codes = set(re.findall(r'return "([a-z][a-z-]+)", ', fn)) | {re.search(r'NOT_ENABLED_CODE = "([a-z-]+)"', API).group(1)}
    assert codes == {"not-enabled", "aws-error", "no-answer"}
    assert sorted(codes - js_map_keys("REGION_ERROR_TEXT")) == []


def test_every_capacity_block_code_has_page_text():
    notes = set(re.findall(r'WINDOW_\w+ = "([a-z-]+)"', CB))
    assert notes == {"cb-window-end", "cb-window-start"}
    assert sorted(notes - js_map_keys("CB_NOTE_TEXT")) == []
    reasons = set(re.findall(r'"(cb-[a-z-]+)"', CB)) - notes
    handled = set(re.findall(r'case "(cb-[a-z-]+)"', APP))
    assert reasons and sorted(reasons - handled) == []


def test_probe_results_have_page_text():
    results = set(re.findall(r'return "([a-z-]+)"', API[API.index("def classify_error"):API.index("def iso")])) | {"launched", "in-progress"}
    assert sorted(results - js_map_keys("PROBE_RESULT")) == []


def test_throttled_and_capacity_block_alert_codes_have_page_text():
    assert {"throttled", "sps-limited", "cb-unsupported-type"} <= js_map_keys("ERROR_TEXT")


def test_spot_heatmap_codes_have_page_text():
    """The batch Spot score route reports per-type error codes; the page maps each one to its own text."""
    assert {"sps-limited", "sps-unsupported", "sps-error", "sps-timeout"} <= js_map_keys("ERROR_TEXT")


def test_compare_regions_has_no_quota_or_type_picker():
    """Compare Regions is a heatmap of every offered type: no type select, no list editor, no quota columns."""
    panel = HTML[HTML.index('id="panel-compare"'):HTML.index('id="panel-capacity"')]
    assert 'id="cmp-type"' not in panel and 'id="cmp-edit-list"' not in panel
    assert 'id="cmp-table" class="heat"' in panel and 'class="scroll heat-scroll"' in panel
    assert "/api/quota-overview" not in APP


# ------------------------------------------------------------ the direction: signals and validation, no control plane
def tool_bar(panel_id):
    """The HTML of a tab's first panel-tools block (the buttons next to the heading)."""
    start = HTML.index(f'id="{panel_id}"')
    block = HTML[HTML.index('class="panel-tools', start):]
    return block[:block.index("</div>\n          </div>")] if "</div>\n          </div>" in block else block


def test_hold_feature_is_gone_from_page_and_strings():
    for word in ("/api/reservations", "hold-badge", "reservationEnabled", "reservationHours", "maxReservationsPerUser", "HOLDING_STATES"):
        assert word not in APP and word not in HTML, word
    assert not [k for k in EN if re.match(r"(hold|holds|state|badge)\.", k)]
    assert 'id="tab-holds"' not in HTML and 'id="tab-alerts"' in HTML and "Holds" not in EN["tab.alerts"]
    assert "holds: \"alerts\"" in APP and "reservations: \"alerts\"" in APP


def test_every_tab_ends_its_tool_bar_with_one_refresh_button():
    ends = {"panel-compare": "cmp-refresh", "panel-capacity": "cap-refresh", "panel-blocks": "cb-search-again",
            "panel-quotas": "q-refresh", "panel-alerts": "alerts-refresh"}
    for panel, button in ends.items():
        bar = tool_bar(panel)
        buttons = re.findall(r'<button id="([a-z-]+)"[^>]*class="(?:ghost|link)?[^"]*"', bar)
        assert buttons and buttons[-1] == button, (panel, buttons)
    assert "cmp-refresh-scores" not in HTML and "cb-refresh" not in HTML


def test_freshness_labels_have_one_wording_and_no_cached_or_live():
    values = " ".join(EN.values()).lower()
    assert not re.search(r"\bcached\b|\blive\b", values)
    for key in ("fresh.updated", "fresh.justNow", "fresh.updating", "fresh.blocked", "fresh.infoSpot", "fresh.infoCapacity", "fresh.infoQuotas", "fresh.infoBlocks", "fresh.infoAlerts"):
        assert key in EN
    assert EN["fresh.updated"] == "Updated {when}" and EN["fresh.updating"] == "Updating..."
    assert "{scoreMinutes}" in EN["fresh.infoSpot"] and "{quotaMinutes}" in EN["fresh.infoQuotas"]
    assert "{seconds}" in EN["fresh.blocked"]


def test_no_example_prices_in_strings():
    for code in LOCALES:
        for key, text in locale_strings(code).items():
            assert not re.search(r"[$\u00a5\u20ac\u00a3]\s?\d|\d\s?(USD|CNY|EUR)", text), (code, key)


def test_cli_reservation_command_is_a_template_filled_from_the_row():
    assert "aws ec2 create-capacity-reservation --region ${region} --availability-zone ${az} --instance-type ${type} --instance-platform Linux/UNIX" in APP
    assert "--instance-count 1 --instance-match-criteria targeted --end-date-type limited --end-date ${reserveEnd()}" in APP
    assert "/api/reservations" not in APP


CSS = (WEB / "style.css").read_text(encoding="utf-8")


def test_tool_bars_share_one_grid_with_fixed_tracks():
    """Every tab head is the heading, then one tool bar grid [filters | freshness | info | action] with fixed tracks,
    so a language or a state never moves a control (measured in a browser; this guards the structure)."""
    assert "grid-template-columns:minmax(0, 1fr) var(--w-fresh) var(--w-info) var(--w-action)" in CSS
    for var in ("--w-region", "--w-type", "--w-fresh", "--w-action", "--ctl-h", "--label-h"):
        assert re.search(rf"{var}:\d+px", CSS), var
    for panel in ("panel-compare", "panel-capacity", "panel-quotas"):
        assert '<div class="panel-filters">' in tool_bar(panel), panel
    for panel in ("panel-blocks", "panel-alerts"):
        start = HTML.index(f'id="{panel}"')
        assert HTML.index('class="panel-tools', start) == HTML.index('class="panel-tools no-filters"', start), panel
    # Families sits in the Compare Regions bar; every select in a bar has its label above it in a fixed-width field
    assert 'id="cmp-filters"' in tool_bar("panel-compare")
    for select in ("cap-region", "cap-type", "q-region"):
        assert re.search(rf'<label class="tool-field w-\w+" for="{select}"><span class="field-label"', HTML), select
    # The add-alert form places each field in a named area; Threshold shares the Count area
    assert 'class="tool-field af-count" id="alert-threshold-label"' in HTML and "grid-template-areas" in CSS


def test_quota_tip_is_two_lines_on_the_name_with_no_badge_or_link():
    """The quota name carries a two-line tip box (what the quota counts, then its families); no badge, link, or hardware line."""
    for key in ("q.typeBadge", "q.whichCount", "q.countsType", "q.countsFamilies", "q.hwP", "q.hwG", "q.hwTrn", "q.hwInf", "q.hwCb"):
        assert key not in EN and key not in APP, key
    assert EN["q.countsCb"].endswith(":") and "{" not in EN["q.countsCb"]
    # Line 1: AWS's own count for the whole quota group in this account; no "track" wording (there is no such setting).
    for key in ("q.countsOd", "q.countsSpot", "q.groupFamilies", "q.groupExamples"):
        assert "track" not in EN[key].lower(), key
    for key in ("q.countsOd", "q.countsSpot"):
        assert "{group}" in EN[key] and "{examples}" not in EN[key] and "whole {group} quota group in this account" in EN[key]
    assert "running On-Demand" in EN["q.countsOd"] and "Spot requests" in EN["q.countsSpot"]
    # Line 2: the group's families the enabled Regions offer, or the examples while not known.
    assert EN["q.groupFamilies"] == "Families in this group that the enabled Regions offer: {families}."
    assert "{examples}" in EN["q.groupExamples"] and "q.tracks" not in EN and "q.tracks" not in APP
    for lang in ("zh-TW", "zh-CN"):
        loc = json.loads((WEB / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        for key in ("q.countsOd", "q.countsSpot"):
            assert "{group}" in loc[key] and "{examples}" not in loc[key] and loc[key].endswith("。"), (lang, key)
        assert "{families}" in loc["q.groupFamilies"] and loc["q.groupFamilies"].endswith("。"), lang
        assert "{examples}" in loc["q.groupExamples"] and loc["q.groupExamples"].endswith("。"), lang
        assert "追蹤" not in "".join(loc[k] for k in ("q.countsOd", "q.countsSpot", "q.groupFamilies", "q.groupExamples"))
        assert "跟踪" not in "".join(loc[k] for k in ("q.countsOd", "q.countsSpot", "q.groupFamilies", "q.groupExamples"))
    # No VT family is named in the fallback examples.
    examples = APP[APP.index("const QUOTA_COUNTS"):APP.index("const tipOffer")]
    assert "vt1" not in examples
    body = APP[APP.index("function quotaInfoLines"):APP.index("function quotaName")]
    assert "lastUsed" not in body and "*" not in body and "QUOTA_COUNTS" in APP
    name = APP[APP.index("function quotaName"):]
    name = name[: name.index("\n}\n")]
    assert 'role: "tooltip"' in name and 'tabindex: "0"' in name and "aria-describedby" in name
    assert "tag-hl" not in APP and "tag-btn" not in APP and "info-link" not in APP and "q-info-slot" not in APP
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert "tag-hl" not in css and "tag-btn" not in css and "q-info-slot" not in css


def test_users_delete_note_names_a_real_admin_guide_section():
    guide = (WEB.parent / "docs" / "admin-guide.md").read_text(encoding="utf-8")
    headings = [line.lstrip("#").strip() for line in guide.splitlines() if line.startswith("#")]
    note = EN["users.deleteNote"]
    assert [h for h in headings if h.startswith("Remove a user") and h in note] != []
    # The stack keeps no per-user saved data: the type list lives in the browser.
    assert "saved data" not in note
    # The HTML text before the script runs is the same message on one line; the page shows it as points.
    assert note.replace("\n", " ") in HTML


def test_language_and_config_files_are_revalidated_on_every_load():
    """A browser that keeps an old language file shows a new string key as raw text (seen after a release)."""
    js = APP
    assert "fetch(`i18n/${code}.json`, { cache: \"no-cache\" })" in js
    assert "fetch(\"config.json\", { cache: \"no-cache\" })" in js
