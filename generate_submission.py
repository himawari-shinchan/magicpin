"""Build the canonical 30-line JSONL submission from the expanded challenge data."""
import json
from pathlib import Path

from bot import _compose

ROOT = Path(__file__).parent / "challenge_bundle" / "expanded"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    pairs = load_json(ROOT / "test_pairs.json")["pairs"]
    categories = {p.stem: load_json(p) for p in (ROOT / "categories").glob("*.json")}
    merchants = {load_json(p)["merchant_id"]: load_json(p) for p in (ROOT / "merchants").glob("*.json")}
    customers = {load_json(p)["customer_id"]: load_json(p) for p in (ROOT / "customers").glob("*.json")}
    triggers = {load_json(p)["id"]: load_json(p) for p in (ROOT / "triggers").glob("*.json")}
    output = []
    for pair in pairs:
        merchant = merchants[pair["merchant_id"]]
        category = categories[merchant["category_slug"]]
        trigger = triggers[pair["trigger_id"]]
        customer = customers.get(pair.get("customer_id"))
        output.append({"test_id": pair["test_id"], **_compose(category, merchant, trigger, customer)})
    target = Path(__file__).parent / "submission.jsonl"
    target.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in output), encoding="utf-8")
    print(f"Wrote {len(output)} canonical messages to {target}")


if __name__ == "__main__":
    main()
