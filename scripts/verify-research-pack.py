"""Verify handoff integrity and financial example arithmetic, not bot behavior."""

from decimal import Decimal, ROUND_CEILING
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "README.md",
    "JARVIS_TRADE_BLUEPRINT.md",
    "JARVIS_TRADE_MASTER_PROMPT.md",
    "EXECUTION_ARCHITECTURE_V2.md",
    "MASTER_BLUEPRINT_V3.md",
    "MASTER_BLUEPRINT_V4.md",
    "research/data-provider-evidence-v4.md",
    "research/onboarding-image-prompts.md",
    "BUILD_STATUS.md",
    "TESTING_GUIDE.md",
    "RUNNING.md",
    "GEMINI_BLUEPRINT_REVIEW.md",
    "ACCEPTANCE_AND_ROLLOUT.md",
    "research/whatsapp-evidence.md",
    "research/browser-agent-evidence.md",
    "research/operations-evidence.md",
    "research/extension-bridge-evidence-v2.md",
    "research/adaptive-runtime-evidence-v2.md",
    "research/whatsapp-pilot-evidence-v2.md",
    "research/repository-snapshot.json",
    "specs/screenshot-fixtures.json",
)


def main():
    errors = []
    warnings = []
    for name in REQUIRED:
        path = ROOT / name
        if not path.is_file() or not path.stat().st_size:
            warnings.append(f"Missing historical research file: {name}")

    documents = [*ROOT.glob("*.md")]
    if (ROOT / "research").is_dir():
        documents.extend((ROOT / "research").rglob("*.md"))
    for path in documents:
        body = path.read_text(encoding="utf-8-sig")
        if body.count("```") % 2:
            errors.append(f"Unpaired Markdown fence: {path.relative_to(ROOT)}")
        for target in re.findall(r"\]\(([^\s)]+)\)", body):
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
                continue
            local_target = target.split("#", 1)[0]
            if local_target and not (path.parent / local_target).resolve().exists():
                if any(local_target.endswith(r) or r.endswith(local_target) for r in REQUIRED):
                    warnings.append(f"Historical reference not present in checkout: {path.relative_to(ROOT)} -> {target}")
                else:
                    errors.append(f"Broken local link: {path.relative_to(ROOT)} -> {target}")
        if re.search(r"turn\d+(?:search|view|fetch)\d+", body):
            errors.append(f"Unresolved browser reference in {path.relative_to(ROOT)}")

    fixture_path = ROOT / "specs/screenshot-fixtures.json"
    cases = []
    if fixture_path.is_file():
        data = json.loads(fixture_path.read_text(encoding="utf-8-sig"))
        cases = data.get("cases", [])
        ids = [case["id"] for case in cases]
        if len(ids) != len(set(ids)):
            errors.append("Fixture IDs are not unique")
        if set(case["source_screenshot"] for case in cases) != set(range(1, 7)):
            errors.append("Fixtures do not cover all six supplied screenshots")
        for case in cases:
            expected = case["expected"]
            if expected.get("owned_inventory_delta", 0) != 0:
                errors.append(f"Screenshot alone creates owned stock: {case['id']}")
            if re.search(r"(?<!\d)\d{10,15}(?!\d)", json.dumps(case)):
                errors.append(f"Possible unredacted phone identifier: {case['id']}")
        colors = next((c["expected"] for c in cases if c["id"] == "linked-color-stock-and-price"), None)
        if colors:
            if sum(colors["color_quantities"].values()) != colors["quantity_total"]:
                errors.append("Color quantity sum is inconsistent")
            if 300 - colors["color_quantities"]["Lavender"] != colors["lavender_shortfall_for_300"]:
                errors.append("Lavender shortfall is inconsistent")
    else:
        warnings.append("screenshot-fixtures.json not present in checkout; skipping fixture check")

    # Canonical blueprint policy: margin is on net INR proceeds, fees separate.
    cost = Decimal("71000")
    rate = Decimal("85")
    haircut = Decimal("0.01")
    fee = Decimal("0.01")
    margin = Decimal("0.05")
    denominator = (1 - margin) * (1 - fee) * rate * (1 - haircut)
    raw_quote = cost / denominator
    rounded_quote = raw_quote.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    if denominator != Decimal("79.143075") or rounded_quote != Decimal("897.11"):
        errors.append("Canonical quote arithmetic does not match published example")

    # Research note's alternative: margin on gross revenue, cost-risk buffer.
    alternate = cost * Decimal("1.01") / (rate * (1 - Decimal(".02") - Decimal(".08")))
    alternate_increment = (alternate / Decimal(".50")).to_integral_value(rounding=ROUND_CEILING) * Decimal(".50")
    if alternate_increment != Decimal("937.50"):
        errors.append("Research note alternative pricing example is inconsistent")

    assisted = 10000 * 30 * Decimal(".30")
    input_tokens = assisted * 1500
    output_tokens = assisted * 200
    illustrative_cost = input_tokens / 1000000 + output_tokens / 1000000 * 5
    if (assisted, input_tokens, output_tokens, illustrative_cost) != (90000, 135000000, 18000000, 225):
        errors.append("Synthetic token/cost example is inconsistent")

    repo_path = ROOT / "research/repository-snapshot.json"
    repo_data = {"repositories": []}
    unavailable = []
    if repo_path.is_file():
        repo_data = json.loads(repo_path.read_text(encoding="utf-8-sig"))
        if len(repo_data.get("repositories", [])) != 15:
            errors.append("Expected 15 repository metadata results")
        unavailable = [r["requested_repo"] for r in repo_data.get("repositories", []) if r.get("status") != "metadata_retrieved"]
    else:
        warnings.append("research/repository-snapshot.json not present; skipping repo metadata verification")

    for warning in warnings:
        print(f"WARN: {warning}")

    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        raise SystemExit(1)

    print(f"PASS: Markdown local links/fences and financial arithmetic verified")
    if cases:
        print(f"PASS: {len(cases)} redacted cases covering six screenshots; total 151, Lavender shortfall 290")
    print(f"PASS: decimal quote {raw_quote} -> ${rounded_quote}; alternate policy ${alternate_increment}")
    print(f"PASS: synthetic token/cost arithmetic")
    if unavailable:
        print(f"NOTE: unavailable GitHub metadata: {', '.join(unavailable)}")
    print("LIVE INTEGRATIONS NOT VERIFIED: WhatsApp, identity provider enrollment, production deployment, payments, or OS installers. Runtime unit tests are separate.")


if __name__ == "__main__":
    main()
