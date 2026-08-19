"""
Quick local test — run this directly to test the scraper without the API server.

Usage:
    python test_scraper.py                                  # default address
    python test_scraper.py "2100 Cypress Ave, Los Angeles"  # by address
    python test_scraper.py --ain 5443-016-018               # by AIN
"""
import asyncio
import json
import logging
import sys

from scraper import LADBSScraper

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

DEFAULT_ADDRESS = "2100 Cypress Ave, Los Angeles, CA 90065"


async def main() -> int:
    args = sys.argv[1:]
    scraper = LADBSScraper()

    if args and args[0] in ("--ain", "-a"):
        if len(args) < 2:
            print("--ain requires a value, e.g. --ain 5443-016-018")
            return 2
        target = args[1]
        print(f"Scraping LADBS for AIN: {target}")
        print("This may take 20-40 seconds...\n")
        result = await scraper.scrape_by_ain(target)
    else:
        target = args[0] if args else DEFAULT_ADDRESS
        print(f"Scraping LADBS for: {target}")
        print("This may take 20-40 seconds...\n")
        result = await scraper.scrape(target)

    print("=== SUMMARY ===")
    print(result["summary"])
    print(f"\n=== TOTAL RECORDS: {result['total_records']} ===")

    for i, rec in enumerate(result["records"]):
        print(f"\n[{i+1}] {rec.get('doc_type', 'Unknown')} — {rec.get('doc_number', 'N/A')}")
        print(f"    Date: {rec.get('doc_date', 'N/A')} | Sub-type: {rec.get('sub_type', 'N/A')}")
        if rec.get("attachments"):
            print(f"    Attachments ({len(rec['attachments'])}):")
            for att in rec["attachments"]:
                print(f"      - {att['label']}: {att['url']}")

    diag = result.get("diagnostics", {})
    if result["total_records"] == 0:
        print("\n=== DIAGNOSTICS (no records — why) ===")
        for step in diag.get("steps", []):
            print(f"  . {step}")
        for warn in diag.get("warnings", []):
            print(f"  ! {warn}")

    print("\n=== FULL JSON OUTPUT ===")
    print(json.dumps(result, indent=2))
    return 0 if result["total_records"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
