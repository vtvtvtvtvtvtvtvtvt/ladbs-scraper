"""
Quick local test — run this directly to test the scraper without the API server.
Usage: python test_scraper.py
"""
from scraper import LADBSScraper
import json

address = "2100 Cypress Ave, Los Angeles, CA 90065"

print(f"Scraping LADBS for: {address}")
print("This may take 20-40 seconds...\n")

scraper = LADBSScraper()
result = scraper.scrape(address)

print("=== SUMMARY ===")
print(result["summary"])
print(f"\n=== TOTAL RECORDS: {result['total_records']} ===")

for i, rec in enumerate(result["records"]):
    print(f"\n[{i+1}] {rec.get('doc_type', 'Unknown')} — {rec.get('doc_number', 'N/A')}")
    print(f"    Date: {rec.get('doc_date', 'N/A')} | Status: {rec.get('status', 'N/A')}")
    if rec.get("attachments"):
        print(f"    Attachments ({len(rec['attachments'])}):")
        for att in rec["attachments"]:
            print(f"      - {att['label']}: {att['url']}")

print(f"\n=== FULL JSON OUTPUT ===")
print(json.dumps(result, indent=2))
